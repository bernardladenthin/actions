# SPDX-FileCopyrightText: 2026 Bernard Ladenthin <bernard.ladenthin@gmail.com>
#
# SPDX-License-Identifier: MIT OR Apache-2.0
"""No escape hatch in a machine-checked proof or specification: no construct that makes the checker
accept what it did not prove -- a placeholder proof, an axiom nobody proved, an assumed contract, a
switched-off check.

The case this exists for: a proof that is green but proves nothing. A `sorry` left in a Lean proof,
an `axiom` injected next to the theorem it then "proves", a `PROOF OMITTED` in a TLAPS module, an
`assume` in a JML contract -- each is accepted by its checker, often with only a warning in a log
nobody reads, and the build stays green. Such constructs are legitimate while a proof is being
written; in a merged proof each one is either a defect or a decision, and a decision is written down
as an exemption with its reason.

What it catches, per language (the table ``TOKENS``; a language is an entry of ``LANGUAGES``):

- TLA+ / TLAPS (``.tla``): ``OMITTED``, ``AXIOM``, ``ASSUME FALSE``, ``ASSUMPTION FALSE``. Only the
  text between the first ``---- MODULE`` line and the last ``====`` line counts, as for SANY.
- Lean 4 (``.lean``): ``sorry``, ``sorryAx``, ``admit``, ``axiom``, ``native_decide``,
  ``ofReduceBool``, ``implemented_by``, ``extern``, ``unsafe``, ``debug.skipKernelTC``.
- Coq / Rocq (``.v``): ``Admitted``, ``admit``, ``Admit Obligations``, ``Axiom``, ``Axioms``,
  ``Parameter``, ``Parameters``, ``Conjecture``, ``Unset Guard Checking``, ``Unset Positivity
  Checking``, ``Unset Universe Checking``, ``bypass_check``. ``.v`` is also Verilog: a file counts
  as Coq only when it carries a Coq marker (``Require Import``, ``Theorem``, ``Lemma``,
  ``Definition``, ``Proof.``, ``Qed.``, ...); the others are skipped and counted.
- Isabelle (``.thy``): ``sorry``, ``oops``, ``axiomatization``, ``quick_and_dirty``.
- Dafny (``.dfy``): ``assume``, ``{:axiom}``, ``{:verify false}``, ``requires false``.
- JML (``.jml``, and the JML annotations of ``.java``): ``assume``, ``skipesc``, ``skiprac``,
  ``nowarn``, ``axiom``, ``requires false`` inside ``//@`` and ``/*@ ... @*/`` annotations (keyed
  ones like ``//+OPENJML@`` too); in the Java code around them only the OpenJML annotations
  ``@SkipEsc`` and ``@SkipRac``. A ``.java`` file counts as scanned only when it has an annotation.

A token counts as a whole word, outside comments and string literals, with each language's comment
syntax (nested block comments where the language nests them) and its literals (Lean raw strings and
character literals, Dafny verbatim strings, Isabelle cartouches, Java text blocks). A comment or
string that does not end is an error: the rest of the file could not be read the way its checker
reads it, and a scan that silently stops is the false pass this check is against.

What it cannot catch: this is the syntactic class only. A definition that is vacuous (a predicate
defined as ``True``, an invariant implied by anything), a specification that states something else
than what was meant, a theorem proved about the wrong model -- all of that passes every checker and
this check. Those need negative controls: mutants of the specification or the code that the proof
must reject (a proof that still passes after the property was made false proves nothing). Also out
of reach: escapes built by metaprogramming (a Lean macro or an Isabelle ``ML`` block that creates
the axiom or oracle), the checker's command-line flags, a TLAPS step with no proof at all, a Coq
``Hypothesis`` or ``Variable`` outside a section, a Dafny method without a body. The authoritative
check is the checker's own account of what a theorem rests on: ``#print axioms`` and lean4checker
for Lean, ``Print Assumptions`` for Coq, tlapm's count of omitted obligations for TLAPS, Isabelle
without ``quick_and_dirty``; this check is the cheap first line in every repository, not their
replacement.

The files are the tracked files of the repository (``git ls-files``; a walk when it is not a git
repository) with an extension of an enabled language. Configured in the ``proof-escapes`` section
of ``.github/actions-config.json``: ``languages`` (default: all of ``LANGUAGES``; when given, each
listed language must have a file), ``exclude`` (globs relative to the repository, ``**`` for any
depth; default none) and ``exempt``, a list of ``{"file", "token", "count", "reason"}``. An
exemption matches exactly: that file, that token, exactly ``count`` occurrences, and a reason that
says why. More or fewer occurrences fail (a new escape cannot hide behind an old exemption), and an
exemption that matches nothing fails as stale.

Exit 1 on an escape or an exemption that does not match, 2 on a configuration error or when no file
of an enabled language exists -- an empty scan is a failure, never a pass.
"""

