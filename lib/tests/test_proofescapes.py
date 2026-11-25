# SPDX-FileCopyrightText: 2026 Bernard Ladenthin <bernard.ladenthin@gmail.com>
#
# SPDX-License-Identifier: MIT OR Apache-2.0

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from actionlib.common import proofescapes

GIT = shutil.which("git")

# Per language: a file around a snippet on line 3, and the snippet hidden in comments and strings.
HEADS = {
    "tla": "---- MODULE M ----\nEXTENDS Naturals\n",
    "lean": "import Mathlib\nnamespace M\n",
    "coq": "Require Import Arith.\n(* a spec *)\n",
    "isabelle": "theory T imports Main\nbegin\n",
    "dafny": "// a spec\nmodule M {\n",
    "jml": "package p;\npublic class C {\n",
}
TAILS = {"tla": "\n====\n", "lean": "\nend M\n", "coq": "\n", "isabelle": "\nend\n", "dafny": "\n}\n", "jml": "\n}\n"}
CODE = {
    "tla": ["{}"], "lean": ["{}"], "coq": ["{}"], "isabelle": ["{}"], "dafny": ["{}"],
    "jml": ["//@ {} x;", "/*@ {} x; @*/", "  /*@ public normal_behavior\n    @ {} x;\n    @*/"],
}
HIDDEN = {
    "tla": [r"\* {}", "(* (* nested *) {} *)", 'S == "{}"', r'S == "a \" {}"'],
    "lean": ["-- {}", "/- /- nested -/ {} -/", "/-- doc {} -/", 'def s := "{}"', 'def s := r#"a " {}"#'],
    "coq": ["(* {} *)", "(* (* nested *) {} *)", 'Definition s := "{}".', 'Definition s := "a "" {}".'],
    "isabelle": ["(* {} *)", "(* (* nested *) {} *)", 'lemma "{}"', "text \\<open>a \\<open>b\\<close> {}\\<close>",
                 "text \u2039{}\u203a"],
    "dafny": ["// {}", "/* /* nested */ {} */", 'var s := "{}";', 'var s := @"a "" {}";'],
    "jml": ["// {}", "/* {} */", "{}", 'String s = "//@ {}";', '//@ requires s == "{}";', "/*@ x; // {} \n @*/"],
}
# a Java-code token (@SkipEsc) lives in the code, so the JML code samples are its hidden ones
CODE_SCOPE = {"jml": (["{}", "@Pure {}"], ["// {}", "/* {} */", "//@ {}", 'String s = "{}";'])}


def text_for(language, line):
    return HEADS[language] + line + TAILS[language]


def found(language, text, path="f"):
    findings, problems, _ = proofescapes.scan(path, text, proofescapes.LANGUAGES[language])
    return [(finding.line, finding.token.name) for finding in findings], problems


class TokenTest(unittest.TestCase):
    """Every token of the table: found in code at its line, ignored in comments and strings."""

    def samples(self, token):
        if token.scope == "code":
            return CODE_SCOPE[token.language]
        return CODE[token.language], HIDDEN[token.language]

    def test_every_token_is_found_in_code_at_its_line(self):
        for token in proofescapes.TOKENS:
            for sample in self.samples(token)[0]:
                line = sample.format(token.name)
                with self.subTest(language=token.language, sample=line):
                    findings, problems = found(token.language, text_for(token.language, line))
                    expected = 3 + line.split("\n").index(next(part for part in line.split("\n") if token.name in part))
                    self.assertEqual(findings, [(expected, token.name)])
                    self.assertEqual(problems, [])

    def test_every_token_is_ignored_in_comments_and_strings(self):
        for token in proofescapes.TOKENS:
            for sample in self.samples(token)[1]:
                line = sample.format(token.name)
                with self.subTest(language=token.language, sample=line):
                    self.assertEqual(found(token.language, text_for(token.language, line)), ([], []))

    def test_every_language_has_tokens_and_samples(self):
        self.assertEqual({token.language for token in proofescapes.TOKENS}, set(proofescapes.LANGUAGES))
        self.assertEqual(set(CODE), set(proofescapes.LANGUAGES))


