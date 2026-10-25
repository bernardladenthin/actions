# SPDX-FileCopyrightText: 2026 Bernard Ladenthin <bernard.ladenthin@gmail.com>
#
# SPDX-License-Identifier: MIT OR Apache-2.0

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from actionlib.common.snapshot import GitHub, publish


OLD, NEW = "a" * 40, "b" * 40


class FakeGitHub:
    def __init__(self):
        self.ref = {"object": {"type": "commit", "sha": OLD}}
        self.release = {"id": 7, "body": "Snapshot from old commit"}
        self.comparison = "ahead"
        self.pages = {1: [{"id": 1, "name": "old.jar"}, {"id": 2, "name": "new.json"}]}
        self.calls = []
        self.fail_upload = False
        self.fail_deletes = set()
        self.tag = "snapshot"
        self.drafts = []
        self.refuse_ref_patch = None

    def api(self, method, endpoint, payload=None, optional=False):
        self.calls.append((method, endpoint, payload))
        if method == "PATCH" and "/git/refs/tags/" in endpoint and self.refuse_ref_patch:
            raise RuntimeError(f"GitHub PATCH {endpoint}: {self.refuse_ref_patch}")
        if method == "DELETE" and "/git/refs/" in endpoint:
            return None
        if method == "DELETE" and int(endpoint.rsplit("/", 1)[1]) in self.fail_deletes:
            raise RuntimeError("delete failed")
        if method == "GET":
            if endpoint.endswith(f"git/ref/tags/{self.tag}"):
                return self.ref
            if endpoint.endswith(f"releases/tags/{self.tag}"):
                return self.release
            if "/compare/" in endpoint:
                return {"status": self.comparison}
            if "/git/tags/" in endpoint:
                return {"object": {"type": "commit", "sha": OLD}}
            if "/assets?" in endpoint:
                return self.pages[int(endpoint.split("&page=")[1])]
            if endpoint.endswith("/releases?per_page=100"):
                return self.drafts
        if method == "POST" and endpoint.endswith("/releases"):
            return {"id": 7}
        if method == "POST" and endpoint.endswith("/git/refs"):
            return {"object": {"type": "commit", "sha": payload["sha"]}}
        return {}

    def command(self, *args):
        self.calls.append(("upload", args, None))
        if self.fail_upload:
            raise subprocess.CalledProcessError(1, "gh release upload")


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.assets = Path(self.temp.name)
        for name in ("new.json", "new.json.asc"):
            (self.assets / name).write_text("artifact")
        self.gh = FakeGitHub()
        quiet = patch("builtins.print")
        quiet.start()
        self.addCleanup(quiet.stop)

    def publish(self, **kwargs):
        return publish("owner/repo", NEW, kwargs.pop("run_id", 10), kwargs.pop("attempt", 1),
                       self.assets, kwargs.pop("complete", True), self.gh, **kwargs)

    def test_upload_precedes_tag_notes_and_prune_and_release_itself_survives(self):
        self.assertTrue(self.publish())
        calls = self.gh.calls
        upload = next(i for i, c in enumerate(calls) if c[0] == "upload")
        tag = next(i for i, c in enumerate(calls) if c[0] == "PATCH" and "/git/refs/" in c[1])
        notes = next(c[2] for c in calls if c[0] == "PATCH" and c[1].endswith("/releases/7"))
        self.assertLess(upload, tag)
        self.assertIn(NEW, notes["body"])
        self.assertIn("/actions/runs/10/attempts/1", notes["body"])
        self.assertEqual(notes["target_commitish"], NEW)
        deletes = [c[1] for c in calls if c[0] == "DELETE"]
        self.assertEqual(deletes, ["repos/owner/repo/releases/assets/1"])

    def test_incomplete_collection_changes_nothing(self):
        with self.assertRaisesRegex(ValueError, "incomplete collection"):
            self.publish(complete=False)
        self.assertEqual(self.gh.calls, [])

    def test_missing_signature_also_changes_nothing(self):
        (self.assets / "new.json.asc").unlink()
        with self.assertRaisesRegex(ValueError, "incomplete collection.*unsigned: new.json"):
            self.publish()
        self.assertEqual(self.gh.calls, [])

    def test_versioned_release_tag_is_never_a_rolling_snapshot(self):
        for tag in ("v1.2.3", "v2"):
            with self.subTest(tag=tag):
                with self.assertRaisesRegex(ValueError, "versioned release"):
                    self.publish(tag=tag)
                self.assertEqual(self.gh.calls, [])

    def test_names_github_would_rename_are_refused_before_any_change(self):
        # GitHub would upload "a b.jar" as "a.b.jar", and the prune would then delete it.
        for name in ("a b.jar", "a b.jar.asc"):
            (self.assets / name).write_text("artifact")
        with self.assertRaisesRegex(ValueError, "rename release assets: a b.jar"):
            self.publish()
        self.assertEqual(self.gh.calls, [])

    def test_late_finishing_older_commit_cannot_overwrite_newer_snapshot(self):
        self.gh.comparison = "behind"
        self.assertFalse(self.publish())
        self.assertTrue(all(c[0] == "GET" for c in self.gh.calls))

    def test_empty_signature_or_orphan_also_prevents_promotion(self):
        for name, content in (("new.json.asc", ""), ("orphan.jar.asc", "signature")):
            with self.subTest(name=name):
                (self.assets / "new.json.asc").write_text("signature")
                (self.assets / name).write_text(content)
                self.gh.calls.clear()
                with self.assertRaisesRegex(ValueError, "incomplete collection"):
                    self.publish()
                self.assertEqual(self.gh.calls, [])

    def test_rolling_tag_and_title_are_parameters(self):
        self.gh.tag = "nightly"
        self.assertTrue(self.publish(tag="nightly", title="Nightly build"))
        upload = next(c for c in self.gh.calls if c[0] == "upload")
        self.assertEqual(upload[1][2], "nightly")
        notes = next(c[2] for c in self.gh.calls if c[0] == "PATCH" and c[1].endswith("/releases/7"))
        self.assertEqual(notes["name"], "Nightly build")

    def test_same_commit_older_run_or_attempt_is_skipped(self):
        self.gh.ref["object"]["sha"] = NEW
        for run, attempt in ((11, 1), (10, 2)):
            self.gh.release["body"] = f"<!-- snapshot-publish {run} {attempt} {NEW} -->"
            self.gh.calls.clear()
            self.assertFalse(self.publish())
            self.assertTrue(all(c[0] == "GET" for c in self.gh.calls))

    def test_new_attempt_of_same_commit_can_replace_old_artifacts(self):
        self.gh.ref["object"]["sha"] = NEW
        self.gh.release["body"] = f"<!-- snapshot-publish 10 1 {NEW} -->"
        self.assertTrue(self.publish(attempt=2))

    def test_annotated_tags_are_resolved_before_comparing_commits(self):
        self.gh.ref["object"]["type"] = "tag"
        self.assertTrue(self.publish())
        self.assertTrue(any("/git/tags/" in c[1] for c in self.gh.calls if c[0] == "GET"))

    def test_creates_missing_tag_and_release_once(self):
        self.gh.ref, self.gh.release = None, None
        self.assertTrue(self.publish())
        calls = self.gh.calls
        self.assertEqual(sum(c[0] == "POST" and c[1].endswith("/git/refs") for c in calls), 1)
        created = [c[2] for c in calls if c[0] == "POST" and c[1].endswith("/releases")]
        self.assertEqual(len(created), 1)
        # A draft without the build's notes until the upload is complete; the tag comes after it.
        self.assertTrue(created[0]["draft"])
        self.assertNotIn(NEW, created[0]["body"])
        upload = next(i for i, c in enumerate(calls) if c[0] == "upload")
        tag = next(i for i, c in enumerate(calls) if c[0] == "POST" and c[1].endswith("/git/refs"))
        self.assertLess(upload, tag)
        notes = next(c[2] for c in calls if c[0] == "PATCH" and c[1].endswith("/releases/7"))
        self.assertFalse(notes["draft"])
        self.assertIn(NEW, notes["body"])

    def test_failed_first_upload_leaves_no_tag_and_no_public_release(self):
        self.gh.ref, self.gh.release = None, None
        self.gh.fail_upload = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.publish()
        self.assertFalse(any(c[0] in ("PATCH", "DELETE") or c[1].endswith("/git/refs")
                             for c in self.gh.calls if c[0] != "upload"))
        created = next(c[2] for c in self.gh.calls if c[0] == "POST" and c[1].endswith("/releases"))
        self.assertTrue(created["draft"])

    def test_a_draft_a_failed_first_upload_left_is_reused(self):
        self.gh.ref, self.gh.release = None, None
        self.gh.drafts = [{"id": 5, "draft": False, "tag_name": "snapshot"},
                          {"id": 6, "draft": True, "tag_name": "other"},
                          {"id": 8, "draft": True, "tag_name": "snapshot"}]
        self.assertTrue(self.publish())
        self.assertFalse(any(c[0] == "POST" and c[1].endswith("/releases") for c in self.gh.calls))
        notes = next(c[2] for c in self.gh.calls if c[0] == "PATCH" and c[1].endswith("/releases/8"))
        self.assertFalse(notes["draft"])

    def test_reads_all_asset_pages_before_deleting(self):
        self.gh.pages = {1: [{"id": i, "name": f"old-{i}.jar"} for i in range(100)],
                         2: [{"id": 101, "name": "last-old.jar"}, {"id": 102, "name": "new.json"}]}
        self.assertTrue(self.publish())
        calls = self.gh.calls
        page2 = next(i for i, c in enumerate(calls) if c[0] == "GET" and "&page=2" in c[1])
        deletion = next(i for i, c in enumerate(calls) if c[0] == "DELETE")
        self.assertLess(page2, deletion)
        self.assertEqual(sum(c[0] == "DELETE" for c in calls), 101)

    def test_divergent_commit_and_empty_upload_are_refused(self):
        self.gh.comparison = "diverged"
        with self.assertRaisesRegex(ValueError, "divergent"):
            self.publish()
        self.assertTrue(all(c[0] == "GET" for c in self.gh.calls))
        for path in self.assets.iterdir():
            path.unlink()
        self.gh.calls.clear()
        with self.assertRaisesRegex(ValueError, "nonempty"):
            self.publish()
        self.assertEqual(self.gh.calls, [])

    def test_upload_failure_does_not_move_tag_or_prune(self):
        self.gh.fail_upload = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.publish()
        self.assertFalse(any(c[0] in ("PATCH", "DELETE") for c in self.gh.calls))

    def test_prune_tries_remaining_assets_after_a_deletion_failure(self):
        self.gh.pages[1].append({"id": 3, "name": "another-old.jar"})
        self.gh.fail_deletes.add(1)
        with self.assertRaisesRegex(RuntimeError, "delete failed"):
            self.publish()
        self.assertTrue(any(c[0] == "DELETE" and c[1].endswith("/assets/3") for c in self.gh.calls))

    def test_signature_only_directory_does_not_mutate_snapshot(self):
        (self.assets / "new.json").unlink()
        with self.assertRaisesRegex(ValueError, "no artifacts"):
            self.publish()
        self.assertEqual(self.gh.calls, [])

    def test_a_refused_tag_move_deletes_and_recreates_the_tag(self):
        self.gh.refuse_ref_patch = "gh: Resource not accessible by integration (HTTP 403)"
        with patch("builtins.print"):
            self.assertTrue(self.publish())
        calls = [(c[0], c[1]) for c in self.gh.calls]
        refused = calls.index(("PATCH", "repos/owner/repo/git/refs/tags/snapshot"))
        self.assertEqual(calls[refused + 1:refused + 3],
                         [("DELETE", "repos/owner/repo/git/refs/tags/snapshot"),
                          ("POST", "repos/owner/repo/git/refs")])
        self.assertEqual(self.gh.calls[refused + 2][2], {"ref": "refs/tags/snapshot", "sha": NEW})
        notes = next(c for c in self.gh.calls if c[0] == "PATCH" and c[1].endswith("/releases/7"))
        self.assertGreater(self.gh.calls.index(notes), refused + 2)
        self.assertEqual(notes[2]["tag_name"], "snapshot")
        self.assertFalse(notes[2]["draft"])

    def test_another_tag_move_failure_is_not_turned_into_a_delete(self):
        self.gh.refuse_ref_patch = "gh: Validation Failed (HTTP 422)"
        with self.assertRaisesRegex(RuntimeError, "422"):
            self.publish()
        self.assertFalse(any(c[0] == "DELETE" for c in self.gh.calls))
        self.assertFalse(any(c[0] == "PATCH" and c[1].endswith("/releases/7") for c in self.gh.calls))

    def test_a_read_is_retried_on_a_server_error_and_nothing_else_is(self):
        gh = GitHub()
        fail = subprocess.CompletedProcess([], 1, "", "gh: Server Error (HTTP 502)")
        ok = subprocess.CompletedProcess([], 0, '{"jobs": []}', "")
        with patch("actionlib.common.snapshot.time.sleep") as sleep, patch("builtins.print"):
            with patch("actionlib.common.snapshot.subprocess.run", side_effect=[fail, fail, ok]) as run:
                self.assertEqual(gh.api("GET", "repos/owner/repo/actions/runs/1/jobs"), {"jobs": []})
            self.assertEqual(run.call_count, 3)
            self.assertEqual([c.args[0] for c in sleep.call_args_list], [5, 15])
            with patch("actionlib.common.snapshot.subprocess.run", return_value=fail) as run:
                with self.assertRaisesRegex(RuntimeError, "502"):
                    gh.api("GET", "repos/owner/repo/actions/runs/1/jobs")
            self.assertEqual(run.call_count, 4)
            stream = subprocess.CompletedProcess([], 1, "", "stream error: stream ID 1; CANCEL; received from peer")
            with patch("actionlib.common.snapshot.subprocess.run", side_effect=[stream, ok]) as run:
                gh.api("GET", "repos/owner/repo/actions/runs/1/jobs")
            self.assertEqual(run.call_count, 2)
            for method, stderr in (("PATCH", "gh: Server Error (HTTP 502)"),
                                   ("GET", "gh: Resource not accessible by integration (HTTP 403)")):
                response = subprocess.CompletedProcess([], 1, "", stderr)
                with patch("actionlib.common.snapshot.subprocess.run", return_value=response) as run:
                    with self.assertRaises(RuntimeError):
                        gh.api(method, "repos/owner/repo/git/refs/tags/snapshot", {"sha": NEW})
                self.assertEqual(run.call_count, 1, method)

    def test_optional_api_failure_accepts_only_404_not_authentication_failure(self):
        gh = GitHub()
        for status in (403, 404):
            response = subprocess.CompletedProcess([], 1, "", f"gh: failure (HTTP {status})")
            with patch("actionlib.common.snapshot.subprocess.run", return_value=response):
                if status == 404:
                    self.assertIsNone(gh.api("GET", "repos/owner/repo/releases/tags/snapshot", optional=True))
                else:
                    with self.assertRaisesRegex(RuntimeError, "403"):
                        gh.api("GET", "repos/owner/repo/releases/tags/snapshot", optional=True)


if __name__ == "__main__":
    unittest.main()
