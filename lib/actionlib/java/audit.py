# SPDX-FileCopyrightText: 2026 Bernard Ladenthin <bernard.ladenthin@gmail.com>
#
# SPDX-License-Identifier: MIT OR Apache-2.0
"""Audit a published release from the outside: the GitHub release, Maven Central and the run.

The publication gate (publication.py) proves the package was complete when it was frozen. This
module re-checks the release once it exists, from what a consumer sees, without trusting the run
that produced it -- every failure class that ever reached a user reached them through a green
pipeline. It runs on a checkout of the tag (the contract and the POMs at the tag decide what must
exist) with the GitHub release files downloaded into one directory:

  run         exactly one successful run of the publish workflow on the tag, at the tag's commit,
              and that commit on the default branch
  jobs        no job of that run failed or was cancelled; the skipped jobs are exactly the other
              channel's (a skip propagates down `needs:` past any `if:`, so an unexpected one can
              turn a run green while it published nothing)
  assets      the release files are exactly what .github/release-assets.json and the POMs promise,
              each with its detached signature
  central     every Central file (and signature) resolves from Maven Central with the bytes of the
              GitHub file: its .sha256 (or .sha1) against the local digest, no second download
  signatures  every signature verifies with the release key fetched from a public keyserver, and
              ONE signing subkey of that key signed everything
  provenance  the build provenance attestation is verified (gh attestation verify, this tag, this
              workflow) and lists every release file with its digest, from the run of `run`
  bytecode    no class above its artifact's class-file major (per file pattern, first match wins;
              an AAR's classes.jar is scanned too); a pattern that scanned nothing fails
  next        the default branch has moved to the next minor -SNAPSHOT (a warning only: right
              after a release the bump is normally still on a branch)

Configured in the ``post-release`` section of ``.github/actions-config.json``::

    "post-release": {"workflow": ".github/workflows/publish.yml",
                     "signing-key": "<primary key fingerprint, 40 hex>",
                     "bytecode": [{"files": "demo-ffm-*.jar", "major": 66}, {"files": "*", "major": 52}]}

Usage: python3 -m actionlib.java.audit --tag v1.2.3 --assets <dir> [--checks run,jobs,...]
[--wait-minutes N]. Central is polled up to --wait-minutes for files it does not serve yet (it
publishes a validated bundle asynchronously); every other check runs once.
"""

import argparse
import fnmatch
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PurePosixPath

from ..common import config as configuration
from ..common import workflow
from ..common.snapshot import GitHub
from .publication import digest
from .releaseassets import _classifiers, _format, configuration as contract, projects
from .wiring import CONSUMERS, REF_CHECKS

DEFAULTS = {"workflow": ".github/workflows/publish.yml", "signing-key": "",
            "bytecode": [{"files": "*", "major": 52}]}
CHECKS = ("run", "jobs", "assets", "central", "signatures", "provenance", "bytecode", "next")
CENTRAL = "https://repo1.maven.org/maven2"
KEYSERVERS = ("https://keyserver.ubuntu.com/pks/lookup?op=get&options=mr&search=0x{fpr}",
              "https://keys.openpgp.org/vks/v1/by-fingerprint/{fpr}")
FINGERPRINT = re.compile(r"[0-9A-F]{40}")
JOB_NAME = re.compile(r"^    name:\s*(.+?)\s*$", re.MULTILINE)


class Result:
    """The outcome of one check: problems fail the audit, warnings and notes are reported."""

    def __init__(self, name):
        self.name, self.problems, self.warnings, self.notes = name, [], [], []

    def need(self, condition, message):
        if not condition:
            self.problems.append(message)
        return condition


# --- what the release must contain ---------------------------------------------------------------

def _group(pom_path):
    pom = ET.parse(pom_path).getroot()
    return pom.findtext("{*}groupId") or pom.findtext("{*}parent/{*}groupId")


