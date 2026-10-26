# SPDX-FileCopyrightText: 2026 Bernard Ladenthin <bernard.ladenthin@gmail.com>
#
# SPDX-License-Identifier: MIT OR Apache-2.0
"""The per-repository configuration of the shared actions: ``.github/actions-config.json``.

One JSON object, one key per check, each optional where the check has defaults::

    {
      "templates": {"sets": ["common", "java"], "exclude": [...], "relocate": {"lombok.config": "llama/lombok.config"}},
      "release-gate": {"workflow": ".github/workflows/publish.yml", "gates": [...], "exemptions": "..."},
      "run-scripts": {"patterns": [".github/workflows/*.yml", ...]},
      "maven-versions": {"poms": ["pom.xml"], "family": "java-maven", "ignore-groups": ["net.ladenthin"]},
      "publication-wiring": {"workflow": ".github/workflows/publish.yml"},
      "post-release": {"signing-key": "<fingerprint>", "bytecode": [{"files": "*", "major": 52}]}
    }

Unknown keys fail, at every level: a misspelt key would otherwise switch a check back to its
defaults without a word. The file is part of the repository; a path outside it is refused.
"""

import json
import os
import re
import subprocess
from pathlib import Path

DEFAULT_PATH = ".github/actions-config.json"
SECTIONS = ("templates", "release-gate", "run-scripts", "maven-versions", "publication-wiring",
            "post-release")
REMOTE = re.compile(r"[:/]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")


def inside(root, relative, what="path"):
    """`relative` resolved below `root`; absolute paths and escapes are refused."""
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError(f"expected a repository-relative {what}: {relative!r}")
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if path != root and root not in path.parents:
        raise ValueError(f"{what} escapes the repository: {relative}")
    return path


def load(root, path=None, required=False):
    """The configuration object; {} when the default file is absent and nothing requires it."""
    file = inside(root, path or DEFAULT_PATH, "configuration file")
    if not file.is_file():
        if required or path:
            raise ValueError(f"configuration file {path or DEFAULT_PATH} not found")
        return {}
    config = json.loads(file.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError(f"{path or DEFAULT_PATH}: expected a JSON object")
    unknown = sorted(set(config) - set(SECTIONS) - {"$comment"})
    if unknown:
        raise ValueError(f"{path or DEFAULT_PATH}: unknown section(s) {', '.join(unknown)}; "
                         f"known: {', '.join(SECTIONS)}")
    return config


def section(config, name, defaults, required=()):
    """One section merged over its defaults; unknown or missing required keys fail."""
    value = config.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"{name}: expected an object")
    unknown = sorted(set(value) - set(defaults) - set(required) - {"$comment"})
    if unknown:
        raise ValueError(f"{name}: unknown key(s) {', '.join(unknown)}; "
                         f"known: {', '.join(sorted(set(defaults) | set(required)))}")
    missing = [key for key in required if key not in value]
    if missing:
        raise ValueError(f"{name}: missing {', '.join(missing)}")
    merged = dict(defaults)
    merged.update({k: v for k, v in value.items() if k != "$comment"})
    return merged


def strings(value, what):
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"{what}: expected a list of strings")
    return value


def repository(root):
    """(owner, name) of the repository: GITHUB_REPOSITORY, else the origin remote, else the folder."""
    env = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in env:
        owner, name = env.split("/", 1)
        return owner, name
    try:
        url = subprocess.run(["git", "-C", str(root), "remote", "get-url", "origin"],
                             capture_output=True, text=True, check=True).stdout.strip()
        match = REMOTE.search(url)
        if match:
            return match.group(1), match.group(2)
    except (OSError, subprocess.CalledProcessError):
        pass
    return None, Path(root).resolve().name
