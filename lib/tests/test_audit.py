# SPDX-FileCopyrightText: 2026 Bernard Ladenthin <bernard.ladenthin@gmail.com>
#
# SPDX-License-Identifier: MIT OR Apache-2.0
"""actionlib.java.audit: each check on fixtures, the failure it exists for included; no network."""

import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from actionlib.common import config, workflow
from actionlib.java import audit
from tests.helpers import workflow_text

POM = ('<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>'
       '<groupId>local.demo</groupId><artifactId>{artifact}</artifactId><version>1.2.0</version>'
       '<packaging>{packaging}</packaging>{modules}</project>')
FPR = "A" * 40
SUBKEY = "B" * 40


def classfile(major):
    return b"\xca\xfe\xba\xbe\x00\x00" + major.to_bytes(2, "big") + b"rest"


def jar(classes):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in classes.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class FakeGitHub:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def api(self, method, endpoint, payload=None, optional=False):
        self.calls.append(endpoint)
        for prefix, value in self.responses.items():
            if endpoint.startswith(prefix):
                return value
        raise AssertionError(f"unexpected GitHub call {endpoint}")


class Fixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "repo"
        self.assets = Path(tmp.name) / "assets"
        (self.root / ".github").mkdir(parents=True)
        (self.root / "lib").mkdir()
        self.assets.mkdir()
        (self.root / "pom.xml").write_text(POM.format(artifact="demo-parent", packaging="pom",
                                                      modules="<modules><module>lib</module></modules>"))
        (self.root / "lib/pom.xml").write_text(POM.format(artifact="demo", packaging="jar", modules=""))
        (self.root / ".github/natives.csv").write_text("# rows\nclassifier,x\nlinux,1\nwin,2\n")
        (self.root / ".github/release-assets.json").write_text(json.dumps({
            "schema": 1, "maven": {"cyclonedx": True},
            "files": [{"source": "lib/target/demo-{version}-{classifier}.jar", "central": True,
                       "classifiers": {"csv": ".github/natives.csv", "column": "classifier"}},
                      {"source": "dist/upstream-*-demo-{version}-src.tar.gz"},
                      {"source": "android/out.pom", "name": "demo-android-{version}.pom", "central": True}]}))

    def release(self, names):
        for name in names:
            (self.assets / name).write_bytes(name.encode())
            (self.assets / (name + ".asc")).write_bytes(b"sig " + name.encode())


class ExpectedFilesTest(Fixture):
    def test_the_contract_and_the_poms_give_names_central_paths_and_patterns(self):
        version, files, patterns = audit.expected_files(self.root)
        self.assertEqual(version, "1.2.0")
        self.assertEqual(files["demo-1.2.0.jar"], "local/demo/demo/1.2.0/demo-1.2.0.jar")
        self.assertEqual(files["demo-parent-1.2.0.pom"], "local/demo/demo-parent/1.2.0/demo-parent-1.2.0.pom")
        self.assertEqual(files["demo-1.2.0-cyclonedx.json"], "local/demo/demo/1.2.0/demo-1.2.0-cyclonedx.json")
        self.assertEqual(files["demo-1.2.0-win.jar"], "local/demo/demo/1.2.0/demo-1.2.0-win.jar")
        # a file rule's artifact is its name before -<version>, in the reactor's group
        self.assertEqual(files["demo-android-1.2.0.pom"], "local/demo/demo-android/1.2.0/demo-android-1.2.0.pom")
        self.assertNotIn("demo-parent-1.2.0.jar", files)  # a pom-only module has no jars
        self.assertEqual(patterns, ["upstream-*-demo-1.2.0-src.tar.gz"])
        self.assertEqual(len(files), 2 + 5 + 2 + 1)  # the parent gets a CycloneDX BOM too


class AssetsTest(Fixture):
    def test_a_complete_release_passes(self):
        _, files, patterns = audit.expected_files(self.root)
        self.release(list(files) + ["upstream-b42-demo-1.2.0-src.tar.gz"])
        result = audit.check_assets([p.name for p in self.assets.iterdir()], files, patterns)
        self.assertEqual(result.problems, [])
        self.assertIn("11 files + 11 signatures; 10 Central artifacts", result.notes)

    def test_missing_extra_unsigned_orphan_and_an_ambiguous_pattern(self):
        _, files, patterns = audit.expected_files(self.root)
        present = {n for n in files if n != "demo-1.2.0-win.jar"} | {"stray.jar", "upstream-1-demo-1.2.0-src.tar.gz",
                                                                     "upstream-2-demo-1.2.0-src.tar.gz"}
        names = set(present) | {n + ".asc" for n in present if n != "stray.jar"} | {"gone.jar.asc"}
        problems = audit.check_assets(names, files, patterns).problems
        self.assertIn("upstream-*-demo-1.2.0-src.tar.gz: expected one release file, found "
                      "['upstream-1-demo-1.2.0-src.tar.gz', 'upstream-2-demo-1.2.0-src.tar.gz']", problems)
        self.assertIn("missing from the release: demo-1.2.0-win.jar", problems)
        self.assertIn("not in the contract: stray.jar", problems)
        self.assertIn("unsigned: stray.jar has no .asc", problems)
        self.assertIn("orphan signature: gone.jar.asc", problems)


