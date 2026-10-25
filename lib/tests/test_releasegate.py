# SPDX-FileCopyrightText: 2026 Bernard Ladenthin <bernard.ladenthin@gmail.com>
#
# SPDX-License-Identifier: MIT OR Apache-2.0

import io
import json
import tempfile
import unittest
from pathlib import Path

from actionlib.common import releasegate, workflow
from tests.helpers import workflow_text

GATES = ("pub-a", "pub-b")


def jobs(*extra):
    return workflow.parse(workflow_text([("test", None, []), ("build", "test", []), *extra,
                                         ("pub-a", "[build]", []), ("pub-b", "[build]", [])]))


class ReleaseGateTest(unittest.TestCase):

    def check(self, js, non_gating):
        return releasegate.check(js, dict.fromkeys(non_gating, "reason"), GATES)

    def test_transitively_gating_jobs_pass(self):
        self.assertEqual(self.check(jobs(), ["pub-a", "pub-b"]), [])

    def test_a_job_nothing_waits_for_fails(self):
        self.assertEqual(self.check(jobs(("lint", None, [])), ["pub-a", "pub-b"]),
                         ["publish.yml: pub-a, pub-b does not wait for lint -- add it to the needs, or to "
                          "release-gate-exemptions.txt with the reason it may stay red"])

    def test_gating_only_one_publish_job_fails(self):
        js = workflow.parse(workflow_text([("lint", None, []), ("pub-a", "[lint]", []), ("pub-b", None, [])]))
        self.assertEqual(len(self.check(js, ["pub-a", "pub-b"])), 1)

    def test_a_listed_job_may_stay_outside(self):
        self.assertEqual(self.check(jobs(("lint", None, [])), ["pub-a", "pub-b", "lint"]), [])

    def test_a_stale_exemption_fails(self):
        self.assertIn("remove the stale exemption", self.check(jobs(), ["pub-a", "pub-b", "build"])[0])
        self.assertEqual(self.check(jobs(), ["pub-a", "pub-b", "gone"]),
                         ["release-gate-exemptions.txt lists gone, which is no job of publish.yml"])

    def test_reads_the_exemptions_file(self):
        self.assertEqual(releasegate.read_exemptions("# c\n\na: why: because\n b :x\n"),
                         {"a": "why: because", "b": "x"})

    def test_an_exemption_without_a_reason_is_an_error(self):
        for text in ("a\n", "a:\n", "a:  \n", ": x\n", "a: x\na: y\n"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                releasegate.read_exemptions(text)

    def test_main_reads_the_configured_workflow_gates_and_exemptions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "ci").mkdir()
            (root / "ci/release.yml").write_text(workflow_text(
                [("test", None, []), ("lint", None, []), ("pub-a", "[test]", []), ("pub-b", "[test]", [])]))
            (root / "ci/why.txt").write_text("lint: informational only\npub-a: a gate\npub-b: a gate\n")
            (root / ".github").mkdir()
            (root / ".github/actions-config.json").write_text(json.dumps({"release-gate": {
                "workflow": "ci/release.yml", "gates": ["pub-a", "pub-b"], "exemptions": "ci/why.txt"}}))
            out, err = io.StringIO(), io.StringIO()
            self.assertEqual(releasegate.main(["--root", temp], out, err), 0, err.getvalue())
            self.assertIn("4 jobs, 3 exempt, 0 violations", out.getvalue())
            (root / "ci/why.txt").write_text("pub-a: a gate\npub-b: a gate\n")
            err = io.StringIO()
            self.assertEqual(releasegate.main(["--root", temp], io.StringIO(), err), 1)
            self.assertIn("release.yml: pub-a, pub-b does not wait for lint -- add it to the needs, or to why.txt",
                          err.getvalue())

    def test_main_defaults_and_unknown_keys(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".github/workflows").mkdir(parents=True)
            (root / ".github/workflows/publish.yml").write_text(workflow_text(
                [("test", None, []), ("publish-snapshot", "[test]", []), ("publish-release", "[test]", [])]))
            (root / ".github/release-gate-exemptions.txt").write_text(
                "publish-snapshot: a gate\npublish-release: a gate\n")
            self.assertEqual(releasegate.main(["--root", temp], io.StringIO(), io.StringIO()), 0)
            (root / ".github/actions-config.json").write_text(json.dumps({"release-gate": {"gate": ["x"]}}))
            err = io.StringIO()
            self.assertEqual(releasegate.main(["--root", temp], io.StringIO(), err), 1)
            self.assertIn("release-gate: unknown key(s) gate", err.getvalue())


if __name__ == "__main__":
    unittest.main()
