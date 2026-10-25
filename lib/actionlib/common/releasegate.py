# SPDX-FileCopyrightText: 2026 Bernard Ladenthin <bernard.ladenthin@gmail.com>
#
# SPDX-License-Identifier: MIT OR Apache-2.0
"""Every job of the publish workflow gates every publish job, unless the repository lists it in its
exemptions file with a reason.

Configured in the ``release-gate`` section of ``.github/actions-config.json``: ``workflow`` (default
``.github/workflows/publish.yml``), ``gates`` (the publish jobs, default ``publish-snapshot`` and
``publish-release``) and ``exemptions`` (default ``.github/release-gate-exemptions.txt``).

A job that nothing waits for can go red and a release still ships -- the natives-build jobs that
`package` once forgot to wait for, and the aarch64 fat jars that were signed and attached for
releases without any job launching them, were both of that shape. So "not gating" has to be a
decision written down here, not the default a new job gets by being forgotten in two `needs:` lists.

The check runs both ways: a job outside the gates and outside the exemptions fails, and so does an
exemption that names no job or a job that gates both publish jobs after all (a stale
exemption would hide the next job of that name).
"""

import argparse
import sys
from pathlib import Path

from . import config as configuration
from . import workflow

GATES = ("publish-snapshot", "publish-release")

# Each repository lists its own exemptions there, one `<job>: <reason>` per line (# comments).
EXEMPTIONS_FILE = ".github/release-gate-exemptions.txt"
DEFAULTS = {"workflow": ".github/workflows/publish.yml", "gates": list(GATES), "exemptions": EXEMPTIONS_FILE}


def read_exemptions(text):
    """The exemptions file: job -> reason. A line without a reason is an error -- the point of the
    file is that every job allowed to stay red says why."""
    exemptions = {}
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        job, _, reason = line.partition(":")
        if not reason.strip() or not job.strip():
            raise ValueError(f"line {number}: expected `<job>: <reason>`, got {line!r}")
        if job.strip() in exemptions:
            raise ValueError(f"line {number}: {job.strip()} is listed twice")
        exemptions[job.strip()] = reason.strip()
    return exemptions


def check(jobs, non_gating, gates=GATES, workflow_name="publish.yml", exemptions_name="release-gate-exemptions.txt"):
    missing = [g for g in gates if g not in jobs]
    if missing:
        return [f"{workflow_name} has no job {g}" for g in missing]
    closures = [workflow.closure(jobs, g) for g in gates]
    failures = []
    for name in jobs:
        gated = [g for g, c in zip(gates, closures) if name in c]
        if name in non_gating:
            if len(gated) == len(gates):
                failures.append(f"{exemptions_name} lists {name}, which gates {', '.join(gates)} "
                                f"-- remove the stale exemption")
        elif len(gated) != len(gates):
            ungated = [g for g in gates if g not in gated]
            failures.append(f"{workflow_name}: {', '.join(ungated)} does not wait for {name} -- add it to the "
                            f"needs, or to {exemptions_name} with the reason it may stay red")
    failures += [f"{exemptions_name} lists {name}, which is no job of {workflow_name}"
                 for name in sorted(set(non_gating) - set(jobs))]
    return failures


def main(argv=None, out=sys.stdout, err=sys.stderr):
    parser = argparse.ArgumentParser(prog="python3 -m actionlib.common.releasegate",
                                     description="Fail when a job gates no publish job without a written reason.")
    parser.add_argument("--root", default=".", help="the repository (default: the current directory)")
    parser.add_argument("--config", help=f"configuration file (default: {configuration.DEFAULT_PATH})")
    args = parser.parse_args(argv)
    try:
        settings = configuration.section(configuration.load(args.root, args.config), "release-gate", DEFAULTS)
        gates = tuple(configuration.strings(settings["gates"], "release-gate.gates"))
        jobs = workflow.parse(configuration.inside(args.root, settings["workflow"]).read_text(encoding="utf-8"))
        exemptions_path = configuration.inside(args.root, settings["exemptions"])
        exemptions = read_exemptions(exemptions_path.read_text(encoding="utf-8")) if exemptions_path.is_file() else {}
    except (ValueError, OSError) as error:
        print(f"::error::{error}", file=err)
        return 1
    failures = check(jobs, exemptions, gates, Path(settings["workflow"]).name, Path(settings["exemptions"]).name)
    for failure in failures:
        print(f"::error::{failure}", file=err)
    print(f"{len(jobs)} jobs, {len(exemptions)} exempt, {len(failures)} violations", file=out)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