import argparse
import bisect
import os
import re
import subprocess
import sys

from . import config as configuration

BEFORE = r"(?<![\w'])"
AFTER = r"(?![\w'])"
# `requires false || p` is not vacuous; `requires false` followed by a clause or the end is
NOT_AN_OPERAND = r"(?!\s*(?:\|\||&&|==|!=|<==))"
# the same in TLA+, whose module ends in `====`
NOT_A_TLA_OPERAND = r"(?!\s*(?:\\/|/\\|=>|<=>|#|/=|=(?!===)))"


def _phrase(phrase, tail=""):
    return BEFORE + r"\s+".join(re.escape(word) for word in phrase.split()) + AFTER + tail


class Token:
    """One escape: its language, its name (what an exemption names), why it leaves a proof unchecked,
    and where it counts -- in the specification text (``spec``) or, for JML, in the Java code."""

    def __init__(self, language, name, why, pattern=None, scope="spec"):
        self.language = language
        self.name = name
        self.why = why
        self.regex = re.compile(pattern or _phrase(name))
        self.scope = scope


FALSE_ASSUMED = "an assumption of FALSE makes every theorem provable"
VACUOUS = "a precondition no caller can meet makes the body verified vacuously"
TOKENS = (
    Token("tla", "OMITTED", "the proof is omitted; TLAPS accepts the step unproved"),
    Token("tla", "AXIOM", "asserts a fact that nothing proves; an inconsistent one proves anything"),
    Token("tla", "ASSUME FALSE", FALSE_ASSUMED, _phrase("ASSUME FALSE", NOT_A_TLA_OPERAND)),
    Token("tla", "ASSUMPTION FALSE", FALSE_ASSUMED, _phrase("ASSUMPTION FALSE", NOT_A_TLA_OPERAND)),
    Token("lean", "sorry", "a placeholder proof; Lean accepts the theorem through the sorryAx axiom"),
    Token("lean", "sorryAx", "the axiom behind sorry, used directly"),
    Token("lean", "admit", "closes the goal with sorry"),
    Token("lean", "axiom", "declares a fact without proof; an inconsistent one proves anything"),
    Token("lean", "native_decide", "trusts the compiler (Lean.ofReduceBool) instead of the kernel"),
    Token("lean", "ofReduceBool", "the axiom behind native_decide: trusts compiled code instead of the kernel"),
    Token("lean", "implemented_by", "the compiled code replaces the definition the proofs are about"),
    Token("lean", "extern", "the compiled code is foreign code, not the definition the proofs are about"),
    Token("lean", "unsafe", "an unsafe declaration is not checked by the kernel"),
    Token("lean", "debug.skipKernelTC", "the kernel does not type-check the declarations"),
    Token("coq", "Admitted", "the proof is abandoned and the statement becomes an axiom"),
    Token("coq", "admit", "closes the goal without proof"),
    Token("coq", "Admit Obligations", "the open obligations become axioms"),
    Token("coq", "Axiom", "declares a fact without proof; an inconsistent one proves anything"),
    Token("coq", "Axioms", "declares facts without proof; an inconsistent one proves anything"),
    Token("coq", "Parameter", "assumes an inhabitant of a type without defining it"),
    Token("coq", "Parameters", "assumes inhabitants of types without defining them"),
    Token("coq", "Conjecture", "declares a fact without proof"),
    Token("coq", "Unset Guard Checking", "non-terminating fixpoints are accepted and prove False"),
    Token("coq", "Unset Positivity Checking", "non-positive inductive types are accepted and prove False"),
    Token("coq", "Unset Universe Checking", "universe inconsistencies are accepted and prove False"),
    Token("coq", "bypass_check", "switches off the guard, positivity or universe check of a definition"),
    Token("isabelle", "sorry", "a placeholder proof, accepted through the skip_proof oracle"),
    Token("isabelle", "oops", "the proof is abandoned unfinished"),
    Token("isabelle", "axiomatization", "asserts axioms; an inconsistent one proves anything"),
    Token("isabelle", "quick_and_dirty", "lets sorry and skipped proofs pass"),
    Token("dafny", "assume", "the verifier takes the condition as true without proof"),
    Token("dafny", "{:axiom}", "the declaration or assumption is accepted without proof",
          r"\{\s*:\s*axiom\b[^}]*\}"),
    Token("dafny", "{:verify false}", "the verifier skips the declaration", r"\{\s*:\s*verify\s+false\s*\}"),
    Token("dafny", "requires false", VACUOUS, _phrase("requires false", NOT_AN_OPERAND)),
    Token("jml", "assume", "the verifier takes the condition as true without proof"),
    Token("jml", "skipesc", "OpenJML skips the static check of the method"),
    Token("jml", "skiprac", "the runtime assertion check skips the method"),
    Token("jml", "nowarn", "suppresses the verifier's warning instead of proving its condition"),
    Token("jml", "axiom", "asserts a fact without proof; an inconsistent one proves anything"),
    Token("jml", "requires false", VACUOUS, _phrase("requires false", NOT_AN_OPERAND)),
    Token("jml", "@SkipEsc", "OpenJML skips the static check of the method",
          r"@\s*(?:[\w$]+\s*\.\s*)*SkipEsc\b", scope="code"),
    Token("jml", "@SkipRac", "the runtime assertion check skips the method",
          r"@\s*(?:[\w$]+\s*\.\s*)*SkipRac\b", scope="code"),
)


