# SPDX-FileCopyrightText: 2026 Bernard Ladenthin <bernard.ladenthin@gmail.com>
#
# SPDX-License-Identifier: MIT OR Apache-2.0

import io
import json
import os
import shutil
import tempfile
import unittest

from actionlib.common import dependabot
from tests.helpers import REPO

GOOD = """version: 2
updates:
  - package-ecosystem: "maven"
    directory: "/"
    schedule:
      interval: "weekly"
      day: "saturday"
    # a comment, and one after a value
    ignore:
      - dependency-name: "net.jqwik:*"   # pinned
    groups:
      maven:
        patterns: ["*"]
        update-types: ["minor", "patch"]
  - package-ecosystem: github-actions
    directories:
      - "/"
      - "/common/*"
    schedule:
      interval: weekly
      day: saturday
    groups:
      github-actions:
        patterns: ['*']
"""


class ParseTest(unittest.TestCase):

    def test_reads_the_block_style_of_these_files(self):
        document = dependabot.parse(GOOD)
        self.assertEqual(document["version"], "2")
        maven, actions = document["updates"]
        self.assertEqual(maven["schedule"], {"interval": "weekly", "day": "saturday"})
        self.assertEqual(maven["ignore"], [{"dependency-name": "net.jqwik:*"}])
        self.assertEqual(maven["groups"]["maven"], {"patterns": ["*"], "update-types": ["minor", "patch"]})
        self.assertEqual(actions["directories"], ["/", "/common/*"])
        self.assertEqual(actions["groups"]["github-actions"]["patterns"], ["*"])

    def test_a_sequence_at_the_indentation_of_its_key(self):
        self.assertEqual(dependabot.parse("a:\n- 1\n- 2\nb: x\n"), {"a": ["1", "2"], "b": "x"})

    def test_a_quoted_colon_and_hash_are_text(self):
        self.assertEqual(dependabot.parse('a: "x: y # z"\nb: [\'p,q\', r]\n'), {"a": "x: y # z", "b": ["p,q", "r"]})

    def test_what_it_does_not_read_fails_loudly(self):
        for text in ["a: |\n  x\n", "a: {b: c}\n", "a: &x 1\n", "a: *x\n", "a: !tag x\n", "a: 'open\n",
                     "a: 1\na: 2\n", "a:\n  b: 1\n   c: 2\n", "\ta: 1\n", "a: 1\n- b\n", "just text\n"]:
            with self.assertRaises(dependabot.YamlError, msg=text):
                dependabot.parse(text)


class CheckTest(unittest.TestCase):

    def test_a_weekly_saturday_grouped_file_passes(self):
        self.assertEqual(dependabot.check(dependabot.parse(GOOD)), ([], 2))

    def test_every_entry_needs_the_schedule_and_a_group(self):
        document = dependabot.parse(GOOD.replace('      day: "saturday"\n', "").replace(
            "      interval: weekly\n", "      interval: daily\n"))
        del document["updates"][1]["groups"]
        failures, count = dependabot.check(document)
        self.assertEqual(count, 2)
        self.assertEqual(len(failures), 3)
        self.assertIn("updates[0] (maven): schedule.day is None; expected 'saturday'", failures[0])
        self.assertIn("updates[1] (github-actions): schedule.interval is 'daily'", failures[1])
        self.assertIn("updates[1] (github-actions) has no groups", failures[2])

    def test_an_empty_group_list_is_no_group_and_a_multi_ecosystem_group_is_one(self):
        document = dependabot.parse(GOOD)
        document["updates"][0]["groups"] = {}
        document["updates"][1].pop("groups")
        document["updates"][1]["multi-ecosystem-group"] = "all"
        failures, _ = dependabot.check(document)
        self.assertEqual(len(failures), 1)
        self.assertIn("updates[0] (maven) has no groups", failures[0])

    def test_the_rule_is_configurable(self):
        document = dependabot.parse(GOOD.replace("saturday", "monday"))
        self.assertEqual(dependabot.check(document, day="monday"), ([], 2))
        self.assertEqual(dependabot.check(document, day=None), ([], 2))
        del document["updates"][0]["groups"]
        self.assertEqual(dependabot.check(document, day=None, groups=False), ([], 2))

    def test_a_file_without_updates_fails(self):
        for document in [None, {"version": "2"}, {"updates": []}, ["x"]]:
            self.assertEqual(dependabot.check(document), (["has no `updates` entries"], 0))


class MainTest(unittest.TestCase):

    def repository(self, text=None, config=None, name="dependabot.yml"):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        os.makedirs(os.path.join(root, ".github"))
        if text is not None:
            with open(os.path.join(root, ".github", name), "w", encoding="utf-8") as f:
                f.write(text)
        if config is not None:
            with open(os.path.join(root, ".github", "actions-config.json"), "w", encoding="utf-8") as f:
                json.dump(config, f)
        return root

    def run_main(self, root):
        out, err = io.StringIO(), io.StringIO()
        return dependabot.main(["--root", root], out, err), out.getvalue(), err.getvalue()

    def test_passes_and_counts(self):
        code, out, _ = self.run_main(self.repository(GOOD))
        self.assertEqual((code, out.strip()), (0, ".github/dependabot.yml: 2 update entries, 0 problems"))

    def test_the_yaml_extension_is_read_too(self):
        self.assertEqual(self.run_main(self.repository(GOOD, name="dependabot.yaml"))[0], 0)

    def test_fails_on_a_problem_a_missing_file_and_an_unreadable_one(self):
        code, _, err = self.run_main(self.repository(GOOD.replace("saturday", "sunday")))
        self.assertEqual(code, 1)
        self.assertIn("expected 'saturday'", err)
        code, _, err = self.run_main(self.repository())
        self.assertEqual(code, 1)
        self.assertIn("no Dependabot configuration", err)
        code, _, err = self.run_main(self.repository("updates: {a: b}\n"))
        self.assertEqual(code, 1)
        self.assertIn("line 1: cannot read", err)

    def test_the_configuration_sets_the_rule_and_a_bad_one_is_exit_code_2(self):
        root = self.repository(GOOD.replace("saturday", "monday"), {"dependabot": {"day": "monday"}})
        self.assertEqual(self.run_main(root)[0], 0)
        for section in [{"day": ""}, {"interval": 7}, {"groups": "yes"}, {"unknown": 1}]:
            self.assertEqual(self.run_main(self.repository(GOOD, {"dependabot": section}))[0], 2, section)

    def test_this_repository_and_the_java_template_pass(self):
        self.assertEqual(self.run_main(REPO)[0], 0)
        with open(os.path.join(REPO, "templates", "java", ".github", "dependabot.yml"), encoding="utf-8") as f:
            self.assertEqual(dependabot.check(dependabot.parse(f.read())), ([], 2))


if __name__ == "__main__":
    unittest.main()
