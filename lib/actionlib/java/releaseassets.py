# SPDX-FileCopyrightText: 2026 Bernard Ladenthin <bernard.ladenthin@gmail.com>
#
# SPDX-License-Identifier: MIT OR Apache-2.0
"""Collect signed Maven release assets and explicitly configured extra files.

No repository names or native/Android layouts live here: the contract is the repository's
.github/release-assets.json, documented in docs/publication.md of bernardladenthin/actions.
"""

import argparse
import csv
import json
import os
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from ..common.signatures import signature_gaps  # noqa: F401 -- re-exported for the callers


def _keys(value, allowed, context):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError(f"invalid {context}; allowed keys: {', '.join(allowed)}")


def _inside(root, relative):
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError(f"expected a repository-relative path: {relative!r}")
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"path escapes repository: {relative}")
    return path


def configuration(root, path=None):
    """Read the explicit contract; unknown keys fail instead of silently dropping assets.

    The contract is part of the repository: a relative --config is relative to the repository root,
    and a path outside the repository is refused.
    """
    root = Path(root).resolve()
    path = (root / (path if path is not None else ".github/release-assets.json")).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"release asset configuration outside the repository: {path}")
    config = json.loads(path.read_text())
    _keys(config, ("schema", "maven", "files", "publication"), "release asset configuration")
    if type(config.get("schema")) is not int or config["schema"] != 1:
        raise ValueError("release asset configuration requires schema: 1")
    publication = config.get("publication", {})
    _keys(publication, ("central_roots",), "publication configuration")
    if "central_roots" in publication and (not isinstance(publication["central_roots"], list)
            or not publication["central_roots"]
            or any(not isinstance(path, str) or not path for path in publication["central_roots"])):
        raise ValueError("publication.central_roots must be a nonempty list of paths")
    maven = config.get("maven", {})
    _keys(maven, ("standalone", "cyclonedx"), "maven configuration")
    if not isinstance(maven.get("standalone", []), list):
        raise ValueError("maven.standalone must be a list of module paths")
    if type(maven.get("cyclonedx", False)) is not bool:
        raise ValueError("maven.cyclonedx must be a boolean")
    if not isinstance(config.get("files", []), list):
        raise ValueError("files must be a list")
    for rule in config.get("files", []):
        if isinstance(rule, dict) and "central" in rule and type(rule["central"]) is not bool:
            raise ValueError("file rule central must be a boolean")
    return config


def projects(root, standalone=()):
    """Discover the reactor recursively, then optional separately published Maven projects."""
    root = Path(root).resolve()
    seen, result = set(), []

    def visit(folder, reactor=True):
        folder = folder.resolve()
        if not folder.is_relative_to(root) or folder in seen:
            raise ValueError(f"invalid or repeated Maven module: {folder}")
        seen.add(folder)
        pom = ET.parse(folder / "pom.xml").getroot()
        artifact = pom.findtext("{*}artifactId")
        version = pom.findtext("{*}version") or pom.findtext("{*}parent/{*}version")
        packaging = pom.findtext("{*}packaging", "jar")
        if (not artifact or not version or not re.fullmatch(r"[\w.-]+", artifact)
                or not re.fullmatch(r"[\w.+-]+", version)
                or packaging not in ("pom", "jar", "maven-plugin")):
            raise ValueError(f"unsupported release coordinates in {folder / 'pom.xml'}")
        if result and version != result[0][2]:
            raise ValueError(f"release version mismatch in {folder / 'pom.xml'}: {version}")
        result.append((folder, artifact, version, packaging, reactor))
        for module in pom.findall("{*}modules/{*}module"):
            visit(_inside(root, str(folder.relative_to(root) / (module.text or "").strip())), reactor)

    visit(root)
    for module in standalone:
        visit(_inside(root, module), reactor=False)
    return result


