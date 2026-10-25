# SPDX-FileCopyrightText: 2026 Bernard Ladenthin <bernard.ladenthin@gmail.com>
#
# SPDX-License-Identifier: MIT OR Apache-2.0
"""Detached signatures next to the files they sign: every artifact has a nonempty `<name>.asc`."""

from pathlib import Path


def signature_gaps(directory):
    """Presence/size check for every file type; signing itself verifies cryptographic validity."""
    directory = Path(directory)
    files = sorted(directory.iterdir()) if directory.is_dir() else []
    artifacts = [path for path in files if not path.name.endswith(".asc")]
    if not artifacts:
        return -1, [f"no artifacts in {directory} -- the collection step produced nothing"]
    errors = []
    for path in artifacts:
        signature = Path(str(path) + ".asc")
        if not path.is_file() or path.stat().st_size == 0:
            errors.append(f"missing or empty artifact: {path.name}")
        elif not signature.is_file() or signature.stat().st_size == 0:
            errors.append(f"unsigned: {path.name} has no nonempty detached .asc")
    for path in files:
        if path.name.endswith(".asc") and not path.with_suffix("").is_file():
            errors.append(f"orphan signature: {path.name}")
    return len(errors), errors
