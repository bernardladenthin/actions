# SPDX-FileCopyrightText: 2026 Bernard Ladenthin <bernard.ladenthin@gmail.com>
#
# SPDX-License-Identifier: MIT OR Apache-2.0
"""The publication jobs of a repository's publish workflow are wired the way the actions expect.

The signed publication is one gate and three independent consumers (docs/publication.md): the
``publish-<channel>`` job ends in ``java/finalize-publication``, and ``central-<channel>``,
``github-<channel>`` and ``attest-<channel>`` each need that job alone and run only on its
``assets_complete`` output. The actions cannot see how a repository calls them, so this check reads
the workflow: a consumer that also waits for another job, a gate that is skipped, a snapshot
transfer outside its concurrency group or an attestation without its permissions would each still
run green somewhere and break the contract only on a real publication.

Configured in the ``publication-wiring`` section of ``.github/actions-config.json``: ``workflow``
(default ``.github/workflows/publish.yml``) and ``channels`` (default ``snapshot`` and ``release``).
The ref checks are ``check-snapshot`` and ``check-tag``.
"""

import argparse
import re
import sys

from ..common import config as configuration
from ..common import workflow

DEFAULTS = {"workflow": ".github/workflows/publish.yml", "channels": ["snapshot", "release"]}
REF_CHECKS = {"snapshot": "check-snapshot", "release": "check-tag"}
CONSUMERS = (("central", "publish-central"), ("github", "publish-github"), ("attest", "attest-publication"))
OUTPUT = re.compile(r"assets_complete: \$\{\{ steps\.([\w-]+)\.outputs\.complete \}\}")


def uses(text, action):
    return re.search(r"uses: [^\s@]+/java/" + re.escape(action) + r"@", text) is not None


def check(jobs, channels=("snapshot", "release")):
    problems = []

    def need(condition, message):
        if not condition:
            problems.append(message)

    def checkouts_drop_credentials(name):
        for step in jobs[name].steps():
            if any("uses: actions/checkout@" in line for line in step):
                need(any("persist-credentials: false" in line for line in step),
                     f"{name}: the checkout must set persist-credentials: false")

    for channel in channels:
        gate = f"publish-{channel}"
        if gate not in jobs:
            problems.append(f"no job {gate}")
            continue
        prep = jobs[gate].text
        need(uses(prep, "finalize-publication"), f"{gate} does not end in java/finalize-publication")
        need(f"channel: {channel}" in prep, f"{gate} does not pass channel: {channel}")
        output = OUTPUT.search(prep)
        need(output is not None, f"{gate} has no output assets_complete: ${{{{ steps.<id>.outputs.complete }}}}")
        if output:
            need(f"id: {output.group(1)}" in prep, f"{gate}: no step with id {output.group(1)}")
        for permission in ("id-token: write", "attestations: write"):
            need(permission not in prep, f"{gate} must not have {permission} (the attestation job has it)")
        ref_check = REF_CHECKS.get(channel)
        if ref_check:
            need(ref_check in jobs and uses(jobs[ref_check].text, "check-release-ref")
                 and f"channel: {channel}" in jobs[ref_check].text,
                 f"{ref_check} does not run java/check-release-ref with channel: {channel}")
            if ref_check in jobs:
                checkouts_drop_credentials(ref_check)
        for prefix, action in CONSUMERS:
            name = f"{prefix}-{channel}"
            if name not in jobs:
                problems.append(f"no job {name}")
                continue
            job = jobs[name]
            need(job.needs == [gate], f"{name} must need {gate} alone, not {job.needs}")
            need(f"needs.{gate}.outputs.assets_complete == 'true'" in job.text,
                 f"{name} must run only on needs.{gate}.outputs.assets_complete == 'true'")
            need(uses(job.text, action) and f"channel: {channel}" in job.text,
                 f"{name} does not run java/{action} with channel: {channel}")
            checkouts_drop_credentials(name)
        attest = jobs.get(f"attest-{channel}")
        if attest:
            for permission in ("id-token: write", "attestations: write"):
                need(permission in attest.text, f"attest-{channel} needs {permission}")
        github = jobs.get(f"github-{channel}")
        if github:
            need("contents: write" in github.text, f"github-{channel} needs contents: write")
    if "snapshot" in channels:
        central, github = jobs.get("central-snapshot"), jobs.get("github-snapshot")
        if central:
            need("group: central-snapshot-${{ github.repository }}" in central.text
                 and "cancel-in-progress: false" in central.text,
                 "central-snapshot must run in concurrency group central-snapshot-${{ github.repository }} "
                 "without cancel-in-progress")
            need("actions: read" in central.text, "central-snapshot needs actions: read (superseded runs)")
        if github:
            need("group: snapshot-release-${{ github.repository }}" in github.text
                 and "cancel-in-progress: false" in github.text,
                 "github-snapshot must run in concurrency group snapshot-release-${{ github.repository }} "
                 "without cancel-in-progress")
    return problems


def main(argv=None, out=sys.stdout, err=sys.stderr):
    parser = argparse.ArgumentParser(prog="python3 -m actionlib.java.wiring",
                                     description="Check the publication jobs of the publish workflow.")
    parser.add_argument("--root", default=".", help="the repository (default: the current directory)")
    parser.add_argument("--config", help=f"configuration file (default: {configuration.DEFAULT_PATH})")
    args = parser.parse_args(argv)
    try:
        settings = configuration.section(configuration.load(args.root, args.config), "publication-wiring", DEFAULTS)
        channels = configuration.strings(settings["channels"], "publication-wiring.channels")
        text = configuration.inside(args.root, settings["workflow"]).read_text(encoding="utf-8")
        problems = check(workflow.parse(text), channels)
    except (ValueError, OSError) as error:
        print(f"::error::{error}", file=err)
        return 1
    for problem in problems:
        print(f"::error::{settings['workflow']}: {problem}", file=err)
    print(f"publication wiring of {', '.join(channels)}: {len(problems)} problems", file=out)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