# ---------------------------------------------------------------------------------------------
# Lexing: each language's text with its comments and literals blanked (newlines kept, so a match
# keeps its line). A literal reader takes (text, start) and returns (end, terminated), or None
# when no literal starts there.

def _quoted(quote, backslash=True, doubled=False, multiline=True):
    def read(text, start):
        if not text.startswith(quote, start):
            return None
        i = start + len(quote)
        while i < len(text):
            char = text[i]
            if backslash and char == "\\":
                i += 2
                continue
            if doubled and text.startswith(quote * 2, i):
                i += 2 * len(quote)
                continue
            if text.startswith(quote, i):
                return i + len(quote), True
            if char == "\n" and not multiline:
                return i, True  # the compiler rejects it; nothing after the line is hidden
            i += 1
        return len(text), False
    return read


def _prefixed(prefix, reader):
    def read(text, start):
        if not text.startswith(prefix, start) or (start and (text[start - 1].isalnum() or text[start - 1] in "_'")):
            return None
        return reader(text, start + len(prefix))
    return read


LEAN_RAW = re.compile(r'r(#*)"')


def _lean_raw(text, start):
    match = LEAN_RAW.match(text, start)
    if not match or (start and (text[start - 1].isalnum() or text[start - 1] in "_'.")):
        return None
    close = '"' + match.group(1)
    end = text.find(close, match.end())
    return (len(text), False) if end < 0 else (end + len(close), True)


CHAR_LITERAL = re.compile(r"'(?:\\(?:x[0-9a-fA-F]{2}|u\{[0-9a-fA-F]+\}|u[0-9a-fA-F]{4}|.)|[^\\'\n])'")


def _char(text, start):
    """A character literal ('a', '\\n', '"'); a quote after an identifier character is a prime."""
    if text[start] != "'" or (start and (text[start - 1].isalnum() or text[start - 1] in "_'")):
        return None
    match = CHAR_LITERAL.match(text, start)
    return (match.end(), True) if match else None


def _nested(opening, closing):
    pattern = re.compile(re.escape(opening) + "|" + re.escape(closing))

    def read(text, start):
        if not text.startswith(opening, start):
            return None
        depth = 0
        i = start
        while True:
            match = pattern.search(text, i)
            if not match:
                return len(text), False
            depth += 1 if match.group() == opening else -1
            i = match.end()
            if depth == 0:
                return i, True
    return read


def _line(prefix):
    def read(text, start):
        if not text.startswith(prefix, start):
            return None
        end = text.find("\n", start)
        return (len(text) if end < 0 else end), True
    return read


def _block(opening, closing, nests):
    if nests:
        return _nested(opening, closing)

    def read(text, start):
        if not text.startswith(opening, start):
            return None
        end = text.find(closing, start + len(opening))
        return (len(text), False) if end < 0 else (end + len(closing), True)
    return read


class Syntax:
    """The comments and literals of a language, each a reader, tried in order at each position the
    `starts` pattern finds."""

    def __init__(self, starts, comments, literals):
        self.starts = re.compile(starts)
        self.readers = [(reader, "comment") for reader in comments] + [(reader, "string") for reader in literals]


