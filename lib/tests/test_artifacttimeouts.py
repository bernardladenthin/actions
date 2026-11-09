# SPDX-FileCopyrightText: 2026 Bernard Ladenthin <bernard.ladenthin@gmail.com>
#
# SPDX-License-Identifier: MIT OR Apache-2.0

import io
import json
import os
import shutil
import tempfile
import unittest

from actionlib.common import artifacttimeouts
from tests.helpers import REPO, upload, workflow_text

ENV = ["env:", "  ARTIFACT_TIMEOUT_MINUTES: ${{ vars.ARTIFACT_TIMEOUT_MINUTES || '20' }}"]
STEP = "        timeout-minutes: ${{ fromJSON(env.ARTIFACT_TIMEOUT_MINUTES) }}"


def with_env(text, env=ENV):
    head, rest = text.split("\n", 1)
    return "\n".join([head] + env) + "\n" + rest


def failures(text, max_minutes=artifacttimeouts.MAX_MINUTES):
    return artifacttimeouts.check("w.yml", text, max_minutes)[0]


class StepTest(unittest.TestCase):

    def test_a_step_with_the_variable_passes_before_or_after_uses(self):
        text = with_env(workflow_text([("a", None, upload("x") + [STEP, "      - name: d", STEP,
                                                                  "        uses: actions/download-artifact@v8"])]))
        self.assertEqual(failures(text), [])
        self.assertEqual(artifacttimeouts.check("w.yml", text)[1], 2)

    def test_a_step_without_a_timeout_fails_naming_its_line(self):
        text = with_env(workflow_text([("a", None, upload("x") + ["      - run: echo"])]))
        self.assertEqual(len(failures(text)), 1)
        self.assertIn("w.yml:10: the upload-artifact step has no timeout-minutes", failures(text)[0])

    def test_a_literal_timeout_fails(self):
        text = with_env(workflow_text([("a", None, upload("x") + ["        timeout-minutes: 10"])]))
        self.assertIn("has timeout-minutes 10; expected", failures(text)[0])

    def test_the_spaces_inside_the_expression_do_not_matter(self):
        text = with_env(workflow_text([("a", None, upload("x") + [
            "        timeout-minutes: ${{fromJSON(env.ARTIFACT_TIMEOUT_MINUTES)}}  # the variable"])]))
        self.assertEqual(failures(text), [])

    def test_the_timeout_of_the_next_step_does_not_count(self):
        text = with_env(workflow_text([("a", None, upload("x") + ["      - run: echo", STEP])]))
        self.assertEqual(len(failures(text)), 1)

    def test_a_timeout_inside_with_does_not_count(self):
        text = with_env(workflow_text([("a", None, upload("x") + ["          timeout-minutes: 5"])]))
        self.assertIn("has no timeout-minutes", failures(text)[0])

    def test_a_quoted_uses_and_a_step_of_a_later_job_are_found(self):
        text = with_env(workflow_text([("a", None, ["      - run: echo"]),
                                       ("b", "a", ["      - uses: 'actions/download-artifact@v8'", STEP])]))
        self.assertEqual(artifacttimeouts.check("w.yml", text), ([], 1))

    def test_another_action_is_not_an_artifact_step(self):
        text = workflow_text([("a", None, ["      - uses: actions/checkout@v6",
                                           "      - uses: someone/upload-artifact-plus@v1"])])
        self.assertEqual(artifacttimeouts.check("w.yml", text), ([], 0))


