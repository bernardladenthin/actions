# SPDX-FileCopyrightText: 2026 Bernard Ladenthin <bernard.ladenthin@gmail.com>
#
# SPDX-License-Identifier: MIT OR Apache-2.0
"""The Maven dependencies and plugins a family of repositories has in common, compared across the
repositories: a warning per coordinate whose version differs.

Repositories of one family pin the same build tooling (JUnit, Error Prone, NullAway, Spotless, SpotBugs,
JaCoCo, the Maven plugins, ...), and Dependabot bumps each repository on its own, so they drift one
pull request at a time. This check makes the drift visible in every repository's checks. It compares what the POMs actually USE -- every `groupId:artifactId` of a plugin, dependency or
annotation-processor path with its version, plus the formatter version Spotless is configured with,
`${property}` references resolved -- not property
names, which differ between repositories and include values that must differ (the Java release).

It also compares the Kotlin the POMs use with every Kotlin Gradle plugin a `*.gradle.kts` of the
repository pins: Dependabot's maven ecosystem bumps `kotlin.version` and never sees the Gradle files,
which is how a Compose compiler plugin stayed one patch release behind the POMs.

Warnings, never failures: a bump lands in one pull request per repository, and the first one merged
would otherwise turn the others red. The family's own artifacts (``ignore-groups``) are left out: a
sibling depends on the last RELEASE of another while that one is at its next snapshot.

Configured in the ``maven-versions`` section of ``.github/actions-config.json``:

- ``poms``: this repository's POMs (required);
- ``family``: a family of ``families/<name>.json`` in bernardladenthin/actions -- the repositories
  to compare with; or ``siblings``: the repositories (``owner/name``) given directly;
- ``ignore-groups``: groupIds left out (the family's own artifacts).

A sibling's POMs are the ``maven-versions.poms`` of its own ``.github/actions-config.json`` on its
default branch, so a repository's POM list is written down once, in that repository.
"""

import argparse
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from ..common import config as configuration

NS = "{http://maven.apache.org/POM/4.0.0}"
FAMILIES = Path(__file__).resolve().parents[3] / "families"
DEFAULTS = {"family": None, "siblings": [], "ignore-groups": []}
DEFAULT_PLUGIN_GROUP = "org.apache.maven.plugins"
VERSIONED = ("plugin", "dependency", "path", "annotationProcessorPath")
# Versions a plugin's <configuration> names without coordinates: Spotless takes its formatter as
# <palantirJavaFormat><version>. The versions plugin cannot see those, which is how palantir fell
# behind unnoticed more than once (workspace crossrepostatus, "Tool versions").
CONFIGURED = {"palantirJavaFormat": "com.palantir.javaformat:palantir-java-format"}
RAW_URL = "https://raw.githubusercontent.com/{repository}/HEAD/{path}"
PROPERTY = re.compile(r"\$\{([^}]+)\}")
KOTLIN_GROUP = "org.jetbrains.kotlin"
# `id("org.jetbrains.kotlin.plugin.compose") version "2.4.21"` and `kotlin("jvm") version "2.4.21"`
GRADLE_KOTLIN = re.compile(
    r'(?:id\("(org\.jetbrains\.kotlin(?:\.[A-Za-z.]+)?)"\)|kotlin\("([A-Za-z.]+)"\))\s+version\s+"([^"]+)"')
SKIPPED_DIRS = {".git", ".gradle", ".idea", "build", "node_modules", "target"}


def coordinates(pom_texts, ignore_groups=()):
    """groupId:artifactId -> set of versions used across the POMs of one repository. Properties
    are looked up across all of them (a child module uses its parent's), up to a few levels deep;
    a version that does not resolve to a literal is left out."""
    roots = [ET.fromstring(text) for text in pom_texts]
    props = {}
    for root in roots:
        for element in root.findall(NS + "properties/*"):
            props[element.tag[len(NS):]] = (element.text or "").strip()

    def resolve(value):
        for _ in range(5):
            match = PROPERTY.fullmatch(value)
            if not match or match.group(1) not in props:
                break
            value = props[match.group(1)]
        return None if "${" in value else value

    found = {}
    for root in roots:
        for element in root.iter():
            tag = element.tag[len(NS):]
            if tag in CONFIGURED:
                version = resolve((element.findtext(NS + "version") or "").strip())
                if version:
                    found.setdefault(CONFIGURED[tag], set()).add(version)
                continue
            if tag not in VERSIONED:
                continue
            artifact, version = element.findtext(NS + "artifactId"), element.findtext(NS + "version")
            if not artifact or not version:
                continue
            group = (element.findtext(NS + "groupId") or DEFAULT_PLUGIN_GROUP).strip()
            version = resolve(version.strip())
            if version and group not in ignore_groups:
                found.setdefault(f"{group}:{artifact.strip()}", set()).add(version)
    return found


def compare(own, others):
    """Warnings for every coordinate another repository uses in a different version."""
    warnings = []
    for repo, theirs in sorted(others.items()):
        for coordinate in sorted(set(own) & set(theirs)):
            if own[coordinate] != theirs[coordinate]:
                warnings.append(f"{coordinate} is {', '.join(sorted(own[coordinate]))} here and "
                                f"{', '.join(sorted(theirs[coordinate]))} in {repo}: bump it in every repository")
    return warnings