def _blank(chars, start, end):
    for i in range(start, end):
        if chars[i] != "\n":
            chars[i] = " "


def lex(text, syntax):
    """The text with comments and literals blanked, and (offset, what) per one that does not end."""
    chars = list(text)
    problems = []
    i = 0
    while True:
        match = syntax.starts.search(text, i)
        if not match:
            break
        i = match.start()
        for reader, what in syntax.readers:
            found = reader(text, i)
            if found:
                end, terminated = found
                if not terminated:
                    problems.append((i, what))
                _blank(chars, i, end)
                i = end
                break
        else:
            i += 1
    return "".join(chars), problems


TLA = Syntax(r'\\\*|\(\*|"', [_line("\\*"), _nested("(*", "*)")], [_quoted('"')])
LEAN = Syntax(r"--|/-|r#*\"|[\"']", [_line("--"), _nested("/-", "-/")], [_quoted('"'), _lean_raw, _char])
COQ = Syntax(r'\(\*|"', [_nested("(*", "*)")], [_quoted('"', backslash=False, doubled=True)])
ISABELLE = Syntax(r'\(\*|"|`|\{\*|\\<open>|‹', [_nested("(*", "*)")],
                  [_quoted('"'), _quoted("`"), _block("{*", "*}", False), _nested("\\<open>", "\\<close>"),
                   _nested("‹", "›")])
DAFNY = Syntax(r"//|/\*|@\"|[\"']", [_line("//"), _nested("/*", "*/")],
               [_prefixed("@", _quoted('"', backslash=False, doubled=True)), _quoted('"'), _char])
# inside a JML annotation: Java strings and characters, and a // comment
JML_INNER = Syntax(r"//|[\"']", [_line("//")], [_quoted('"', multiline=False), _quoted("'", multiline=False)])
JAVA_START = re.compile(r'"""|["\'/]')
ANNOTATION = re.compile(r"(?://|/\*)(?:[+-][A-Za-z0-9_]*)*@")
MODULE_START = re.compile(r"(?m)^-{4,}\s*MODULE\b")
MODULE_END = re.compile(r"(?m)^={4,}\s*$")


def _whole(text, syntax):
    masked, problems = lex(text, syntax)
    return {"spec": masked}, problems, True


def mask_tla(text):
    """SANY reads from the first module header to the last module end; the rest is free text."""
    start = MODULE_START.search(text)
    ends = list(MODULE_END.finditer(text))
    if not start:
        return _whole(text, TLA)
    end = ends[-1].end() if ends and ends[-1].start() > start.start() else len(text)
    outside = list(text)
    _blank(outside, 0, start.start())
    _blank(outside, end, len(text))
    return _whole("".join(outside), TLA)


def mask_java(text):
    """The JML annotations (``spec``) and the Java code around them (``code``), each with the other,
    the ordinary comments and the literals blanked; relevant when there is an annotation."""
    spec = list(text)
    _blank(spec, 0, len(text))
    code = list(text)
    problems = []
    annotated = False
    i = 0
    while True:
        match = JAVA_START.search(text, i)
        if not match:
            break
        i = match.start()
        token = match.group()
        if token == "/" and not text.startswith(("//", "/*"), i):
            i += 1
            continue
        annotation = ANNOTATION.match(text, i)
        if token == '"""':
            close = text.find('"""', i + 3)
            end = len(text) if close < 0 else close + 3
            if close < 0:
                problems.append((i, "string"))
        elif token in "\"'":
            end, _ = _quoted(token, multiline=False)(text, i)
        elif annotation:
            annotated = True
            body = annotation.end()
            if text.startswith("//", i):
                close = text.find("\n", body)
                end = inner_end = len(text) if close < 0 else close
            else:
                close = text.find("*/", body)
                inner_end = len(text) if close < 0 else close
                end = len(text) if close < 0 else close + 2
                if close < 0:
                    problems.append((i, "comment"))
            inner, inner_problems = lex(text[body:inner_end], JML_INNER)
            spec[body:inner_end] = inner
            problems += [(body + offset, what) for offset, what in inner_problems]
        elif text.startswith("//", i):
            close = text.find("\n", i)
            end = len(text) if close < 0 else close
        else:
            close = text.find("*/", i + 2)
            end = len(text) if close < 0 else close + 2
            if close < 0:
                problems.append((i, "comment"))
        _blank(code, i, end)
        i = end
    return {"spec": "".join(spec), "code": "".join(code)}, problems, annotated


