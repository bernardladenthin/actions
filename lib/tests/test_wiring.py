# SPDX-FileCopyrightText: 2026 Bernard Ladenthin <bernard.ladenthin@gmail.com>
#
# SPDX-License-Identifier: MIT OR Apache-2.0
"""actionlib.java.wiring over a publish workflow wired the way docs/publication.md describes."""

import io
import json
import os
import tempfile
import unittest

from actionlib.common import workflow
from actionlib.java import wiring

PIN = "@0123456789012345678901234567890123456789 # v1.0.0"
CHECKOUT = ["      - uses: actions/checkout@v7", "        with:", "          persist-credentials: false"]


def job(name, needs=None, head=(), steps=()):
    lines = [f"  {name}:"]
    if needs:
        lines.append(f"    needs: [{needs}]")
    lines += ["    runs-on: ubuntu-latest", *head, "    steps:", *steps]
    return lines


def gate(channel):
    return job(f"publish-{channel}", None, ["    outputs:", "      assets_complete: ${{ steps.finalize.outputs.complete }}"],
               ["      - run: mvn -DskipUpload=true deploy",
                "      - id: finalize",
                f"        uses: bernardladenthin/actions/java/finalize-publication{PIN}",
                "        with:", f"          channel: {channel}"])


def consumer(prefix, channel, action, head=()):
    condition = f"    if: ${{{{ !cancelled() && needs.publish-{channel}.outputs.assets_complete == 'true' }}}}"
    return job(f"{prefix}-{channel}", f"publish-{channel}", [condition, *head],
               [*CHECKOUT, f"      - uses: bernardladenthin/actions/java/{action}{PIN}",
                "        with:", f"          channel: {channel}"])


def ref_check(name, channel):
    return job(name, None, [], [*CHECKOUT, f"      - uses: bernardladenthin/actions/java/check-release-ref{PIN}",
                                "        with:", f"          channel: {channel}"])


def wired(drop=(), replace=None):
    lines = ["name: publish", "on:", "  push:", "jobs:"]
    lines += ref_check("check-snapshot", "snapshot") + ref_check("check-tag", "release")
    for channel in ("snapshot", "release"):
        lines += gate(channel)
        central_head = ["    permissions:", "      contents: read"]
        github_head = ["    permissions:", "      contents: write"]
        if channel == "snapshot":
            central_head = ["    concurrency:", "      group: central-snapshot-${{ github.repository }}",
                            "      cancel-in-progress: false", "    permissions:", "      contents: read",
                            "      actions: read"]
            github_head = ["    concurrency:", "      group: snapshot-release-${{ github.repository }}",
                           "      cancel-in-progress: false", "    permissions:", "      contents: write"]
        lines += consumer("central", channel, "publish-central", central_head)
        lines += consumer("github", channel, "publish-github", github_head)
        lines += consumer("attest", channel, "attest-publication",
                          ["    permissions:", "      id-token: write", "      attestations: write"])
    text = "\n".join(lines) + "\n"
    for line in drop:
        text = text.replace(line + "\n", "", 1)
    if replace:
        text = text.replace(*replace)
    return text


class WiringTest(unittest.TestCase):

    def problems(self, text):
        return wiring.check(workflow.parse(text))

    def test_the_documented_wiring_passes(self):
        self.assertEqual(self.problems(wired()), [])

    def test_a_consumer_waiting_for_another_job_fails(self):
        problems = self.problems(wired(replace=("    needs: [publish-release]\n    runs-on: ubuntu-latest\n"
                                                "    if: ${{ !cancelled() && needs.publish-release.outputs.assets_complete"
                                                " == 'true' }}\n    permissions:\n      contents: read",
                                                "    needs: [publish-release, check-tag]\n    runs-on: ubuntu-latest\n"
                                                "    if: ${{ !cancelled() && needs.publish-release.outputs.assets_complete"
                                                " == 'true' }}\n    permissions:\n      contents: read")))
        self.assertIn("central-release must need publish-release alone, not ['publish-release', 'check-tag']", problems)

    def test_a_snapshot_transfer_outside_its_concurrency_group_fails(self):
        problems = self.problems(wired(drop=["      group: central-snapshot-${{ github.repository }}"]))
        self.assertTrue(any(p.startswith("central-snapshot must run in concurrency group") for p in problems))

    def test_an_attestation_without_its_permissions_fails(self):
        problems = self.problems(wired(drop=["      attestations: write"]))
        self.assertIn("attest-snapshot needs attestations: write", problems)

    def test_a_gate_with_attestation_permissions_fails(self):
        text = wired().replace("    outputs:\n      assets_complete: ${{ steps.finalize.outputs.complete }}",
                               "    permissions:\n      id-token: write\n    outputs:\n"
                               "      assets_complete: ${{ steps.finalize.outputs.complete }}", 1)
        self.assertIn("publish-snapshot must not have id-token: write (the attestation job has it)", self.problems(text))

    def test_a_checkout_that_keeps_credentials_fails(self):
        problems = self.problems(wired(drop=["          persist-credentials: false"]))
        self.assertEqual(problems, ["check-snapshot: the checkout must set persist-credentials: false"])

    def test_a_missing_consumer_and_a_local_copy_of_an_action_fail(self):
        text = wired(replace=("uses: bernardladenthin/actions/java/publish-github" + PIN + "\n        with:\n"
                              "          channel: release", "uses: ./.github/actions/publish-github\n        with:\n"
                              "          channel: release"))
        self.assertIn("github-release does not run java/publish-github with channel: release", self.problems(text))
        self.assertIn("no job publish-nightly", wiring.check(workflow.parse(wired()), ["nightly"]))

    def test_main_reads_workflow_and_channels_from_the_configuration(self):
        with tempfile.TemporaryDirectory() as temp:
            os.makedirs(os.path.join(temp, ".github", "workflows"))
            with open(os.path.join(temp, ".github", "workflows", "release.yml"), "w") as f:
                f.write(wired())
            with open(os.path.join(temp, ".github", "actions-config.json"), "w") as f:
                json.dump({"publication-wiring": {"workflow": ".github/workflows/release.yml", "channels": ["release"]}}, f)
            out = io.StringIO()
            self.assertEqual(wiring.main(["--root", temp], out, io.StringIO()), 0)
            self.assertIn("publication wiring of release: 0 problems", out.getvalue())
            with open(os.path.join(temp, ".github", "actions-config.json"), "w") as f:
                json.dump({"publication-wiring": {"workflow": "../outside.yml"}}, f)
            err = io.StringIO()
            self.assertEqual(wiring.main(["--root", temp], io.StringIO(), err), 1)
            self.assertIn("escapes the repository", err.getvalue())


if __name__ == "__main__":
    unittest.main()