class RunAndJobsTest(unittest.TestCase):
    def github(self, runs, status="ahead"):
        return FakeGitHub({"repos/o/r/actions/workflows/publish.yml/runs?branch=v1.2.0": {"workflow_runs": runs},
                           "repos/o/r/compare/": {"status": status}, "repos/o/r": {"default_branch": "main"}})

    def test_one_successful_run_at_the_tag_commit_passes_and_cancelled_ones_are_noted(self):
        runs = [{"id": 2, "conclusion": "success", "head_sha": "c", "event": "workflow_dispatch", "run_attempt": 1},
                {"id": 1, "conclusion": "cancelled", "head_sha": "old", "event": "push", "status": "completed"}]
        result, run = audit.check_run(self.github(runs), "o/r", ".github/workflows/publish.yml", "v1.2.0", "c")
        self.assertEqual((result.problems, run["id"]), ([], 2))
        self.assertIn("1 cancelled", result.notes[1])

    def test_two_successes_another_commit_or_off_the_default_branch_fail(self):
        two = [{"id": i, "conclusion": "success", "head_sha": "c", "event": "push"} for i in (1, 2)]
        result, run = audit.check_run(self.github(two), "o/r", "publish.yml", "v1.2.0", "c")
        self.assertIsNone(run)
        self.assertIn("expected one successful run on v1.2.0, found [1, 2]", result.problems)
        other = [{"id": 3, "conclusion": "success", "head_sha": "x", "event": "push"}]
        result, _ = audit.check_run(self.github(other, "diverged"), "o/r", "publish.yml", "v1.2.0", "c")
        self.assertEqual(result.problems, ["run 3 built x, the tag is c",
                                           "the tag commit is not on main (compare: diverged)"])

    def test_the_other_channel_jobs_are_read_by_display_name(self):
        jobs = workflow.parse(workflow_text([
            ("check-snapshot", None, ["      - run: x"]), ("publish-snapshot", None, ["      - run: x"]),
            ("central-snapshot", "publish-snapshot", ["      - run: x"]),
            ("github-snapshot", "publish-snapshot", ["      - run: x"]),
            ("attest-snapshot", "publish-snapshot", ["      - run: x"]), ("build", None, ["      - run: x"])]))
        jobs["publish-snapshot"].lines.insert(1, "    name: Prepare snapshot")
        self.assertEqual(audit.channel_jobs(jobs, "snapshot"),
                         {"check-snapshot", "Prepare snapshot", "central-snapshot", "github-snapshot", "attest-snapshot"})

    def test_jobs(self):
        other = {"Snap A", "Snap B"}
        ok = [{"name": "build", "conclusion": "success"}, {"name": "Snap A", "conclusion": "skipped"},
              {"name": "Snap B", "conclusion": "skipped"}]
        self.assertEqual(audit.check_jobs(ok, other).problems, [])
        bad = [{"name": "build", "conclusion": "failure"}, {"name": "package", "conclusion": "skipped"},
               {"name": "Snap A", "conclusion": "success"}]
        result = audit.check_jobs(bad, other)
        self.assertEqual(result.problems, ["failure: build", "skipped, but not a job of the other channel: package",
                                           "ran, but belongs to the other channel: Snap A"])
        self.assertEqual(result.warnings, ["the other channel's job Snap B is not in the run"])