def _classifiers(root, spec):
    if isinstance(spec, dict):
        _keys(spec, ("csv", "column"), "classifier CSV")
        if not isinstance(spec.get("column"), str) or not spec["column"]:
            raise ValueError("classifier CSV requires a column")
        with _inside(root, spec.get("csv")).open() as stream:
            rows = csv.DictReader(line for line in stream if line.strip() and not line.lstrip().startswith("#"))
            if spec["column"] not in (rows.fieldnames or []):
                raise ValueError(f"missing classifier CSV column: {spec['column']}")
            spec = [row[spec["column"]] for row in rows]
    if (not isinstance(spec, list) or not spec or any(not isinstance(c, str)
            or not re.fullmatch(r"[\w.-]*", c) for c in spec) or len(set(spec)) != len(spec)):
        raise ValueError("classifiers must be a nonempty list of unique classifier names")
    return spec


def _format(template, version, classifier):
    if not isinstance(template, str) or not template:
        raise ValueError("file source/name must be a nonempty string")
    try:
        return template.format(version=version, classifier=classifier)
    except (KeyError, ValueError, IndexError, AttributeError) as error:
        raise ValueError(f"invalid release filename template: {template}") from error


def file_classifiers(root, index, config_path=None):
    """Let packaging loops consume the same classifier list the collector checks."""
    if index < 0:
        raise ValueError("file rule index must not be negative")
    config = configuration(root, config_path)
    try:
        item = config.get("files", [])[index]
    except IndexError as error:
        raise ValueError(f"no file rule at index {index}") from error
    _keys(item, ("source", "name", "classifiers", "central"), "file rule")
    return _classifiers(Path(root).resolve(), item.get("classifiers", [""]))


def collect(root, destination, config_path=None, *, maven_only=False, maven_assets=None):
    """Copy available outputs, returning all missing files/signatures. Never hide partial output.

    maven_only stages deploy-signed files before later packaging overwrites target/.
    maven_assets reads those staged files by their final names during the final collection.
    """
    root, destination = Path(root).resolve(), Path(destination).resolve()
    config = configuration(root, config_path)
    modules = projects(root, config.get("maven", {}).get("standalone", []))
    version, expected, errors = modules[0][2], [], []
    for folder, artifact, _, packaging, reactor in modules:
        base = f"{artifact}-{version}"
        outputs = [(folder / "target" / f"{base}.pom", f"{base}.pom")]
        if packaging != "pom":
            for classifier in ("", "-sources", "-javadoc"):
                name = f"{base}{classifier}.jar"
                outputs.append((folder / "target" / name, name))
        if reactor and config.get("maven", {}).get("cyclonedx", False):
            outputs.append((folder / "target/bom.json", f"{base}-cyclonedx.json"))
        for source, name in outputs:
            expected.append((Path(maven_assets) / name if maven_assets is not None else source, name))

    for item in [] if maven_only else config.get("files", []):
        _keys(item, ("source", "name", "classifiers", "central"), "file rule")
        for classifier in _classifiers(root, item.get("classifiers", [""])):
            pattern = _format(item.get("source"), version, classifier)
            _inside(root, pattern)
            matches = sorted(root.glob(pattern))
            if len(matches) != 1:
                errors.append(f"expected one release output matching {pattern}, found {len(matches)}")
                continue
            source = matches[0]
            _inside(root, str(source.relative_to(root)))
            name = _format(item["name"], version, classifier) if "name" in item else source.name
            expected.append((source, name))

    names = set()
    for source, name in expected:
        if (Path(name).name != name or name in (".", "..") or name.endswith(".asc")
                or name in names or name + ".asc" in names):
            raise ValueError(f"invalid or duplicate release asset filename: {name}")
        names.update((name, name + ".asc"))
        if destination == source.parent.resolve():
            raise ValueError("asset destination overlaps release inputs")
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError(f"asset destination must be empty: {destination}")
    for source, name in expected:
        for suffix in ("", ".asc"):
            path = Path(str(source) + suffix).resolve()
            if not path.is_file() or path.stat().st_size == 0:
                shown = path.relative_to(root) if path.is_relative_to(root) else path
                errors.append(f"missing or empty release output: {shown.as_posix()}")
                continue
            shutil.copyfile(path, destination / (name + suffix))
    return errors