class LexerTest(unittest.TestCase):

    def test_lines_count_across_comments_and_strings(self):
        text = "/- a\nb\n-/\ndef s := \"x\ny\"\ntheorem t : p := by\n  sorry\n"
        self.assertEqual(found("lean", text)[0], [(7, "sorry")])

    def test_tla_text_outside_the_module_is_free_text(self):
        text = "AXIOM before\n---- MODULE M ----\nTHEOREM T == TRUE\nPROOF OMITTED\n====\nAXIOM after\n"
        self.assertEqual(found("tla", text)[0], [(4, "OMITTED")])

    def test_tla_assume_of_something_else_is_fine(self):
        text = text_for("tla", "ASSUME N \\in Nat\nASSUME FALSE \\/ N = 0\nASSUMPTION FALSE")
        self.assertEqual(found("tla", text)[0], [(5, "ASSUMPTION FALSE")])

    def test_lean_primes_character_literals_and_raw_strings(self):
        text = "def c := '\"'\ntheorem t (h' : p) : p := sorry\ndef d := '\\''\ndef r := r\"sorry\"\naxiom a : False\n"
        self.assertEqual(found("lean", text)[0], [(2, "sorry"), (5, "axiom")])

    def test_lean_names_that_only_contain_a_token(self):
        text = "theorem sorry_free : True := trivial\n#print axioms sorry_free\ndef admit' := 1\n"
        self.assertEqual(found("lean", text)[0], [])

    def test_lean_sorry_ax_by_name(self):
        self.assertEqual(found("lean", "def x := ``sorryAx\n")[0], [(1, "sorryAx")])

    def test_dafny_requires_false_as_an_operand_is_fine(self):
        text = "method m()\n  requires false || x\n  requires false\n  ensures y\n"
        self.assertEqual(found("dafny", text)[0], [(3, "requires false")])

    def test_dafny_attributes_with_arguments_and_spaces(self):
        text = 'lemma {:axiom} {:other "x"} L()\nmethod {: verify  false } m()\nfunction {:axiom "why"} f(): int\n'
        self.assertEqual(found("dafny", text)[0], [(1, "{:axiom}"), (2, "{:verify false}"), (3, "{:axiom}")])

    def test_multi_word_tokens_span_whitespace(self):
        self.assertEqual(found("coq", "Local Unset\n  Guard Checking.\n")[0], [(1, "Unset Guard Checking")])

    def test_an_unterminated_comment_is_a_problem(self):
        findings, problems = found("coq", "Require Import A.\n(* open\nAdmitted.\n", "a.v")
        self.assertEqual(findings, [])
        self.assertEqual(problems, ["a.v:2: unterminated comment -- the rest of the file cannot be scanned"])


class JmlTest(unittest.TestCase):

    def test_only_annotations_count_in_java(self):
        text = ("class C {\n  void assume(int axiom) { assume(axiom); }  // requires false\n"
                "  /* assume x; */\n  String s = \"//@ assume x;\";\n  char c = '\"';\n"
                "  //@ assume x > 0;\n  //+OPENJML@ nowarn Null;\n  /*-ESC@ axiom true; @*/\n"
                "  @org.jmlspecs.annotation.SkipEsc void m() {}\n}\n")
        findings, problems, relevant = proofescapes.scan("C.java", text, proofescapes.LANGUAGES["jml"])
        self.assertEqual([(f.line, f.token.name) for f in findings],
                         [(6, "assume"), (7, "nowarn"), (8, "axiom"), (9, "@SkipEsc")])
        self.assertEqual((problems, relevant), ([], True))

    def test_a_java_file_without_annotations_is_not_a_jml_file(self):
        _, _, relevant = proofescapes.scan("C.java", "class C { /* x */ }\n", proofescapes.LANGUAGES["jml"])
        self.assertFalse(relevant)

    def test_requires_false_as_an_operand_is_fine(self):
        text = "//@ requires false || x;\n//@ requires false;\n//@ requires false ==> y;\n"
        self.assertEqual(found("jml", text)[0], [(2, "requires false")])


