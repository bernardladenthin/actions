# SPDX-FileCopyrightText: 2026 Bernard Ladenthin <bernard.ladenthin@gmail.com>
#
# SPDX-License-Identifier: MIT OR Apache-2.0
"""Every Dependabot update of a repository runs on one schedule and opens grouped pull requests.

Dependabot reads ``.github/dependabot.yml`` from each repository's default branch and cannot take
it from anywhere else, so every repository carries its own -- and the files differ (the Java
repositories one Maven build, BroomCabinet sixteen Maven and three pip projects, the others only
GitHub Actions). What they share is the rule, and it lives here: every entry of ``updates`` runs
``interval: weekly`` on ``day: saturday`` and has at least one entry in ``groups`` (or names a
``multi-ecosystem-group``), so a week of updates arrives as one pull request per ecosystem, one
merge and one run of the pipeline on main, instead of one per dependency.

Configured in the ``dependabot`` section of ``.github/actions-config.json``: ``interval``
(default ``weekly``), ``day`` (default ``saturday``; ``null`` for no rule on the day) and
``groups`` (default ``true``). A repository without the file fails: a repository that runs the
check has said that it uses Dependabot.

The file is read with a small reader for the block style these files are written in (mappings,
sequences, ``[a, "b"]`` flow sequences, quoted and plain scalars, comments). Anything else --
anchors, tags, block scalars, flow mappings -- fails loudly instead of being guessed at.
"""

import argparse
import os
import re
import sys

from . import config as configuration

PATHS = (".github/dependabot.yml", ".github/dependabot.yaml")
DEFAULTS = {"interval": "weekly", "day": "saturday", "groups": True}
UNSUPPORTED = re.compile(r"^(?:[&*!|>{]|---|\.\.\.)")


class YamlError(ValueError):
    pass


def _strip_comment(text):
    """The line without its comment: a # at the start or after a space, outside quotes."""
    quote = None
    for i, char in enumerate(text):
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i].rstrip()
    return text.rstrip()


def _lines(text):
    lines = []
    for number, raw in enumerate(text.splitlines(), 1):
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise YamlError(f"line {number}: a tab in the indentation")
        content = _strip_comment(raw)
        if content.strip():
            lines.append((number, len(content) - len(content.lstrip(" ")), content.strip()))
    return lines


def _split_key(content, number):
    """(key, value text) of a `key: value` line, else None. The colon must be outside quotes."""
    quote = None
    for i, char in enumerate(content):
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == ":" and (i + 1 == len(content) or content[i + 1] == " "):
            key = _scalar(content[:i].strip(), number)
            return key, content[i + 1:].strip()
    return None


def _scalar(text, number):
    if not text:
        return None
    if UNSUPPORTED.match(text):
        raise YamlError(f"line {number}: cannot read {text!r} (only block mappings, sequences and plain values)")
    if text[0] in "\"'":
        if len(text) < 2 or text[-1] != text[0]:
            raise YamlError(f"line {number}: unterminated quote in {text!r}")
        return text[1:-1]
    if text[0] == "[":
        if text[-1] != "]":
            raise YamlError(f"line {number}: cannot read {text!r}")
        inner = text[1:-1].strip()
        return [_scalar(item.strip(), number) for item in _split_flow(inner)] if inner else []
    return text


def _split_flow(inner):
    items, quote, start = [], None, 0
    for i, char in enumerate(inner):
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == ",":
            items.append(inner[start:i])
            start = i + 1
    items.append(inner[start:])
    return items


def _block(lines, index, indent):
    """The value of the block that starts at lines[index] with this indentation, and the next index."""
    number, _, content = lines[index]
    if content == "-" or content.startswith("- "):
        return _sequence(lines, index, indent)
    if _split_key(content, number) is None:
        raise YamlError(f"line {number}: expected `key: value`, found {content!r}")
    return _mapping(lines, index, indent)


def _mapping(lines, index, indent, first=None):
    result = {}
    while index < len(lines) or first is not None:
        if first is not None:
            number, content = first
            first = None
        else:
            number, line_indent, content = lines[index]
            if line_indent < indent:
                break
            if line_indent > indent:
                raise YamlError(f"line {number}: unexpected indentation")
            if content == "-" or content.startswith("- "):
                break
            index += 1
        split = _split_key(content, number)
        if split is None:
            raise YamlError(f"line {number}: expected `key: value`, found {content!r}")
        key, value = split
        if key in result:
            raise YamlError(f"line {number}: {key!r} twice")
        if value:
            result[key] = _scalar(value, number)
        elif index < len(lines) and (lines[index][1] > indent or
                                     (lines[index][1] == indent and lines[index][2].startswith("-"))):
            result[key], index = _block(lines, index, lines[index][1])
        else:
            result[key] = None
    return result, index


