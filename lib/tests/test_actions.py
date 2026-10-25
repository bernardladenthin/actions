# SPDX-FileCopyrightText: 2026 Bernard Ladenthin <bernard.ladenthin@gmail.com>
#
# SPDX-License-Identifier: MIT OR Apache-2.0
"""The composite actions and reusable workflows of this repository are self-contained and pinned.

A consumer runs an action of this repository at a pinned commit, with nothing of it on disk but
that commit: every script and module an action names has to exist next to it, every input it reads
has to be declared, and a `uses: ./...` inside it would resolve against the CALLER's checkout.
"""

import re
import unittest
from pathlib import Path

from tests.helpers import REPO

ROOT = Path(REPO)
ACTIONS = sorted(ROOT.glob("common/*/action.yml")) + sorted(ROOT.glob("java/*/action.yml"))
WORKFLOWS = sorted(ROOT.glob(".github/workflows/*.yml"))
TEMPLATE_WORKFLOWS = sorted(ROOT.glob("templates/*/.github/workflows/*.yml"))
MODULE = re.compile(r"python3 -m (actionlib(?:\.[a-z_]+)+)")
SCRIPT = re.compile(r'"\$GITHUB_ACTION_PATH/([^"]+)"')
INPUT = re.compile(r"\$\{\{\s*inputs\.([A-Za-z0-9_-]+)")
USES = re.compile(r"(?m)^\s*(?:-\s+)?uses:\s*[\"']?([^\s\"'#]+)")
PINNED = re.compile(r"@[0-9a-f]{40}$")


def declared_inputs(text):
    block = re.search(r"^inputs:\n((?:  .*\n|\n)*)", text, re.M)
    return set(re.findall(r"^  ([A-Za-z0-9_-]+):", block.group(1), re.M)) if block else set()


class ActionsTest(unittest.TestCase):

    def test_there_are_actions_in_both_families(self):
        families = {path.parent.parent.name for path in ACTIONS}
        self.assertEqual(families, {"common", "java"})

    def test_every_action_is_composite_described_and_self_contained(self):
        for path in ACTIONS:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path.relative_to(ROOT).as_posix()):
                self.assertRegex(text, r"(?m)^name: \S")
                self.assertRegex(text, r"(?m)^description: \S")
                self.assertIn("using: composite", text)
                for module in MODULE.findall(text):
                    self.assertTrue((ROOT / "lib" / (module.replace(".", "/") + ".py")).is_file(), module)
                for script in SCRIPT.findall(text):
                    self.assertTrue((path.parent / script).exists(), script)
                self.assertLessEqual(set(INPUT.findall(text)), declared_inputs(text))
                for target in USES.findall(text):
                    self.assertFalse(target.startswith("./"), f"{target}: a local action resolves in the caller")

    def test_every_third_party_action_is_pinned_to_a_commit(self):
        for path in ACTIONS + WORKFLOWS:
            for target in USES.findall(path.read_text(encoding="utf-8")):
                if target.startswith(("./", "bernardladenthin/actions/")):
                    continue
                with self.subTest(f"{path.relative_to(ROOT).as_posix()}: {target}"):
                    self.assertRegex(target, PINNED)

    def test_every_reusable_workflow_is_callable(self):
        for path in WORKFLOWS:
            if path.name == "ci.yml":
                continue
            with self.subTest(path.name):
                self.assertIn("workflow_call:", path.read_text(encoding="utf-8"))

    def test_every_workflow_template_calls_a_reusable_workflow_that_exists(self):
        for path in TEMPLATE_WORKFLOWS:
            for target in USES.findall(path.read_text(encoding="utf-8")):
                if target.startswith("bernardladenthin/actions/"):
                    with self.subTest(f"{path.name}: {target}"):
                        local = target[len("bernardladenthin/actions/"):].split("@")[0]
                        self.assertTrue((ROOT / local).is_file(), local)

    def test_the_readme_lists_every_action_and_reusable_workflow(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for path in ACTIONS:
            with self.subTest(path.parent.name):
                self.assertIn(f"`{path.parent.parent.name}/{path.parent.name}`", readme)
        for path in WORKFLOWS:
            if path.name != "ci.yml":
                with self.subTest(path.name):
                    self.assertIn(f"`{path.name}`", readme)


if __name__ == "__main__":
    unittest.main()