class CentralTest(Fixture):
    def test_checksums_signatures_absent_files_and_the_wait(self):
        files = {"demo-1.2.0.jar": "g/demo/1.2.0/demo-1.2.0.jar", "demo-1.2.0-src.tar.gz": None,
                 "demo-1.2.0.pom": "g/demo/1.2.0/demo-1.2.0.pom"}
        self.release(files)
        jar_sum = hashlib.sha256(b"demo-1.2.0.jar").hexdigest()
        served = {"B/g/demo/1.2.0/demo-1.2.0.jar.sha256": f"{jar_sum}  demo-1.2.0.jar".encode(),
                  "B/g/demo/1.2.0/demo-1.2.0.jar.asc": b"sig demo-1.2.0.jar",
                  "B/g/demo/1.2.0/demo-1.2.0.pom.sha1": hashlib.sha1(b"tampered").hexdigest().encode(),
                  "B/g/demo/1.2.0/demo-1.2.0.pom.asc": b"sig demo-1.2.0.pom"}
        clock = iter(range(0, 10000, 100))
        sleeps = []
        result = audit.check_central(files, self.assets, wait_minutes=3, base="B", get=served.get,
                                     sleep=sleeps.append, clock=lambda: next(clock))
        self.assertEqual(len(result.problems), 1)
        self.assertTrue(result.problems[0].startswith("g/demo/1.2.0/demo-1.2.0.pom: sha1 differs"))
        self.assertEqual(sleeps, [])  # nothing absent, nothing to wait for
        served.pop("B/g/demo/1.2.0/demo-1.2.0.jar.asc")
        result = audit.check_central(files, self.assets, wait_minutes=3, base="B", get=served.get,
                                     sleep=sleeps.append, clock=lambda: next(clock))
        self.assertIn("g/demo/1.2.0/demo-1.2.0.jar.asc: absent", result.problems)
        self.assertTrue(sleeps)  # polled until the deadline


class SignaturesTest(Fixture):
    def run_check(self, outcomes, fingerprint=FPR):
        self.release(["a.jar", "b.jar"])
        with patch.object(audit, "import_key"):
            return audit.check_signatures(self.assets, fingerprint, get=lambda url: b"-----BEGIN PGP PUBLIC KEY BLOCK",
                                          verify=lambda home, sig, art: outcomes[sig.name])

    def test_one_subkey_of_the_release_key_passes(self):
        result = self.run_check({"a.jar.asc": (SUBKEY, FPR), "b.jar.asc": (SUBKEY, FPR)})
        self.assertEqual(result.problems, [])

    def test_a_bad_signature_another_key_and_two_subkeys_fail(self):
        result = self.run_check({"a.jar.asc": None, "b.jar.asc": (SUBKEY, "C" * 40)})
        self.assertEqual(result.problems, ["bad or unverifiable signature: a.jar.asc",
                                           f"b.jar.asc: signed by key {'C' * 40}, not {FPR}"])
        result = self.run_check({"a.jar.asc": (SUBKEY, FPR), "b.jar.asc": ("D" * 40, FPR)})
        self.assertTrue(result.problems[0].startswith("more than one signing key"))

    def test_the_fingerprint_must_be_configured(self):
        self.assertEqual(audit.check_signatures(self.assets, "").problems,
                         ["post-release.signing-key must be the release key's 40-hex primary fingerprint"])

    def test_the_keyserver_answer_must_be_a_key(self):
        with self.assertRaisesRegex(ValueError, "no keyserver serves"):
            audit.fetch_key(FPR, get=lambda url: b"<html>not found</html>")


class ProvenanceTest(Fixture):
    def statement(self, subjects, run=7, sha="c"):
        return {"verificationResult": {
            "signature": {"certificate": {"githubWorkflowSHA": sha}},
            "statement": {"subject": [{"name": n, "digest": {"sha256": d}} for n, d in subjects],
                          "predicate": {"runDetails": {"metadata": {
                              "invocationId": f"https://github.com/o/r/actions/runs/{run}/attempts/1"}}}}}}

    def test_every_file_must_be_a_subject_with_its_digest(self):
        self.release(["a.jar", "b.jar"])
        digest = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.assets.iterdir()}
        full = [(n, d) for n, d in digest.items()] + [("libnative.so", "0" * 64)]
        result = audit.check_provenance([self.statement(full)], self.assets, "o/r", {"id": 7}, "c")
        self.assertEqual(result.problems, [])
        partial = [(n, d) for n, d in digest.items() if n != "b.jar.asc"]
        partial = [(n, "f" * 64 if n == "a.jar" else d) for n, d in partial]
        result = audit.check_provenance([self.statement(partial), self.statement(full, run=99)],
                                        self.assets, "o/r", {"id": 7}, "c")
        self.assertEqual(result.problems, ["attested with another digest: a.jar", "not attested: b.jar.asc"])
        self.assertIn("an attestation of another run", result.notes[0])

    def test_the_attestation_must_come_from_the_tag_commit(self):
        self.release(["a.jar"])
        digest = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.assets.iterdir()}
        result = audit.check_provenance([self.statement(digest.items(), sha="x")], self.assets, "o/r", {"id": 7}, "c")
        self.assertEqual(result.problems, ["attestation signed at x, the tag is c"])