class EnvTest(unittest.TestCase):

    def test_a_workflow_without_artifact_steps_needs_no_env(self):
        self.assertEqual(failures(workflow_text([("a", None, ["      - run: echo"])])), [])

    def test_a_workflow_with_artifact_steps_needs_the_env(self):
        text = workflow_text([("a", None, upload("x") + [STEP])])
        self.assertEqual(len(failures(text)), 1)
        self.assertIn("no top-level env ARTIFACT_TIMEOUT_MINUTES", failures(text)[0])

    def test_a_job_env_is_not_the_workflow_env(self):
        text = workflow_text([("a", None, upload("x") + [STEP])]).replace(
            "    runs-on:", "    env:\n      ARTIFACT_TIMEOUT_MINUTES: ${{ vars.ARTIFACT_TIMEOUT_MINUTES || '20' }}\n"
            "    runs-on:")
        self.assertIn("no top-level env", failures(text)[0])

    def test_the_env_must_read_the_variable_with_a_default(self):
        for value in ["20", "${{ vars.ARTIFACT_TIMEOUT_MINUTES }}", "${{ vars.OTHER || '20' }}"]:
            text = with_env(workflow_text([("a", None, upload("x") + [STEP])]),
                            ["env:", f"  ARTIFACT_TIMEOUT_MINUTES: {value}"])
            self.assertIn("expected ${{ vars.ARTIFACT_TIMEOUT_MINUTES || '<minutes>' }}", failures(text)[0], value)

    def test_the_default_must_be_whole_minutes_within_the_limit(self):
        for default, ok in [("1", True), ("20", True), ("30", True), ("31", False), ("0", False), ("x", False),
                            ("2.5", False)]:
            text = with_env(workflow_text([("a", None, upload("x") + [STEP])]),
                            ["env:", "  OTHER: 1", f"  ARTIFACT_TIMEOUT_MINUTES: \"${{{{ vars.ARTIFACT_TIMEOUT_MINUTES"
                                                   f" || '{default}' }}}}\""])
            self.assertEqual(failures(text) == [], ok, default)

    def test_the_limit_is_configurable(self):
        text = with_env(workflow_text([("a", None, upload("x") + [STEP])]))
        self.assertIn("1 to 10 minutes", failures(text, max_minutes=10)[0])


class MainTest(unittest.TestCase):

    def run_main(self, root, *args):
        out, err = io.StringIO(), io.StringIO()
        code = artifacttimeouts.main(["--root", root, *args], out, err)
        return code, out.getvalue(), err.getvalue()

    def repository(self, files, config=None):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        for name, text in files.items():
            path = os.path.join(root, ".github", "workflows", name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        os.makedirs(os.path.join(root, ".github"), exist_ok=True)
        if config is not None:
            with open(os.path.join(root, ".github", "actions-config.json"), "w", encoding="utf-8") as f:
                json.dump(config, f)
        return root

    def test_counts_the_steps_and_fails_on_a_problem(self):
        good = with_env(workflow_text([("a", None, upload("x") + [STEP])]))
        bad = workflow_text([("a", None, upload("x"))])
        root = self.repository({"good.yml": good, "bad.yaml": bad})
        code, out, err = self.run_main(root)
        self.assertEqual(code, 1)
        self.assertIn("2 artifact transfer steps in 2 workflows, 2 problems", out)
        self.assertIn(".github/workflows/bad.yaml", err)

    def test_scorecard_is_exempt_by_default_and_exempt_is_configurable(self):
        bad = workflow_text([("a", None, upload("x"))])
        self.assertEqual(self.run_main(self.repository({"scorecard.yml": bad}))[0], 0)
        root = self.repository({"x.yml": bad}, {"artifact-timeouts": {"exempt": {"x.yml": "a reason"}}})
        self.assertEqual(self.run_main(root)[0], 0)

    def test_a_bad_configuration_is_exit_code_2(self):
        for section in [{"exempt": {"x.yml": ""}}, {"max-minutes": 0}, {"max-minutes": True}, {"patterns": "x"}]:
            root = self.repository({}, {"artifact-timeouts": section})
            self.assertEqual(self.run_main(root)[0], 2, section)

    def test_this_repository_passes(self):
        self.assertEqual(self.run_main(REPO)[0], 0)


if __name__ == "__main__":
    unittest.main()