def gradle_kotlin_plugins(root):
    """(path, plugin id, version) for every Kotlin Gradle plugin a `*.gradle.kts` below root pins, in
    path order; build output and the tools' own directories are not searched."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIPPED_DIRS)
        for name in sorted(filenames):
            if not name.endswith(".gradle.kts"):
                continue
            path = os.path.join(dirpath, name)
            with open(path, encoding="utf-8") as f:
                text = f.read()
            for match in GRADLE_KOTLIN.finditer(text):
                plugin = match.group(1) or f"{KOTLIN_GROUP}.{match.group(2)}"
                found.append((os.path.relpath(path, root).replace(os.sep, "/"), plugin, match.group(3)))
    return found


def compare_gradle_kotlin(own, plugins):
    """Warnings for every Kotlin Gradle plugin whose version is not one the POMs use for
    `org.jetbrains.kotlin:*`; nothing when the POMs use no Kotlin or no Gradle file pins a plugin."""
    maven = set()
    for coordinate, used in own.items():
        if coordinate.startswith(KOTLIN_GROUP + ":"):
            maven |= used
    if not maven:
        return []
    return [f"{plugin} is {version} in {path} and the POMs use Kotlin {', '.join(sorted(maven))}: "
            f"Dependabot bumps only the POMs, bump the Gradle plugin with them"
            for path, plugin, version in plugins if version not in maven]


def fetch(repository, path):
    with urllib.request.urlopen(RAW_URL.format(repository=repository, path=path), timeout=20) as response:
        return response.read().decode("utf-8")


def siblings(settings, families=None):
    """The repositories (owner/name) to compare with, from ``siblings`` or the ``family`` file."""
    families = families or FAMILIES
    listed = configuration.strings(settings["siblings"], "maven-versions.siblings") if settings["siblings"] else []
    if settings["family"] is None:
        return listed
    if listed:
        raise ValueError("maven-versions: give either family or siblings, not both")
    name = settings["family"]
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValueError(f"maven-versions.family: invalid family name {name!r}")
    path = Path(families) / f"{name}.json"
    if not path.is_file():
        known = sorted(p.stem for p in Path(families).glob("*.json"))
        raise ValueError(f"maven-versions.family: no family {name!r}; known: {', '.join(known)}")
    family = json.loads(path.read_text(encoding="utf-8"))
    return configuration.strings(family.get("repositories"), f"families/{name}.json repositories")


def read_local(root, poms):
    return [configuration.inside(root, path, "POM").read_text(encoding="utf-8") for path in poms]


def sibling_poms(repository, fetcher):
    """A sibling's POM list: the maven-versions.poms of its own configuration."""
    data = json.loads(fetcher(repository, configuration.DEFAULT_PATH))
    return configuration.strings(data.get("maven-versions", {}).get("poms"), f"{repository} maven-versions.poms")


def main(argv=None, fetcher=fetch, out=sys.stdout, err=sys.stderr):
    parser = argparse.ArgumentParser(prog="python3 -m actionlib.java.versions",
                                     description="Compare Maven versions with the sibling repositories (warnings only).")
    parser.add_argument("--root", default=".", help="the repository (default: the current directory)")
    parser.add_argument("--config", help=f"configuration file (default: {configuration.DEFAULT_PATH})")
    args = parser.parse_args(argv)
    root = args.root
    try:
        settings = configuration.section(configuration.load(root, args.config, required=True), "maven-versions",
                                         DEFAULTS, required=("poms",))
        poms = configuration.strings(settings["poms"], "maven-versions.poms")
        ignore = (configuration.strings(settings["ignore-groups"], "maven-versions.ignore-groups")
                  if settings["ignore-groups"] else [])
        repositories = siblings(settings)
        own = coordinates(read_local(root, poms), ignore)
    except (ValueError, OSError, ET.ParseError) as error:
        print(f"::error::{error}", file=err)
        return 2
    owner, name = configuration.repository(root)
    here = f"{owner}/{name}" if owner else name
    others = {}
    for sibling in repositories:
        if sibling == here:
            continue
        texts = []
        try:
            paths = sibling_poms(sibling, fetcher)
        except Exception as e:  # noqa: BLE001 -- a sibling that cannot be read is a warning, never a red
            print(f"::warning::could not read {sibling}'s {configuration.DEFAULT_PATH}: {e}", file=err)
            paths = []
        for path in paths:
            try:
                texts.append(fetcher(sibling, path))
            except Exception as e:  # noqa: BLE001 -- a missing POM skips that file, not the repository
                print(f"::warning::could not read {sibling}'s {path}: {e}", file=err)
        if texts:
            others[sibling.split("/")[-1]] = coordinates(texts, ignore)
    warnings = compare(own, others) + compare_gradle_kotlin(own, gradle_kotlin_plugins(root))
    for warning in warnings:
        print(f"::warning::{warning}", file=err)
    shared = set(own).intersection(*others.values()) if others else set()
    print(f"{len(own)} versioned coordinates here, {len(shared)} used by every repository read, "
          f"{len(warnings)} version differences (compared with {', '.join(sorted(others)) or 'no other repository'})",
          file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