def dry_run(root, config_path=None):
    """Problems with the repository's contract, found without a build: every expected output is
    faked in a scratch copy (the POMs, the CSVs the rules name and the JSON itself), collected,
    and the result compared with what the contract promises. Runs in every pull request, so a
    contract that would fail a publication fails long before one."""
    import tempfile

    repo = Path(root).resolve()
    config = configuration(repo, config_path)
    problems = []
    with tempfile.TemporaryDirectory(prefix="release-assets-dry-run-") as temp:
        scratch = Path(temp)
        expected = set()

        def copy(relative):
            target = scratch / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(repo / relative, target)

        def artifact(relative, name):
            path = scratch / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            for suffix in ("", ".asc"):
                Path(str(path) + suffix).write_text("fixture " + str(relative) + suffix)
                expected.add(name + suffix)

        contract = Path(config_path) if config_path else Path(".github/release-assets.json")
        copy(contract)
        modules = projects(repo, config.get("maven", {}).get("standalone", []))
        version = modules[0][2]
        for folder, name, _, packaging, reactor in modules:
            relative = folder.relative_to(repo)
            copy(relative / "pom.xml")
            base = f"{name}-{version}"
            artifact(relative / "target" / f"{base}.pom", f"{base}.pom")
            if packaging != "pom":
                for classifier in ("", "-sources", "-javadoc"):
                    artifact(relative / "target" / f"{base}{classifier}.jar", f"{base}{classifier}.jar")
            if reactor and config.get("maven", {}).get("cyclonedx", False):
                artifact(relative / "target/bom.json", f"{base}-cyclonedx.json")
        for index, rule in enumerate(config.get("files", [])):
            if isinstance(rule.get("classifiers"), dict):
                copy(rule["classifiers"]["csv"])
            for classifier in file_classifiers(repo, index, config_path):
                source = rule["source"].format(version=version, classifier=classifier).replace("*", "fixture")
                name = rule.get("name", Path(source).name).format(version=version, classifier=classifier)
                artifact(source, name)
        destination = scratch / "collected"
        problems += collect(scratch, destination, config_path)
        if not problems:
            collected = {path.name for path in destination.iterdir()}
            problems += [f"expected but not collected: {name}" for name in sorted(expected - collected)]
            problems += [f"collected but not expected: {name}" for name in sorted(collected - expected)]
            if signature_gaps(destination)[0] != 0:
                problems.append("the collected files are not all signed")
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 -m actionlib.java.releaseassets",
                                     description="Collect assets using the repository's explicit release-assets.json contract.")
    parser.add_argument("destination", nargs="?")
    parser.add_argument("--root", default=".", help="the repository (default: the current directory)")
    parser.add_argument("--config", help="JSON contract (default: .github/release-assets.json)")
    parser.add_argument("--maven-only", action="store_true", help="stage signed Maven files before a rebuild")
    parser.add_argument("--maven-assets", help="read Maven files from a previous staging directory")
    parser.add_argument("--list-classifiers", type=int, metavar="RULE", help="print classifiers of a file rule (zero based)")
    parser.add_argument("--dry-run", action="store_true",
                        help="check the contract against faked outputs in a scratch copy, collect nothing")
    args = parser.parse_args(argv)
    if args.dry_run:
        try:
            problems = dry_run(args.root, args.config)
        except (ValueError, OSError, KeyError, ET.ParseError) as error:
            problems = [str(error)]
        for problem in problems:
            print(f"::error::{problem}")
        print(f"release asset contract dry run: {len(problems)} problems")
        return 1 if problems else 0
    if args.list_classifiers is None and args.destination is None:
        parser.error("destination is required unless --list-classifiers or --dry-run is used")
    errors = []
    try:
        root = Path(args.root).resolve()
        if args.list_classifiers is not None:
            if args.list_classifiers < 0:
                raise ValueError("file rule index must not be negative")
            print("\n".join(file_classifiers(root, args.list_classifiers, args.config)))
            return 0
        errors = collect(root, args.destination, args.config,
                         maven_only=args.maven_only, maven_assets=args.maven_assets)
    except (ValueError, OSError, ET.ParseError) as error:
        errors.append(str(error))
    for error in errors:
        print(f"::error::{error}")
    if not errors:
        print(f"Collected {len(list(Path(args.destination).iterdir()))} release files including signatures.")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.write(f"complete={'false' if errors else 'true'}\n")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