COQ_MARKER = re.compile(r"(?m)\bRequire\s+(?:Import|Export)\b|\b(?:Proof|Qed|Admitted|Defined)\.|"
                        r"^\s*(?:Theorem|Lemma|Definition|Fixpoint|Inductive|Axiom|Parameter|Module|Section)\s+\w")


class Language:
    """A language: the file extensions it owns, how its text is masked, and for an extension shared
    with another language (Coq's .v is also Verilog's) what marks a file as this language's."""

    def __init__(self, name, extensions, mask, marker=None):
        self.name = name
        self.extensions = extensions
        self.mask = mask
        self.marker = marker


LANGUAGES = {
    "tla": Language("tla", (".tla",), mask_tla),
    "lean": Language("lean", (".lean",), lambda text: _whole(text, LEAN)),
    "coq": Language("coq", (".v",), lambda text: _whole(text, COQ), COQ_MARKER),
    "isabelle": Language("isabelle", (".thy",), lambda text: _whole(text, ISABELLE)),
    "dafny": Language("dafny", (".dfy",), lambda text: _whole(text, DAFNY)),
    "jml": Language("jml", (".jml", ".java"), mask_java),
}
TOKEN_NAMES = frozenset(token.name for token in TOKENS)


class Finding:
    def __init__(self, path, line, token):
        self.path = path
        self.line = line
        self.token = token

    def __str__(self):
        return f"{self.path}:{self.line}: {self.token.name} ({self.token.language}) -- {self.token.why}"


def scan(path, text, language):
    """(findings, problems, relevant) of one file: the escapes, the comments and literals that do
    not end (as messages), and whether the file counts as one of the language's."""
    masks, problems, relevant = language.mask(text)
    starts = [0] + [match.end() for match in re.finditer("\n", text)]

    def line(offset):
        return bisect.bisect_right(starts, offset)

    findings = []
    for token in TOKENS:
        if token.language != language.name:
            continue
        for match in token.regex.finditer(masks.get(token.scope, "")):
            findings.append(Finding(path, line(match.start()), token))
    findings.sort(key=lambda finding: finding.line)
    messages = [f"{path}:{line(offset)}: unterminated {what} -- the rest of the file cannot be scanned"
                for offset, what in problems]
    return findings, messages, relevant


# ---------------------------------------------------------------------------------------------
# Files, configuration, exemptions.

def _glob(pattern):
    """A glob relative to the repository: `*` and `?` within a path segment, `**` across them."""
    out = ""
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(out + r"\Z")


def files(root):
    """The tracked files of the repository, repository-relative with `/`; all files outside git."""
    try:
        listed = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True,
                                check=True).stdout.decode("utf-8", "surrogateescape")
        paths = [path for path in listed.split("\0") if path]
    except (OSError, subprocess.CalledProcessError):
        paths = []
        for directory, subdirectories, names in os.walk(root):
            subdirectories[:] = [name for name in subdirectories if name != ".git"]
            paths += [os.path.relpath(os.path.join(directory, name), root).replace(os.sep, "/") for name in names]
    return sorted(path for path in paths if os.path.isfile(os.path.join(root, path)))


EXEMPTION_KEYS = {"file", "token", "count", "reason"}


def exemptions(value):
    """The exemptions of the configuration, validated: {(file, token): (count, reason)}."""
    if not isinstance(value, list):
        raise ValueError("proof-escapes.exempt: expected a list of objects")
    result = {}
    for number, entry in enumerate(value):
        where = f"proof-escapes.exempt[{number}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{where}: expected an object")
        keys = set(entry) - {"$comment"}
        if keys != EXEMPTION_KEYS:
            unknown, missing = sorted(keys - EXEMPTION_KEYS), sorted(EXEMPTION_KEYS - keys)
            raise ValueError(f"{where}: " + "; ".join(
                part for part in (unknown and f"unknown key(s) {', '.join(unknown)}",
                                  missing and f"missing {', '.join(missing)}") if part))
        file, token, count, reason = entry["file"], entry["token"], entry["count"], entry["reason"]
        if not isinstance(file, str) or not file:
            raise ValueError(f"{where}: file must be a repository-relative path")
        if token not in TOKEN_NAMES:
            raise ValueError(f"{where}: unknown token {token!r}; known: {', '.join(sorted(TOKEN_NAMES))}")
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise ValueError(f"{where}: count must be a positive whole number")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"{where}: reason must say why this escape is acceptable")
        if (file, token) in result:
            raise ValueError(f"{where}: a second exemption for {token} in {file}")
        result[(file, token)] = (count, reason)
    return result