def expected_files(root, config_path=None):
    """(version, files, patterns): files maps each exact release file name to its Central path
    (None for a GitHub-only file); patterns are the fnmatch names of wildcard rules without a
    `name`, each of which must match exactly one release file. Signatures are not listed."""
    root = Path(root).resolve()
    config = contract(root, config_path)
    modules = projects(root, config.get("maven", {}).get("standalone", []))
    version = modules[0][2]
    root_group = _group(root / "pom.xml")
    files, patterns = {}, []

    def central(group, artifact, name):
        return f"{group.replace('.', '/')}/{artifact}/{version}/{name}"

    for folder, artifact, _, packaging, reactor in modules:
        group = _group(folder / "pom.xml") or root_group
        base = f"{artifact}-{version}"
        names = [base + ".pom"]
        if packaging != "pom":
            names += [base + suffix + ".jar" for suffix in ("", "-sources", "-javadoc")]
        if reactor and config.get("maven", {}).get("cyclonedx", False):
            names.append(base + "-cyclonedx.json")
        for name in names:
            files[name] = central(group, artifact, name)
    for rule in config.get("files", []):
        for classifier in _classifiers(root, rule.get("classifiers", [""])):
            source = _format(rule["source"], version, classifier)
            name = _format(rule["name"], version, classifier) if "name" in rule else PurePosixPath(source).name
            if any(c in name for c in "*?["):
                if rule.get("central", False):
                    raise ValueError(f"a Central file needs an exact name, not the pattern {name}")
                patterns.append(name)
                continue
            path = None
            if rule.get("central", False):
                marker = f"-{version}"
                if marker not in name:
                    raise ValueError(f"cannot place {name} on Central: no -{version} in its name")
                path = central(root_group, name[:name.index(marker)], name)
            files[name] = path
    return version, files, patterns


def check_assets(present, files, patterns):
    """The release's file names against the contract: exact membership, one signature each."""
    result = Result("assets")
    present = set(present)
    artifacts = {name for name in present if not name.endswith(".asc")}
    matched = set()
    for pattern in patterns:
        hits = sorted(name for name in artifacts if fnmatch.fnmatchcase(name, pattern))
        result.need(len(hits) == 1, f"{pattern}: expected one release file, found {hits or 'none'}")
        matched.update(hits)
    for name in sorted(set(files) - artifacts):
        result.problems.append(f"missing from the release: {name}")
    for name in sorted(artifacts - set(files) - matched):
        result.problems.append(f"not in the contract: {name}")
    for name in sorted(artifacts):
        result.need(name + ".asc" in present, f"unsigned: {name} has no .asc")
    for name in sorted(present):
        if name.endswith(".asc"):
            result.need(name[:-len(".asc")] in artifacts, f"orphan signature: {name}")
    central = sum(1 for path in files.values() if path)
    result.notes.append(f"{len(artifacts)} files + {len(present) - len(artifacts)} signatures; "
                        f"{central} Central artifacts")
    return result


# --- the run ------------------------------------------------------------------------------------

def tag_commit(github, repository, tag):
    return github.api("GET", f"repos/{repository}/commits/{tag}")["sha"]


def check_run(github, repository, workflow_path, tag, commit):
    """(result, run): the one successful run of the publish workflow on the tag."""
    result = Result("run")
    runs = github.api("GET", f"repos/{repository}/actions/workflows/{PurePosixPath(workflow_path).name}"
                             f"/runs?branch={tag}&per_page=50")["workflow_runs"]
    good = [run for run in runs if run["conclusion"] == "success"]
    result.need(len(good) == 1, f"expected one successful run on {tag}, found "
                                f"{[run['id'] for run in good] or 'none'}")
    run = good[0] if len(good) == 1 else None
    if run:
        result.need(run["head_sha"] == commit, f"run {run['id']} built {run['head_sha']}, the tag is {commit}")
        result.notes.append(f"run {run['id']} attempt {run.get('run_attempt', 1)} ({run['event']}) at {commit}")
    others = [f"{r['id']} {r['conclusion'] or r['status']}" for r in runs if r not in good]
    if others:
        result.notes.append("other runs on the tag (a re-pointed tag leaves these): " + ", ".join(others))
    branch = github.api("GET", f"repos/{repository}")["default_branch"]
    status = github.api("GET", f"repos/{repository}/compare/{commit}...{branch}")["status"]
    result.need(status in ("ahead", "identical"), f"the tag commit is not on {branch} (compare: {status})")
    return result, run