class Repository:
    """A temporary repository: files, a configuration, and main() over it."""

    def __init__(self, test, files, config=None, git=False):
        temp = tempfile.TemporaryDirectory()
        test.addCleanup(temp.cleanup)
        self.root = temp.name
        for path, text in files.items():
            full = os.path.join(self.root, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
        if config is not None:
            os.makedirs(os.path.join(self.root, ".github"), exist_ok=True)
            with open(os.path.join(self.root, ".github", "actions-config.json"), "w") as f:
                json.dump(config, f)
        if git:
            subprocess.run(["git", "init", "-q", self.root], check=True)
            subprocess.run(["git", "-C", self.root, "add", "--", *files], check=True)

    def main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        code = proofescapes.main(["--root", self.root, *args], out, err)
        return code, out.getvalue(), err.getvalue()


TLA_OMITTED = "---- MODULE M ----\nTHEOREM T == TRUE\nPROOF OMITTED\nTHEOREM U == TRUE\nPROOF OMITTED\n====\n"
TLA_CLEAN = "---- MODULE M ----\nTHEOREM T == TRUE\nPROOF OBVIOUS\n====\n"


def exempt(file="spec/M.tla", token="OMITTED", count=2, reason="the model checker covers T and U"):
    return {"proof-escapes": {"exempt": [{"file": file, "token": token, "count": count, "reason": reason}]}}


class MainTest(unittest.TestCase):

    def test_findings_are_errors_with_line_language_and_why_and_a_summary(self):
        repo = Repository(self, {"spec/M.tla": TLA_OMITTED, "spec/N.tla": TLA_CLEAN,
                                 "src/C.java": "class C { //@ assume x;\n}\n", "src/D.java": "class D {}\n"})
        code, out, err = repo.main()
        self.assertEqual(code, 1)
        self.assertEqual(err.splitlines(), [
            "::error::spec/M.tla:3: OMITTED (tla) -- the proof is omitted; TLAPS accepts the step unproved",
            "::error::spec/M.tla:5: OMITTED (tla) -- the proof is omitted; TLAPS accepts the step unproved",
            "::error::src/C.java:1: assume (jml) -- the verifier takes the condition as true without proof",
        ])
        self.assertEqual(out, "3 files scanned (tla 2, jml 1), 3 escapes, 0 exempted\n")

    def test_clean_files_pass(self):
        code, out, err = Repository(self, {"M.tla": TLA_CLEAN}).main()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "1 files scanned (tla 1), 0 escapes, 0 exempted\n")

    def test_an_exemption_with_the_exact_count_takes_its_findings(self):
        repo = Repository(self, {"spec/M.tla": TLA_OMITTED}, exempt())
        code, out, err = repo.main()
        self.assertEqual((code, err), (0, ""))
        self.assertIn("0 escapes, 2 exempted", out)

    def test_more_or_fewer_occurrences_than_the_exemption_fail(self):
        for count in (1, 3):
            with self.subTest(count=count):
                code, out, err = Repository(self, {"spec/M.tla": TLA_OMITTED}, exempt(count=count)).main()
                self.assertEqual(code, 1)
                self.assertIn(f"::error::spec/M.tla: the exemption for OMITTED allows {count}, found 2", err)
                self.assertEqual(err.count("OMITTED (tla)"), 2)
                self.assertIn("2 escapes, 0 exempted", out)

    def test_a_stale_exemption_fails(self):
        code, _, err = Repository(self, {"spec/M.tla": TLA_CLEAN}, exempt()).main()
        self.assertEqual(code, 1)
        self.assertIn("::error::spec/M.tla: the exemption for OMITTED matches nothing -- remove it", err)

    def test_malformed_exemptions_are_configuration_errors(self):
        cases = {
            "reason must say why": exempt(reason=" "),
            "count must be a positive": exempt(count=0),
            "unknown token 'omitted'": exempt(token="omitted"),
        }
        missing = exempt()
        del missing["proof-escapes"]["exempt"][0]["reason"]
        cases["missing reason"] = missing
        extra = exempt()
        extra["proof-escapes"]["exempt"][0]["line"] = 3
        cases["unknown key(s) line"] = extra
        twice = exempt()
        twice["proof-escapes"]["exempt"] *= 2
        cases["a second exemption"] = twice
        for message, config in cases.items():
            with self.subTest(message):
                code, _, err = Repository(self, {"spec/M.tla": TLA_OMITTED}, config).main()
                self.assertEqual(code, 2)
                self.assertIn(message, err)

    def test_unknown_keys_and_languages_are_configuration_errors(self):
        for config, message in (({"proof-escapes": {"language": ["tla"]}}, "unknown key(s) language"),
                                ({"proof-escapes": {"languages": ["tlaplus"]}}, "unknown tlaplus"),
                                ({"proof-escapes": {"languages": []}}, "empty"),
                                ({"proof-escapes": {"exclude": "x"}}, "expected a list of strings")):
            with self.subTest(message):
                code, _, err = Repository(self, {"M.tla": TLA_CLEAN}, config).main()
                self.assertEqual(code, 2)
                self.assertIn(message, err)

    def test_an_empty_scan_fails(self):
        code, out, err = Repository(self, {"README.md": "sorry\n", "C.java": "class C {}\n"}).main()
        self.assertEqual(code, 2)
        self.assertIn("0 files scanned (none)", out)
        self.assertIn("an empty scan proves nothing", err)

    def test_an_explicit_language_without_a_file_fails(self):
        code, _, err = Repository(self, {"M.tla": TLA_CLEAN}, {"proof-escapes": {"languages": ["tla", "lean"]}}).main()
        self.assertEqual(code, 2)
        self.assertIn("no file of lean (.lean) found", err)

    def test_disabled_languages_and_excluded_files_are_not_scanned(self):
        files = {"M.tla": TLA_CLEAN, "L.lean": "theorem t : p := sorry\n", "vendor/a/X.tla": TLA_OMITTED}
        config = {"proof-escapes": {"languages": ["tla"], "exclude": ["vendor/**"]}}
        code, out, err = Repository(self, files, config).main()
        self.assertEqual((code, err), (0, ""))
        self.assertIn("1 files scanned (tla 1)", out)

    def test_verilog_v_files_are_skipped_and_counted(self):
        files = {"rtl/top.v": "module top(input a);\n  // Admitted\n  assign Axiom = a;\nendmodule\n",
                 "proof/T.v": "Require Import Arith.\nLemma l : True.\nProof. admit. Admitted.\n"}
        code, out, err = Repository(self, files).main()
        self.assertEqual(code, 1)
        self.assertEqual(out, "1 files scanned (coq 1), 2 escapes, 0 exempted, 1 .v files skipped as not Coq\n")
        self.assertNotIn("rtl/top.v", err)

    def test_unterminated_comments_fail(self):
        code, _, err = Repository(self, {"M.tla": "---- MODULE M ----\n(* open\n====\n"}).main()
        self.assertEqual(code, 1)
        self.assertIn("::error::M.tla:2: unterminated comment", err)

    @unittest.skipUnless(GIT, "git not installed")
    def test_only_tracked_files_count_in_a_git_repository(self):
        repo = Repository(self, {"M.tla": TLA_CLEAN}, git=True)
        with open(os.path.join(repo.root, "Untracked.tla"), "w") as f:
            f.write(TLA_OMITTED)
        code, out, err = repo.main()
        self.assertEqual((code, err), (0, ""))
        self.assertIn("1 files scanned (tla 1)", out)

    def test_unknown_arguments_fail(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            proofescapes.main(["-v"])


if __name__ == "__main__":
    unittest.main()
