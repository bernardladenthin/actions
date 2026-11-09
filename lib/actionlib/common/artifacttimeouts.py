# SPDX-FileCopyrightText: 2026 Bernard Ladenthin <bernard.ladenthin@gmail.com>
#
# SPDX-License-Identifier: MIT OR Apache-2.0
"""Every artifact transfer step of the workflows carries a timeout that a repository variable sets.

An `actions/upload-artifact` step of java-llama.cpp's aarch64 Vulkan build hung for hours in a
Publish run (42675444364) after a build of a minute and a half. No step set `timeout-minutes`, so
GitHub's 360 minutes were the only limit, and every job behind it waited until then. Measured over
two complete runs of that pipeline, the longest artifact transfer took 18 s. A job-level limit would
have to cover the longest build of a job (hours for a cold native build) and would not have caught
the hang sooner, so the limit sits on the transfer step.

The value is one per repository and changeable without a commit: a workflow that transfers artifacts
defines, in its top-level `env:`,

    ARTIFACT_TIMEOUT_MINUTES: ${{ vars.ARTIFACT_TIMEOUT_MINUTES || '20' }}

(the repository variable of that name if it is set, else the default written there, at most
``max-minutes``), and every `actions/upload-artifact` / `actions/download-artifact` step says

    timeout-minutes: ${{ fromJSON(env.ARTIFACT_TIMEOUT_MINUTES) }}

`fromJSON` turns the text into the number `timeout-minutes` needs; a variable that is not a number
fails the run. The value of the variable lives in the repository's settings and is not seen here.

The files are the ``patterns`` of the ``artifact-timeouts`` section of ``.github/actions-config.json``
(default: the workflows); ``exempt`` names files left out, each with its reason (default: the
``scorecard.yml`` template, whose workflow scorecard-action verifies: no top-level `env:`). Composite
actions are not checked: their steps cannot carry `timeout-minutes` at all; the step that calls one can.

Like the other checks this reads the two-space layout the workflows are written in, not general YAML:
a step is a list item whose `uses:` names one of the two actions, and its keys are the lines at the
indentation of that `uses:` until the next item or a dedent.
"""

import argparse
import glob
import os
import re
import sys

from . import config as configuration

VARIABLE = "ARTIFACT_TIMEOUT_MINUTES"
MAX_MINUTES = 30
PATTERNS = (".github/workflows/*.yml", ".github/workflows/*.yaml")
EXEMPT = {"scorecard.yml": "the template scorecard.yml: scorecard-action refuses a workflow with a top-level env"}
STEP_TIMEOUT = "${{ fromJSON(env.%s) }}" % VARIABLE
ARTIFACT_USES = re.compile(r"^( *)(- +)?uses: *[\"']?actions/(upload|download)-artifact@")
ITEM = re.compile(r"^( *)- +")
TIMEOUT = re.compile(r"^timeout-minutes:\s*(.*?)\s*(?:#.*)?$")
TOP_LEVEL_KEY = re.compile(r"^[A-Za-z_][\w-]*:")
ENV_DEFAULT = re.compile(r"^\"?\$\{\{\s*vars\.%s\s*\|\|\s*'([^']*)'\s*\}\}\"?$" % VARIABLE)
SPACES = re.compile(r"\s+")


def _indent(line):
    return len(line) - len(line.lstrip(" "))


def _expression(text):
    """`text` with the whitespace inside it collapsed, so the spelling of spaces does not matter."""
    return SPACES.sub(" ", text.strip().strip('"')).replace("${{ ", "${{").replace(" }}", "}}")


def artifact_steps(text):
    """Yield (line number, upload|download, key lines) for every artifact transfer step."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = ARTIFACT_USES.match(line)
        if not m:
            continue
        key_indent = len(m.group(1)) + (len(m.group(2)) if m.group(2) else 0)
        start = i
        if not m.group(2):
            # the item line of this step: a "- " whose key column is the column of this uses:
            start = next((j for j in range(i - 1, -1, -1)
                          if ITEM.match(lines[j]) and len(ITEM.match(lines[j]).group(0)) == key_indent), i)
        keys = []
        for j in range(start, len(lines)):
            raw = lines[j]
            if not raw.strip() or raw.lstrip().startswith("#"):
                continue
            if j > start and _indent(raw) < key_indent:
                break  # the next item of the list, or the end of the steps
            if j == start:
                keys.append(ITEM.sub("", raw, count=1).strip() if ITEM.match(raw) else raw.strip())
            elif _indent(raw) == key_indent:
                keys.append(raw.strip())
        yield i + 1, m.group(3), keys


def env_default(text):
    """(line number, value) of the variable in the workflow's top-level env, else None."""
    lines = text.splitlines()
    inside = False
    for i, line in enumerate(lines):
        if TOP_LEVEL_KEY.match(line):
            inside = line.split(":", 1)[0] == "env"
            continue
        if inside and line.startswith("  ") and not line.startswith("   "):
            key, _, value = line.strip().partition(":")
            if key == VARIABLE:
                return i + 1, value.strip()
    return None