def _sequence(lines, index, indent):
    result = []
    while index < len(lines):
        number, line_indent, content = lines[index]
        if line_indent != indent or not (content == "-" or content.startswith("- ")):
            if line_indent > indent:
                raise YamlError(f"line {number}: unexpected indentation")
            break
        index += 1
        item = content[1:].strip()
        if not item:
            if index < len(lines) and lines[index][1] > indent:
                value, index = _block(lines, index, lines[index][1])
            else:
                value = None
        elif _split_key(item, number) is not None:
            value, index = _mapping(lines, index, indent + 2, first=(number, item))
        else:
            value = _scalar(item, number)
        result.append(value)
    return result, index


def parse(text):
    """The document as dicts, lists and strings. Raises YamlError on what it does not read."""
    lines = _lines(text)
    if not lines:
        return None
    value, index = _block(lines, 0, lines[0][1])
    if index < len(lines):
        raise YamlError(f"line {lines[index][0]}: unexpected content after the document")
    return value


def _where(number, update):
    ecosystem = update.get("package-ecosystem", "?") if isinstance(update, dict) else "?"
    return f"updates[{number}] ({ecosystem})"


def check(document, interval=DEFAULTS["interval"], day=DEFAULTS["day"], groups=DEFAULTS["groups"]):
    """The failures of one parsed dependabot.yml, and the number of update entries."""
    if not isinstance(document, dict) or not isinstance(document.get("updates"), list) or not document["updates"]:
        return ["has no `updates` entries"], 0
    failures = []
    for number, update in enumerate(document["updates"]):
        where = _where(number, update)
        if not isinstance(update, dict):
            failures.append(f"{where} is not a mapping")
            continue
        schedule = update.get("schedule")
        schedule = schedule if isinstance(schedule, dict) else {}
        if interval is not None and schedule.get("interval") != interval:
            failures.append(f"{where}: schedule.interval is {schedule.get('interval')!r}; expected {interval!r}")
        if day is not None and schedule.get("day") != day:
            failures.append(f"{where}: schedule.day is {schedule.get('day')!r}; expected {day!r}")
        if groups:
            named = update.get("groups")
            if not update.get("multi-ecosystem-group") and not (isinstance(named, dict) and named):
                failures.append(f"{where} has no groups: every update of the week would be a pull request "
                                f"of its own")
    return failures, len(document["updates"])


def main(argv=None, out=sys.stdout, err=sys.stderr):
    parser = argparse.ArgumentParser(prog="python3 -m actionlib.common.dependabot",
                                     description="every Dependabot update runs on one schedule, grouped")
    parser.add_argument("--root", default=".", help="the repository (default: the current directory)")
    parser.add_argument("--config", help=f"configuration file (default: {configuration.DEFAULT_PATH})")
    args = parser.parse_args(argv)
    try:
        settings = configuration.section(configuration.load(args.root, args.config), "dependabot", dict(DEFAULTS))
        for key in ("interval", "day"):
            if settings[key] is not None and (not isinstance(settings[key], str) or not settings[key].strip()):
                raise ValueError(f"dependabot.{key}: expected a non-empty string or null")
        if not isinstance(settings["groups"], bool):
            raise ValueError("dependabot.groups: expected true or false")
    except (ValueError, OSError) as error:
        print(f"::error::{error}", file=err)
        return 2
    path = next((p for p in PATHS if os.path.isfile(os.path.join(args.root, p))), None)
    if path is None:
        print(f"::error::no Dependabot configuration ({' or '.join(PATHS)})", file=err)
        return 1
    with open(os.path.join(args.root, path), encoding="utf-8") as f:
        text = f.read()
    try:
        document = parse(text)
    except YamlError as error:
        print(f"::error::{path}: {error}", file=err)
        return 1
    failures, count = check(document, settings["interval"], settings["day"], settings["groups"])
    for failure in failures:
        print(f"::error::{path}: {failure}", file=err)
    print(f"{path}: {count} update entries, {len(failures)} problems", file=out)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
