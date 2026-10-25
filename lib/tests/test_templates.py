# SPDX-FileCopyrightText: 2026 Bernard Ladenthin <bernard.ladenthin@gmail.com>
#
# SPDX-License-Identifier: MIT OR Apache-2.0
"""actionlib.common.templates: sets, placeholders, pins, exclusions, relocation, --write."""

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from actionlib.common import templates
from tests.helpers import REPO


class TemplatesTest(unittest.TestCase):

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        self.templates = base / "templates"
        self.root = base / "repo"
        self.write(self.templates / "base/SUPPORT.md", "Issues: https://github.com/{{owner}}/{{repo}}/issues\n")
        self.write(self.templates / "base/.github/workflows/ci.yml",
                   "jobs:\n  a:\n    steps:\n      - uses: actions/checkout@v7\n"
                   "      - uses: someone/thing/x@v1 # pinned later\n")
        self.write(self.templates / "lang/lombok.config", "lombok.addLombokGeneratedAnnotation = true\n")
        self.write(self.templates / "lang/.mvn/settings.xml", "<settings/>\n")
        self.settings = {"sets": ["base", "lang"], "exclude": [".mvn/settings.xml"],
                         "relocate": {"lombok.config": "module/lombok.config"}}

    @staticmethod
    def write(path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))

    def check(self):
        return templates.check(self.root, self.settings, "owner", "repo", self.templates)

    def test_write_renders_the_placeholders_and_honours_exclude_and_relocate(self):
        written = templates.write(self.root, self.settings, "owner", "repo", self.templates)
        self.assertEqual(sorted(written), [".github/workflows/ci.yml", "SUPPORT.md", "module/lombok.config"])
        self.assertEqual((self.root / "SUPPORT.md").read_text(), "Issues: https://github.com/owner/repo/issues\n")
        self.assertFalse((self.root / ".mvn/settings.xml").exists())
        self.assertEqual(self.check(), [])
        self.assertEqual(templates.write(self.root, self.settings, "owner", "repo", self.templates), [])

    def test_pins_and_line_endings_may_differ_content_may_not(self):
        templates.write(self.root, self.settings, "owner", "repo", self.templates)
        workflow = self.root / ".github/workflows/ci.yml"
        workflow.write_bytes(workflow.read_bytes()
                             .replace(b"checkout@v7", b"checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7")
                             .replace(b"x@v1 # pinned later", b"x@0123456789012345678901234567890123456789 # v1.2.0")
                             .replace(b"\n", b"\r\n"))
        self.assertEqual(self.check(), [])
        workflow.write_bytes(workflow.read_bytes().replace(b"jobs:", b"jobs: # changed"))
        problems = self.check()
        self.assertEqual(len(problems), 1)
        self.assertIn(".github/workflows/ci.yml differs from template .github/workflows/ci.yml", problems[0])
        self.assertIn("+jobs: # changed", problems[0])

    def test_a_missing_file_and_another_repository_name_are_problems(self):
        templates.write(self.root, self.settings, "owner", "other", self.templates)
        (self.root / "module/lombok.config").unlink()
        problems = self.check()
        self.assertEqual(len(problems), 2)
        self.assertIn("module/lombok.config is missing (template lombok.config)", problems)
        self.assertTrue(problems[0].startswith("SUPPORT.md differs") or problems[1].startswith("SUPPORT.md differs"))

    def test_configuration_errors(self):
        for settings, message in (
                ({"sets": ["nothing"], "exclude": [], "relocate": {}}, "no template set 'nothing'; available: base, lang"),
                ({"sets": ["base"], "exclude": ["gone.txt"], "relocate": {}}, "exclude/relocate name no template: gone.txt"),
                ({"sets": ["base"], "exclude": [], "relocate": {"SUPPORT.md": "../out.md"}}, "escapes the repository")):
            with self.subTest(message), self.assertRaisesRegex(ValueError, message):
                templates.check(self.root, settings, "owner", "repo", self.templates)
        self.write(self.templates / "lang/SUPPORT.md", "twice\n")
        with self.assertRaisesRegex(ValueError, "SUPPORT.md is in more than one of the sets"):
            templates.check(self.root, {"sets": ["base", "lang"], "exclude": [], "relocate": {}}, "o", "r", self.templates)

    def test_main_checks_against_this_repository_s_templates(self):
        self.root.mkdir(parents=True)
        (self.root / ".github").mkdir()
        (self.root / ".github/actions-config.json").write_text(json.dumps({"templates": {"sets": ["common"]}}))
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "someone/project"}):
            out = io.StringIO()
            self.assertEqual(templates.main(["--root", str(self.root), "--write"], out, io.StringIO()), 0)
            self.assertIn("wrote SUPPORT.md", out.getvalue())
            self.assertIn("github.com/someone/project/issues", (self.root / "SUPPORT.md").read_text(encoding="utf-8"))
            out = io.StringIO()
            self.assertEqual(templates.main(["--root", str(self.root)], out, io.StringIO()), 0)
            self.assertIn("0 problems", out.getvalue())


class RepositoryTemplatesTest(unittest.TestCase):

    def test_every_template_set_is_disjoint_and_every_template_is_text(self):
        sets = templates.available_sets()
        self.assertIn("common", sets)
        self.assertIn("java", sets)
        files = templates.template_files(sets)
        for relative, path in files.items():
            with self.subTest(relative):
                path.read_bytes().decode("utf-8")
        self.assertEqual(Path(templates.TEMPLATES), Path(REPO) / "templates")


if __name__ == "__main__":
    unittest.main()