def channel_jobs(jobs_by_id, channel):
    """The display names of one channel's publication jobs, as the workflow names them."""
    ids = [f"publish-{channel}"] + [f"{prefix}-{channel}" for prefix, _ in CONSUMERS]
    if channel in REF_CHECKS:
        ids.append(REF_CHECKS[channel])
    names = set()
    for job_id in ids:
        if job_id in jobs_by_id:
            match = JOB_NAME.search(jobs_by_id[job_id].text)
            names.add(match.group(1).strip("'\"") if match else job_id)
    return names


def list_jobs(github, repository, run_id):
    jobs, page = [], 1
    while True:
        batch = github.api("GET", f"repos/{repository}/actions/runs/{run_id}/jobs?filter=latest"
                                  f"&per_page=50&page={page}")["jobs"]
        jobs += batch
        if len(batch) < 50:
            return jobs
        page += 1


def check_jobs(jobs, other_channel_names):
    """Nothing failed; the skipped jobs are exactly the other channel's."""
    result = Result("jobs")
    counts = {}
    for job in jobs:
        counts[job["conclusion"]] = counts.get(job["conclusion"], 0) + 1
        if job["conclusion"] not in ("success", "skipped"):
            result.problems.append(f"{job['conclusion']}: {job['name']}")
    skipped = {job["name"] for job in jobs if job["conclusion"] == "skipped"}
    for name in sorted(skipped - other_channel_names):
        result.problems.append(f"skipped, but not a job of the other channel: {name}")
    ran = {job["name"] for job in jobs if job["conclusion"] != "skipped"}
    for name in sorted(other_channel_names & ran):
        result.problems.append(f"ran, but belongs to the other channel: {name}")
    for name in sorted(other_channel_names - skipped - ran):
        result.warnings.append(f"the other channel's job {name} is not in the run")
    result.notes.append(", ".join(f"{n} {c}" for c, n in sorted(counts.items(), key=lambda kv: str(kv[0]))))
    return result


# --- Maven Central ------------------------------------------------------------------------------

def fetch(url, timeout=60):
    """The body of url, or None on 404."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "actionlib-audit"}),
                                    timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def central_state(path, local, base=CENTRAL, get=fetch):
    """None when Central serves the local bytes at path, else what is wrong ('absent' if 404).

    Files: the published .sha256, else the .sha1. Central keeps no checksum of its own for a
    signature, which is small, so that is compared byte for byte."""
    url = f"{base}/{path}"
    if path.endswith(".asc"):
        body = get(url)
        if body is None:
            return "absent"
        return None if body == local.read_bytes() else "bytes differ"
    for algorithm in ("sha256", "sha1"):
        body = get(f"{url}.{algorithm}")
        if body is None:
            continue
        expected = body.decode("ascii", "replace").split()[0].lower() if body.strip() else ""
        actual = digest(local) if algorithm == "sha256" else hashlib.sha1(local.read_bytes()).hexdigest()
        return None if expected == actual else f"{algorithm} differs (Central {expected[:12]}…, GitHub {actual[:12]}…)"
    return "absent"


def check_central(files, assets, wait_minutes=0, base=CENTRAL, get=fetch, sleep=time.sleep, clock=time.monotonic):
    result = Result("central")
    pending = {}
    for name, path in files.items():
        if path:
            pending[path] = assets / name
            pending[path + ".asc"] = assets / (name + ".asc")
    deadline = clock() + wait_minutes * 60
    states = {}
    while True:
        for path in sorted(pending):
            if not pending[path].is_file():
                states[path] = "not downloaded from the release"
            else:
                states[path] = central_state(path, pending[path], base, get)
        absent = [p for p, s in states.items() if s == "absent"]
        if not absent or clock() >= deadline:
            break
        print(f"Central does not serve {len(absent)} file(s) yet (e.g. {absent[0]}); waiting", file=sys.stderr)
        sleep(min(300, max(1, deadline - clock())))
        pending = {p: pending[p] for p in absent}
    for path, state in sorted(states.items()):
        if state:
            result.problems.append(f"{path}: {state}")
    result.notes.append(f"{sum(1 for s in states.values() if not s)} of {len(states)} files served with "
                        f"the release's bytes")
    return result


# --- signatures ---------------------------------------------------------------------------------

def fetch_key(fingerprint, get=fetch):
    for template in KEYSERVERS:
        try:
            body = get(template.format(fpr=fingerprint))
        except (urllib.error.URLError, OSError):
            body = None
        if body and b"BEGIN PGP PUBLIC KEY BLOCK" in body:
            return body
    raise ValueError(f"no keyserver serves the release key {fingerprint}")


def gpg(gnupghome, *args):
    """gpg's status lines (stdout). A throwaway home, no agent: nothing here needs a secret key, and
    an agent that fails to start (Git for Windows) makes gpg exit 2 after a successful import. POSIX
    paths, which Git for Windows' gpg needs and every other gpg accepts. The exit code is not read:
    the status lines say what happened."""
    return subprocess.run(["gpg", "--homedir", Path(gnupghome).as_posix(), "--batch", "--no-autostart",
                           "--status-fd", "1", *(Path(a).as_posix() if isinstance(a, Path) else a for a in args)],
                          capture_output=True, text=True).stdout


def import_key(gnupghome, key, fingerprint):
    """Imports the key file; the keyserver's answer must be the requested key, nothing else."""
    imported = set(re.findall(r"^\[GNUPG:\] IMPORT_OK \d+ ([0-9A-F]{40})$", gpg(gnupghome, "--import", key),
                              re.MULTILINE))
    if imported != {fingerprint}:
        raise ValueError(f"the keyserver's answer for {fingerprint} imported {sorted(imported) or 'nothing'}")