def check(name, text, max_minutes=MAX_MINUTES):
    """The failures of one workflow, and the number of transfer steps it has."""
    failures = []
    steps = list(artifact_steps(text))
    if not steps:
        return failures, 0
    env = env_default(text)
    if env is None:
        failures.append(f"{name}: has artifact steps but no top-level env {VARIABLE}: "
                        f"${{{{ vars.{VARIABLE} || '<minutes>' }}}}")
    else:
        line, value = env
        m = ENV_DEFAULT.match(value)
        if not m:
            failures.append(f"{name}:{line}: env {VARIABLE} is {value}; expected "
                            f"${{{{ vars.{VARIABLE} || '<minutes>' }}}}")
        elif not m.group(1).isdigit() or not 1 <= int(m.group(1)) <= max_minutes:
            failures.append(f"{name}:{line}: the default of {VARIABLE} is '{m.group(1)}'; "
                            f"it must be 1 to {max_minutes} minutes")
    for line, kind, keys in steps:
        values = [TIMEOUT.match(k).group(1) for k in keys if TIMEOUT.match(k)]
        where = f"{name}:{line}: the {kind}-artifact step"
        if not values:
            failures.append(f"{where} has no timeout-minutes; a hung transfer holds the job for GitHub's "
                            f"360 minutes (expected {STEP_TIMEOUT})")
        elif _expression(values[0]) != _expression(STEP_TIMEOUT):
            failures.append(f"{where} has timeout-minutes {values[0]}; expected {STEP_TIMEOUT}")
    return failures, len(steps)


def files(root, patterns):
    found = set()
    for pattern in patterns:
        found.update(p for p in glob.glob(os.path.join(root, pattern)) if os.path.isfile(p))
    return sorted(found)


def main(argv=None, out=sys.stdout, err=sys.stderr):
    parser = argparse.ArgumentParser(prog="python3 -m actionlib.common.artifacttimeouts",
                                     description="every artifact transfer step has the variable's timeout")
    parser.add_argument("--root", default=".", help="the repository (default: the current directory)")
    parser.add_argument("--config", help=f"configuration file (default: {configuration.DEFAULT_PATH})")
    args = parser.parse_args(argv)
    root = args.root
    try:
        settings = configuration.section(configuration.load(root, args.config), "artifact-timeouts",
                                         {"patterns": list(PATTERNS), "exempt": dict(EXEMPT),
                                          "max-minutes": MAX_MINUTES})
        patterns = configuration.strings(settings["patterns"], "artifact-timeouts.patterns")
        exempt = settings["exempt"]
        if not isinstance(exempt, dict) or any(not isinstance(v, str) or not v.strip() for v in exempt.values()):
            raise ValueError("artifact-timeouts.exempt: expected an object of file name -> reason")
        max_minutes = settings["max-minutes"]
        if not isinstance(max_minutes, int) or isinstance(max_minutes, bool) or max_minutes < 1:
            raise ValueError("artifact-timeouts.max-minutes: expected a positive integer")
    except (ValueError, OSError) as error:
        print(f"::error::{error}", file=err)
        return 2
    failures, steps, checked = [], 0, 0
    for path in files(root, patterns):
        if os.path.basename(path) in exempt:
            continue
        with open(path, encoding="utf-8") as f:
            found, count = check(os.path.relpath(path, root), f.read(), max_minutes)
        failures += found
        steps += count
        checked += 1
    for failure in failures:
        print(f"::error::{failure}", file=err)
    print(f"{steps} artifact transfer steps in {checked} workflows, {len(failures)} problems", file=out)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