class BytecodeTest(Fixture):
    def test_floors_per_pattern_module_info_versions_and_the_aar_classes_jar(self):
        (self.assets / "demo-1.2.0.jar").write_bytes(jar({"a/A.class": classfile(52), "module-info.class": classfile(53),
                                                          "META-INF/versions/11/a/B.class": classfile(55)}))
        (self.assets / "demo-ffm-1.2.0.jar").write_bytes(jar({"f/F.class": classfile(66)}))
        (self.assets / "demo-1.2.0-sources.jar").write_bytes(jar({"a/A.java": b""}))
        (self.assets / "demo-android-1.2.0.aar").write_bytes(jar({"classes.jar": jar({"a/A.class": classfile(52)})}))
        rules = [{"files": "demo-ffm-*.jar", "major": 66}, {"files": "*", "major": 52}]
        result = audit.check_bytecode(self.assets, rules)
        self.assertEqual(result.problems, [])
        self.assertIn("*: 2 classes <= 52", result.notes)
        (self.assets / "demo-ffm-1.2.0.jar").write_bytes(jar({"f/F.class": classfile(66)}))
        result = audit.check_bytecode(self.assets, [{"files": "*", "major": 52}])
        self.assertEqual(result.problems, ["demo-ffm-1.2.0.jar: 1 classes above major 52, e.g. f/F.class (66)"])

    def test_a_rule_that_scanned_nothing_fails(self):
        (self.assets / "demo-1.2.0.jar").write_bytes(jar({"a/A.class": classfile(52)}))
        result = audit.check_bytecode(self.assets, [{"files": "demo-ffm-*.jar", "major": 66}, {"files": "*", "major": 52}])
        self.assertEqual(result.problems, ["the rule demo-ffm-*.jar (major 66) scanned no class"])
        result = audit.check_bytecode(self.assets, [{"files": "other-*.jar", "major": 52}])
        self.assertIn("demo-1.2.0.jar: no bytecode rule matches", result.problems)
        (self.assets / "demo-1.2.0.jar").write_bytes(b"truncated")
        result = audit.check_bytecode(self.assets, [{"files": "*", "major": 52}])
        self.assertIn("demo-1.2.0.jar: not a zip archive", result.problems)


class NextAndSettingsTest(Fixture):
    def test_the_next_minor_snapshot(self):
        self.assertEqual(audit.check_next("6.0.0", "6.1.0-SNAPSHOT").warnings, [])
        self.assertIn("still names the released 6.0.0", audit.check_next("6.0.0", "6.0.0").warnings[0])
        self.assertIn("the next minor after 6.0.0 is 6.1.0-SNAPSHOT", audit.check_next("6.0.0", "6.0.1-SNAPSHOT").warnings[0])
        self.assertEqual(audit.check_next("6.0.0", "6.0.0").problems, [])  # a warning, never a failure

    def test_settings(self):
        self.assertIn("post-release", config.SECTIONS)
        self.assertEqual(audit.settings(self.root)["bytecode"], [{"files": "*", "major": 52}])
        (self.root / ".github/actions-config.json").write_text(json.dumps(
            {"post-release": {"bytecode": [{"files": "*", "major": "52"}]}}))
        with self.assertRaisesRegex(ValueError, "post-release.bytecode"):
            audit.settings(self.root)
        (self.root / ".github/actions-config.json").write_text(json.dumps({"post-release": {"signing_key": "x"}}))
        with self.assertRaisesRegex(ValueError, "unknown key"):
            audit.settings(self.root)

    def test_main_runs_the_offline_checks_and_refuses_a_checkout_at_another_version(self):
        _, files, _ = audit.expected_files(self.root)
        self.release(list(files) + ["upstream-b42-demo-1.2.0-src.tar.gz"])
        out = io.StringIO()
        # the configuration comes from the current directory, not from the tag's checkout
        (self.root / ".github/actions-config.json").write_text('{"post-release": {"unknown": 1}}')
        workspace = self.root.parent / "workspace"
        (workspace / ".github").mkdir(parents=True)
        with patch.dict("os.environ", {"GITHUB_REPOSITORY": "o/r", "GITHUB_STEP_SUMMARY": ""}),                 patch("pathlib.Path.cwd", return_value=workspace):
            code = audit.main(["--root", str(self.root), "--tag", "v1.2.0", "--assets", str(self.assets),
                               "--checks", "assets"], github=FakeGitHub({}), out=out)
            self.assertEqual(code, 0, out.getvalue())
            out = io.StringIO()
            code = audit.main(["--root", str(self.root), "--tag", "v1.3.0", "--assets", str(self.assets),
                               "--checks", "assets"], github=FakeGitHub({}), out=out)
        self.assertEqual(code, 1)
        self.assertIn("the checkout is at version 1.2.0, not the tag v1.3.0", out.getvalue())


if __name__ == "__main__":
    unittest.main()