def verify_signature(gnupghome, signature, artifact):
    """(signing key fingerprint, primary key fingerprint) or None, from gpg's status lines."""
    status = gpg(gnupghome, "--verify", signature, artifact)
    good = re.search(r"^\[GNUPG:\] GOODSIG ", status, re.MULTILINE)
    valid = re.search(r"^\[GNUPG:\] VALIDSIG (\S+) .* (\S+)$", status, re.MULTILINE)
    return (valid.group(1), valid.group(2)) if good and valid else None


def check_signatures(assets, fingerprint, get=fetch, verify=verify_signature):
    result = Result("signatures")
    if not result.need(FINGERPRINT.fullmatch(fingerprint or "") is not None,
                       "post-release.signing-key must be the release key's 40-hex primary fingerprint"):
        return result
    with tempfile.TemporaryDirectory(prefix="audit-gnupg-") as home:
        os.chmod(home, 0o700)
        key = Path(home) / "release-key.asc"
        key.write_bytes(fetch_key(fingerprint, get))
        import_key(home, key, fingerprint)
        signers = {}
        for signature in sorted(assets.glob("*.asc")):
            artifact = signature.with_suffix("")
            outcome = verify(home, signature, artifact) if artifact.is_file() else None
            if not result.need(outcome is not None, f"bad or unverifiable signature: {signature.name}"):
                continue
            result.need(outcome[1] == fingerprint, f"{signature.name}: signed by key {outcome[1]}, not {fingerprint}")
            signers.setdefault(outcome[0], []).append(signature.name)
    result.need(len(signers) <= 1, "more than one signing key: " + "; ".join(
        f"{fpr} ({len(names)} files)" for fpr, names in sorted(signers.items())))
    result.need(bool(signers), "no signature verified")
    for fpr, names in signers.items():
        result.notes.append(f"{len(names)} signatures, all by subkey {fpr} of {fingerprint}")
    return result


# --- provenance ---------------------------------------------------------------------------------

def attestations(repository, workflow_path, tag, sample):
    """gh attestation verify of one release file, pinned to this tag and this workflow."""
    out = subprocess.run(["gh", "attestation", "verify", str(sample), "--repo", repository,
                          "--source-ref", f"refs/tags/{tag}",
                          "--signer-workflow", f"{repository}/{workflow_path}", "--format", "json"],
                         capture_output=True, text=True)
    if out.returncode:
        raise ValueError(f"gh attestation verify {sample.name}: {out.stderr.strip()}")
    return json.loads(out.stdout)


