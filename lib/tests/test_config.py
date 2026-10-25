# SPDX-FileCopyrightText: 2026 Bernard Ladenthin <bernard.ladenthin@gmail.com>
#
# SPDX-License-Identifier: MIT OR Apache-2.0
"""actionlib.common.config: the per-repository .github/actions-config.json."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from actionlib.common import config


class ConfigTest(unittest.TestCase):

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / ".github").mkdir()

    def write(self, data):
        (self.root / ".github/actions-config.json").write_text(json.dumps(data))

    def test_an_absent_default_file_is_empty_unless_required(self):
        self.assertEqual(config.load(self.root), {})
        with self.assertRaisesRegex(ValueError, "not found"):
            config.load(self.root, required=True)
        with self.assertRaisesRegex(ValueError, "not found"):
            config.load(self.root, "ci/other.json")

    def test_unknown_sections_and_keys_fail_and_defaults_merge(self):
        self.write({"release-gates": {}})
        with self.assertRaisesRegex(ValueError, "unknown section"):
            config.load(self.root)
        self.write({"$comment": "fine", "release-gate": {"gates": ["a"], "$comment": "fine"}})
        loaded = config.load(self.root)
        self.assertEqual(config.section(loaded, "release-gate", {"gates": [], "workflow": "w.yml"}),
                         {"gates": ["a"], "workflow": "w.yml"})
        with self.assertRaisesRegex(ValueError, "unknown key"):
            config.section({"release-gate": {"gate": 1}}, "release-gate", {"gates": []})
        with self.assertRaisesRegex(ValueError, "missing poms"):
            config.section({"maven-versions": {}}, "maven-versions", {}, required=("poms",))

    def test_paths_stay_inside_the_repository(self):
        for path in ("/etc/passwd", "../x", "a/../../x", ""):
            with self.subTest(path), self.assertRaises(ValueError):
                config.inside(self.root, path)
        self.assertEqual(config.inside(self.root, "a/b"), (self.root / "a/b").resolve())

    def test_the_repository_comes_from_the_environment_first(self):
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "owner/name"}):
            self.assertEqual(config.repository(self.root), ("owner", "name"))
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": ""}):
            owner, name = config.repository(self.root)
            self.assertEqual(name, self.root.resolve().name)

    def test_remote_urls(self):
        for url in ("git@github.com:owner/repo.git", "https://github.com/owner/repo", "https://github.com/owner/repo.git"):
            with self.subTest(url):
                match = config.REMOTE.search(url)
                self.assertEqual((match.group(1), match.group(2)), ("owner", "repo"))


if __name__ == "__main__":
    unittest.main()