def apply(findings, exempt):
    """(reported findings, number exempted, exemption errors): an exemption takes its file's
    occurrences of its token only when there are exactly `count` of them."""
    grouped = {}
    for finding in findings:
        grouped.setdefault((finding.path, finding.token.name), []).append(finding)
    exempted = 0
    errors = []
    taken = set()
    for (file, token), (count, _) in sorted(exempt.items()):
        found = grouped.get((file, token), [])
        if not found:
            errors.append(f"{file}: the exemption for {token} matches nothing -- remove it")
        elif len(found) != count:
            errors.append(f"{file}: the exemption for {token} allows {count}, found {len(found)}")
        else:
            exempted += count
            taken.add((file, token))
    reported = [finding for finding in findings if (finding.path, finding.token.name) not in taken]
    return reported, exempted, errors


def settings(root, path):
    """(languages, explicit, exclude patterns, exemptions) from the configuration."""
    loaded = configuration.load(root, path)
    section = configuration.section(loaded, "proof-escapes",
                                    {"languages": list(LANGUAGES), "exclude": [], "exempt": []})
    explicit = "languages" in loaded.get("proof-escapes", {})
    languages = configuration.strings(section["languages"], "proof-escapes.languages")
    unknown = sorted(set(languages) - set(LANGUAGES))
    if unknown:
        raise ValueError(f"proof-escapes.languages: unknown {', '.join(unknown)}; known: {', '.join(LANGUAGES)}")
    if not languages:
        raise ValueError("proof-escapes.languages: empty; omit it for all languages")
    exclude = [_glob(pattern) for pattern in
               configuration.strings(section["exclude"], "proof-escapes.exclude")]
    return [LANGUAGES[name] for name in LANGUAGES if name in languages], explicit, exclude, exemptions(section["exempt"])


def main(argv=None, out=sys.stdout, err=sys.stderr):
    parser = argparse.ArgumentParser(prog="python3 -m actionlib.common.proofescapes",
                                     description="no escape hatch (sorry, axiom, PROOF OMITTED, assume, ...) "
                                                 "in the machine-checked proofs and specifications")
    parser.add_argument("--root", default=".", help="the repository (default: the current directory)")
    parser.add_argument("--config", help=f"configuration file (default: {configuration.DEFAULT_PATH})")
    args = parser.parse_args(argv)
    root = args.root
    try:
        languages, explicit, exclude, exempt = settings(root, args.config)
    except (ValueError, OSError) as error:
        print(f"::error::{error}", file=err)
        return 2
    by_extension = {extension: language for language in languages for extension in language.extensions}
    counts = {language.name: 0 for language in languages}
    skipped = 0
    findings = []
    problems = []
    for path in files(root):
        language = by_extension.get(os.path.splitext(path)[1])
        if language is None or any(pattern.match(path) for pattern in exclude):
            continue
        with open(os.path.join(root, path), encoding="utf-8", errors="replace") as f:
            text = f.read()
        if language.marker and not language.marker.search(text):
            skipped += 1
            continue
        found, messages, relevant = scan(path, text, language)
        if relevant:
            counts[language.name] += 1
        findings += found
        problems += messages
    reported, exempted, errors = apply(findings, exempt)
    for finding in reported:
        print(f"::error::{finding}", file=err)
    for message in problems + errors:
        print(f"::error::{message}", file=err)
    total = sum(counts.values())
    languages_seen = ", ".join(f"{name} {count}" for name, count in counts.items() if count) or "none"
    note = f", {skipped} .v files skipped as not Coq" if skipped else ""
    print(f"{total} files scanned ({languages_seen}), {len(reported)} escapes, {exempted} exempted{note}", file=out)
    # an explicit list promises a file of each language; the default only a file of any of them
    missing = [name for name, count in counts.items() if not count and (explicit or not total)]
    if missing:
        what = ", ".join(missing)
        extensions = ", ".join(extension for language in languages if language.name in missing
                               for extension in language.extensions)
        print(f"::error::no file of {what} ({extensions}) found -- an empty scan proves nothing", file=err)
        return 2
    return 1 if reported or problems or errors else 0


if __name__ == "__main__":
    sys.exit(main())