def check_provenance(verified, assets, repository, run, commit):
    """Every release file is a subject, with its digest, of a verified statement from the run."""
    result = Result("provenance")
    subjects = {}
    invocation = f"https://github.com/{repository}/actions/runs/{run['id']}/attempts/" if run else None
    for item in verified:
        statement = item["verificationResult"]["statement"]
        certificate = item["verificationResult"].get("signature", {}).get("certificate", {})
        metadata = statement.get("predicate", {}).get("runDetails", {}).get("metadata", {})
        if invocation and not metadata.get("invocationId", "").startswith(invocation):
            result.notes.append(f"an attestation of another run: {metadata.get('invocationId')}")
            continue
        result.need(certificate.get("githubWorkflowSHA") == commit,
                    f"attestation signed at {certificate.get('githubWorkflowSHA')}, the tag is {commit}")
        for subject in statement.get("subject", []):
            subjects.setdefault(subject["name"], set()).add(subject.get("digest", {}).get("sha256"))
    files = sorted(p for p in assets.iterdir() if p.is_file())
    for path in files:
        if path.name not in subjects:
            result.problems.append(f"not attested: {path.name}")
        elif digest(path) not in subjects[path.name]:
            result.problems.append(f"attested with another digest: {path.name}")
    result.notes.append(f"{len(files)} files checked against {len(subjects)} subjects")
    return result


# --- bytecode -----------------------------------------------------------------------------------

def class_majors(data):
    """{entry: major} of every class a classpath JVM loads (module-info and versions/ skipped)."""
    majors = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for entry in archive.namelist():
            if entry.endswith(".class") and not entry.endswith("module-info.class") \
                    and not entry.startswith("META-INF/versions/"):
                header = archive.read(entry)[:8]
                if header[:4] == b"\xca\xfe\xba\xbe":
                    majors[entry] = int.from_bytes(header[6:8], "big")
            elif entry == "classes.jar":
                majors.update({f"classes.jar!{k}": v for k, v in class_majors(archive.read(entry)).items()})
    return majors


def check_bytecode(assets, rules):
    result = Result("bytecode")
    scanned = {index: 0 for index in range(len(rules))}
    for path in sorted(assets.iterdir()):
        if path.suffix not in (".jar", ".aar") or path.name.endswith(("-sources.jar", "-javadoc.jar")):
            continue
        index = next((i for i, rule in enumerate(rules) if fnmatch.fnmatchcase(path.name, rule["files"])), None)
        if index is None:
            result.problems.append(f"{path.name}: no bytecode rule matches")
            continue
        try:
            majors = class_majors(path.read_bytes())
        except zipfile.BadZipFile:
            result.problems.append(f"{path.name}: not a zip archive")
            continue
        scanned[index] += len(majors)
        above = sorted(entry for entry, major in majors.items() if major > rules[index]["major"])
        if above:
            result.problems.append(f"{path.name}: {len(above)} classes above major {rules[index]['major']}, "
                                   f"e.g. {above[0]} ({majors[above[0]]})")
    for index, count in scanned.items():
        result.need(count > 0, f"the rule {rules[index]['files']} (major {rules[index]['major']}) scanned no class")
        result.notes.append(f"{rules[index]['files']}: {count} classes <= {rules[index]['major']}")
    return result


# --- the next development version ---------------------------------------------------------------

def check_next(version, branch_version, branch="main"):
    result = Result("next")
    match = re.fullmatch(r"(\d+)\.(\d+)\.\d+", version)
    expected = f"{match.group(1)}.{int(match.group(2)) + 1}.0-SNAPSHOT" if match else None
    if branch_version == expected:
        result.notes.append(f"{branch} is at {branch_version}")
    elif branch_version == version:
        result.warnings.append(f"{branch} still names the released {version}; the bump to {expected} is not "
                               f"merged yet (the snapshot channel refuses a release version until it is)")
    else:
        result.warnings.append(f"{branch} is at {branch_version}, the next minor after {version} is {expected}")
    return result


def branch_version(github, repository):
    import base64
    branch = github.api("GET", f"repos/{repository}")["default_branch"]
    content = github.api("GET", f"repos/{repository}/contents/pom.xml?ref={branch}")["content"]
    pom = ET.fromstring(base64.b64decode(content))
    return branch, pom.findtext("{*}version") or pom.findtext("{*}parent/{*}version")


# --- the command --------------------------------------------------------------------------------

def settings(root, config_path=None):
    merged = configuration.section(configuration.load(root, config_path), "post-release", DEFAULTS)
    rules = merged["bytecode"]
    if (not isinstance(rules, list) or not rules
            or any(not isinstance(r, dict) or set(r) != {"files", "major"} or not isinstance(r["files"], str)
                   or type(r["major"]) is not int for r in rules)):
        raise ValueError("post-release.bytecode: expected a nonempty list of {\"files\": <pattern>, \"major\": <int>}")
    return merged


