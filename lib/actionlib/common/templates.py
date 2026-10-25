# SPDX-FileCopyrightText: 2026 Bernard Ladenthin <bernard.ladenthin@gmail.com>
#
# SPDX-License-Identifier: MIT OR Apache-2.0
"""The files a repository must carry itself, held to the templates of this repository.

Community files, licenses, editor and Maven settings, Dependabot, the scorecard workflow and the
thin callers of the reusable workflows have to exist in each repository -- GitHub, Maven, REUSE
and the editors read them there. They live once in ``templates/<set>/`` here, at the path they
take in a repository, and every repository's ``check-templates`` step compares its copies with
them. This replaced the per-repository ``shared-files.sha256`` manifests (one hash list per
repository, kept in step by hand).

Configured in the ``templates`` section of ``.github/actions-config.json``:

- ``sets``: the template sets the repository uses (``common``, ``java``, ...), in order;
- ``exclude``: template paths the repository does not carry (each must name a template);
- ``relocate``: template path -> the path in this repository (``lombok.config`` ->
  ``llama/lombok.config``).

Two things may differ without failing the check:

- ``{{owner}}`` and ``{{repo}}`` in a template stand for the repository's owner and name;
- the ref after ``@`` in a ``uses:`` line (``uses: actions/checkout@<sha> # v7``): Dependabot
  bumps the pins of each repository on its own, so pins are compared as ``<ref>``.

``--write`` writes the templates into the repository instead (a new repository, or after a
template changed); pins are then whatever the template says and are re-pinned by Dependabot.
"""

import argparse
import difflib
import re
import sys
from pathlib import Path

from . import config as configuration

TEMPLATES = Path(__file__).resolve().parents[3] / "templates"
PIN = re.compile(rb"""(uses:[ \t]*["']?[^\s@"']+@)[^\s"'#]+(["']?)([ \t]*#[^\n]*)?""")
DEFAULTS = {"sets": [], "exclude": [], "relocate": {}}


def available_sets(templates=TEMPLATES):
    return sorted(path.name for path in Path(templates).iterdir() if path.is_dir())


def template_files(sets, templates=TEMPLATES):
    """Template path -> file, over the given sets; a path in two sets is an error."""
    files = {}
    for name in sets:
        folder = Path(templates) / name
        if not folder.is_dir():
            raise ValueError(f"no template set {name!r}; available: {', '.join(available_sets(templates))}")
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                relative = path.relative_to(folder).as_posix()
                if relative in files:
                    raise ValueError(f"{relative} is in more than one of the sets {', '.join(sets)}")
                files[relative] = path
    return files


def render(data, owner, repo):
    if owner:
        data = data.replace(b"{{owner}}", owner.encode())
    return data.replace(b"{{repo}}", repo.encode())


def normalize(data):
    return PIN.sub(rb"\1<ref>\2", data.replace(b"\r\n", b"\n"))


def plan(root, settings, templates=TEMPLATES):
    """(template path, repository path, template file) for every template the repository carries."""
    files = template_files(configuration.strings(settings["sets"], "templates.sets"), templates)
    exclude = configuration.strings(settings["exclude"], "templates.exclude")
    relocate = settings["relocate"]
    if not isinstance(relocate, dict) or any(not isinstance(v, str) for v in relocate.values()):
        raise ValueError("templates.relocate: expected an object of paths")
    stale = sorted((set(exclude) | set(relocate)) - set(files))
    if stale:
        raise ValueError(f"templates: exclude/relocate name no template: {', '.join(stale)}")
    result = []
    for relative, path in files.items():
        if relative not in exclude:
            target = relocate.get(relative, relative)
            configuration.inside(root, target, "template target")
            result.append((relative, target, path))
    return result


def check(root, settings, owner, repo, templates=TEMPLATES):
    """Problems as strings; [] when every carried file equals its template."""
    problems = []
    for relative, target, path in plan(root, settings, templates):
        local = Path(root) / target
        if not local.is_file():
            problems.append(f"{target} is missing (template {relative})")
            continue
        expected = normalize(render(path.read_bytes(), owner, repo))
        actual = normalize(local.read_bytes())
        if expected != actual:
            diff = difflib.unified_diff(expected.decode("utf-8", "replace").splitlines(),
                                        actual.decode("utf-8", "replace").splitlines(),
                                        f"template {relative}", target, lineterm="", n=1)
            shown = "\n".join(list(diff)[:12])
            problems.append(f"{target} differs from template {relative}:\n{shown}")
    return problems


def write(root, settings, owner, repo, templates=TEMPLATES):
    written = []
    for relative, target, path in plan(root, settings, templates):
        local = Path(root) / target
        data = render(path.read_bytes(), owner, repo)
        if not local.is_file() or local.read_bytes() != data:
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(data)
            written.append(target)
    return written


def main(argv=None, out=sys.stdout, err=sys.stderr):
    parser = argparse.ArgumentParser(prog="python3 -m actionlib.common.templates", description=__doc__.split("\n")[0])
    parser.add_argument("--root", default=".", help="the repository (default: the current directory)")
    parser.add_argument("--config", help=f"configuration file (default: {configuration.DEFAULT_PATH})")
    parser.add_argument("--write", action="store_true", help="write the templates instead of checking")
    args = parser.parse_args(argv)
    try:
        settings = configuration.section(configuration.load(args.root, args.config, required=True),
                                         "templates", DEFAULTS)
        owner, repo = configuration.repository(args.root)
        if args.write:
            for target in write(args.root, settings, owner, repo):
                print(f"wrote {target}", file=out)
            return 0
        problems = check(args.root, settings, owner, repo)
    except (ValueError, OSError) as error:
        print(f"::error::{error}", file=err)
        return 1
    for problem in problems:
        print(f"::error::{problem}", file=err)
    count = len(plan(args.root, settings))
    print(f"{count} files compared with the templates of {', '.join(settings['sets'])}, "
          f"{len(problems)} problems", file=out)
    return 1 if problems or count == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
