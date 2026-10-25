# SPDX-FileCopyrightText: 2026 Bernard Ladenthin <bernard.ladenthin@gmail.com>
#
# SPDX-License-Identifier: MIT OR Apache-2.0
"""Update the rolling snapshot without replacing a newer publish or deleting the release.

The workflow serializes this entire operation with a job concurrency group. The ancestry/run
checks also reject an older build which finishes after a newer build has already published.
An incomplete collection changes nothing: no upload, no tag or notes update, no prune. The first
snapshot starts as a draft, so a failed first upload leaves neither a public release nor a tag.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from .signatures import signature_gaps

# GitHub renames release assets with other characters; a renamed asset would be pruned as obsolete.
ASSET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

# A GET that GitHub answers with a server error or a dropped HTTP/2 stream is asked again after
# these pauses (seconds). Listing the jobs of a large run (java-llama.cpp: ~80 jobs, several
# attempts) failed that way in two consecutive central-snapshot jobs, and a run that dies on a
# read wastes a whole publication. Only reads are repeated: a write may have taken effect.
RETRY_DELAYS = (5, 15, 45)
TRANSIENT = re.compile(r"HTTP 5\d\d|stream error|CANCEL|connection reset|timed out|EOF", re.IGNORECASE)


class GitHub:
    def command(self, *args):
        subprocess.run(["gh", *map(str, args)], check=True)

    def api(self, method, endpoint, payload=None, optional=False):
        args = ["gh", "api", "--method", method, endpoint]
        if payload is not None:
            args += ["--input", "-"]
        delays = RETRY_DELAYS if method == "GET" else ()
        for attempt in range(len(delays) + 1):
            result = subprocess.run(args, input=json.dumps(payload) if payload is not None else None,
                                    text=True, capture_output=True)
            if not result.returncode or attempt == len(delays) or not TRANSIENT.search(result.stderr):
                break
            print(f"GitHub {method} {endpoint}: {result.stderr.strip()}; retrying in {delays[attempt]} s",
                  file=sys.stderr)
            time.sleep(delays[attempt])
        if result.returncode:
            if optional and "HTTP 404" in result.stderr:
                return None
            raise RuntimeError(f"GitHub {method} {endpoint}: {result.stderr.strip()}")
        return json.loads(result.stdout) if result.stdout.strip() else None


def publish(repository, sha, run_id, attempt, assets_dir, complete, github=None,
            *, tag="snapshot", title="Snapshot (latest)"):
    """Return False for a superseded run; otherwise upload and update the rolling release."""
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("invalid repository or published commit")
    if not re.fullmatch(r"[\w][\w.-]*", tag) or ".." in tag or tag.endswith((".", ".lock")):
        raise ValueError("invalid rolling release tag")
    if re.match(r"v\d", tag):
        raise ValueError("a versioned release is never a rolling snapshot: " + tag)
    run_id, attempt = int(run_id), int(attempt)
    if run_id < 1 or attempt < 1:
        raise ValueError("run ID and attempt must be positive")
    files = sorted(Path(assets_dir).iterdir())
    if not files or any(not path.is_file() for path in files):
        raise ValueError("refusing to update snapshot without a nonempty, flat asset directory")
    artifacts = [path for path in files if not path.name.endswith(".asc")]
    if not artifacts:
        raise ValueError("refusing to update snapshot with signatures but no artifacts")
    names = {path.name for path in files}
    renamed = sorted(name for name in names if not ASSET_NAME.fullmatch(name))
    if renamed:
        raise ValueError("GitHub would rename release assets: " + ", ".join(renamed))
    missing, errors = signature_gaps(assets_dir)
    if not complete or missing:
        raise ValueError("refusing to update snapshot from an incomplete collection; existing "
                         "snapshot metadata and assets were retained" + "".join("; " + e for e in errors))
    gh = github or GitHub()
    repo = f"repos/{repository}"
    ref = gh.api("GET", f"{repo}/git/ref/tags/{tag}", optional=True)
    release = gh.api("GET", f"{repo}/releases/tags/{tag}", optional=True)
    published = ref["object"] if ref else None
    # Annotated tags point to tag objects, not directly to commits.
    for _ in range(8):
        if not published or published["type"] != "tag":
            break
        published = gh.api("GET", f"{repo}/git/tags/{published['sha']}")["object"]
    if published and published["type"] != "commit":
        raise ValueError("snapshot tag does not resolve to a commit")
    if published and published["sha"] != sha:
        comparison = gh.api("GET", f"{repo}/compare/{published['sha']}...{sha}")
        if comparison["status"] == "behind":
            print("Snapshot already names a newer commit; skipping this older build.")
            return False
        if comparison["status"] not in ("ahead", "identical"):
            raise ValueError("refusing to replace snapshot with a divergent commit")
    if release:
        marker = re.search(r"<!-- snapshot-publish (\d+) (\d+) ([0-9a-f]{40}) -->",
                           release.get("body") or "")
        if marker and marker[3] == sha and (int(marker[1]), int(marker[2])) > (run_id, attempt):
            print("Snapshot already names a newer run/attempt of this commit; skipping.")
            return False

    body = (f"Snapshot from [{sha[:8]}](https://github.com/{repository}/commit/{sha}).\n\n"
            f"[Publish run {run_id}, attempt {attempt}]"
            f"(https://github.com/{repository}/actions/runs/{run_id}/attempts/{attempt}).\n\n"
            f"<!-- snapshot-publish {run_id} {attempt} {sha} -->")
    if not release:
        # The first snapshot is a draft until its upload is complete: no public release and no tag
        # that names this build before its assets are there. A draft a failed run left behind is
        # reused (the tag lookup above does not see drafts).
        release = next((r for r in gh.api("GET", f"{repo}/releases?per_page=100") or []
                        if r.get("draft") and r.get("tag_name") == tag), None)
        if not release:
            release = gh.api("POST", f"{repo}/releases", {
                "tag_name": tag, "target_commitish": sha, "name": title, "prerelease": True,
                "draft": True, "body": "Snapshot upload in progress."
            })
    gh.command("release", "upload", tag, *files, "--repo", repository, "--clobber")

    if ref:
        try:
            gh.api("PATCH", f"{repo}/git/refs/tags/{tag}", {"sha": sha, "force": True})
        except RuntimeError as error:
            if "HTTP 403" not in str(error):
                raise
            # GitHub refused this move with 403 "Resource not accessible by integration" in
            # java-llama.cpp run 40227501358 (12 changed workflow files between the two commits;
            # presumably the `workflows` permission, which the workflow token cannot hold). Delete
            # the tag and create it again at a commit the repository already has; the release
            # PATCH below points the release at it again.
            print(f"Moving tag {tag} was refused; deleting and re-creating it at {sha}.")
            gh.api("DELETE", f"{repo}/git/refs/tags/{tag}")
            gh.api("POST", f"{repo}/git/refs", {"ref": f"refs/tags/{tag}", "sha": sha})
    else:
        gh.api("POST", f"{repo}/git/refs", {"ref": f"refs/tags/{tag}", "sha": sha})
    gh.api("PATCH", f"{repo}/releases/{release['id']}", {
        "tag_name": tag, "target_commitish": sha, "name": title, "prerelease": True, "draft": False,
        "body": body
    })

    # Read every page before deleting: deleting while paginating could skip shifted entries.
    assets = []
    page = 1
    while True:
        batch = gh.api("GET", f"{repo}/releases/{release['id']}/assets?per_page=100&page={page}")
        assets.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    failures = []
    for asset in assets:
        if asset["name"] not in names:
            try:
                print(f"Removing obsolete snapshot asset: {asset['name']}")
                gh.api("DELETE", f"{repo}/releases/assets/{asset['id']}", optional=True)
            except RuntimeError as error:
                failures.append(str(error))
    if failures:
        raise RuntimeError("; ".join(failures))
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 -m actionlib.common.snapshot",
                                     description="Upload a directory of signed files as the rolling snapshot release "
                                                 "(requires the serialized snapshot job and ASSETS_COMPLETE=true).")
    parser.add_argument("directory")
    parser.add_argument("--tag", default="snapshot")
    parser.add_argument("--title", default="Snapshot (latest)")
    args = parser.parse_args(argv)
    try:
        publish(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_SHA"],
                os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"], args.directory,
                os.environ.get("ASSETS_COMPLETE") == "true", tag=args.tag, title=args.title)
        return 0
    except (ValueError, OSError, RuntimeError, KeyError, subprocess.CalledProcessError) as error:
        print(f"::error::{error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