def report(results, out=sys.stdout):
    failed = 0
    for result in results:
        state = "FAIL" if result.problems else ("warn" if result.warnings else "ok")
        print(f"== {result.name}: {state}", file=out)
        for note in result.notes:
            print(f"   {note}", file=out)
        for warning in result.warnings:
            print(f"::warning::{result.name}: {warning}", file=out)
        for problem in result.problems:
            print(f"::error::{result.name}: {problem}", file=out)
        failed += bool(result.problems)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write("| Check | Result | Details |\n|---|---|---|\n")
            for r in results:
                state = "❌" if r.problems else ("⚠️" if r.warnings else "✅")
                details = "<br>".join((r.problems + r.warnings + r.notes)[:6]).replace("|", "\\|")
                stream.write(f"| {r.name} | {state} | {details} |\n")
    return failed


def main(argv=None, github=None, out=sys.stdout):
    parser = argparse.ArgumentParser(prog="python3 -m actionlib.java.audit", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=".", help="a checkout of the tag (default: the current directory)")
    parser.add_argument("--config", help=f"configuration file (default: {configuration.DEFAULT_PATH})")
    parser.add_argument("--assets-config", help="release asset contract (default: .github/release-assets.json)")
    parser.add_argument("--tag", required=True, help="the release tag, v<version>")
    parser.add_argument("--assets", required=True, type=Path, help="the GitHub release files, downloaded")
    parser.add_argument("--checks", default=",".join(CHECKS), help="comma-separated subset (default: all)")
    parser.add_argument("--wait-minutes", type=int, default=0, help="poll Central this long for absent files")
    args = parser.parse_args(argv)
    checks = [c.strip() for c in args.checks.split(",") if c.strip()]
    unknown = sorted(set(checks) - set(CHECKS))
    if unknown:
        parser.error(f"unknown check(s) {', '.join(unknown)}; known: {', '.join(CHECKS)}")
    github = github or GitHub()
    results = []
    try:
        root = Path(args.root).resolve()
        conf = settings(root, args.config)
        owner, name = configuration.repository(root)
        repository = f"{owner}/{name}"
        version, files, patterns = expected_files(root, args.assets_config)
        if args.tag != f"v{version}":
            raise ValueError(f"the checkout is at version {version}, not the tag {args.tag}")
        assets = args.assets.resolve()
        present = sorted(p.name for p in assets.iterdir() if p.is_file())
        need_run = {"run", "jobs", "provenance"} & set(checks)
        commit = tag_commit(github, repository, args.tag) if need_run else None
        run = None
        if need_run:
            run_result, run = check_run(github, repository, conf["workflow"], args.tag, commit)
            if "run" in checks:
                results.append(run_result)
        for check in checks:
            if check == "jobs":
                jobs_by_id = workflow.parse(configuration.inside(root, conf["workflow"]).read_text(encoding="utf-8"))
                if run is None:
                    results.append(Result("jobs"))
                    results[-1].problems.append("no single successful run to read")
                else:
                    results.append(check_jobs(list_jobs(github, repository, run["id"]),
                                              channel_jobs(jobs_by_id, "snapshot")))
            elif check == "assets":
                results.append(check_assets(present, files, patterns))
            elif check == "central":
                results.append(check_central(files, assets, args.wait_minutes))
            elif check == "signatures":
                results.append(check_signatures(assets, conf["signing-key"]))
            elif check == "provenance":
                sample = next((assets / n for n in present if not n.endswith(".asc")), None)
                if sample is None:
                    raise ValueError(f"no release files in {assets}")
                results.append(check_provenance(attestations(repository, conf["workflow"], args.tag, sample),
                                                assets, repository, run, commit))
            elif check == "bytecode":
                results.append(check_bytecode(assets, conf["bytecode"]))
            elif check == "next":
                branch, current = branch_version(github, repository)
                results.append(check_next(version, current, branch))
    except (ValueError, OSError, RuntimeError, KeyError, ET.ParseError, subprocess.CalledProcessError) as error:
        print(f"::error::{error}", file=out)
        report(results, out)
        return 1
    failed = report(results, out)
    print(f"post-release audit of {args.tag}: {len(results)} checks, {failed} failed", file=out)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
