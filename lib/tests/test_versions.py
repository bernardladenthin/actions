# SPDX-FileCopyrightText: 2026 Bernard Ladenthin <bernard.ladenthin@gmail.com>
#
# SPDX-License-Identifier: MIT OR Apache-2.0

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from actionlib.java import versions
from tests.helpers import REPO


def pom(body, properties=""):
    return (f'<project xmlns="http://maven.apache.org/POM/4.0.0"><properties>{properties}</properties>'
            f'{body}</project>')


PARENT = pom("", "<junit.version>6.1.3</junit.version><nested>${junit.version}</nested><palantir>2.99.0</palantir>")
CHILD = pom("""<dependencies>
  <dependency><groupId>org.junit.jupiter</groupId><artifactId>junit-jupiter</artifactId>
    <version>${junit.version}</version></dependency>
  <dependency><groupId>a</groupId><artifactId>nested</artifactId><version>${nested}</version></dependency>
  <dependency><groupId>a</groupId><artifactId>unresolved</artifactId><version>${nowhere}</version></dependency>
  <dependency><groupId>a</groupId><artifactId>managed</artifactId></dependency>
  <dependency><groupId>net.ladenthin</groupId><artifactId>llama</artifactId><version>5.2.0</version></dependency>
</dependencies>
<build><plugins><plugin><artifactId>maven-jar-plugin</artifactId><version>3.5.0</version>
  <configuration><annotationProcessorPaths><path><groupId>com.google.errorprone</groupId>
    <artifactId>error_prone_core</artifactId><version>2.50.0</version></path></annotationProcessorPaths>
  </configuration></plugin>
  <plugin><groupId>com.diffplug.spotless</groupId><artifactId>spotless-maven-plugin</artifactId><version>3.10.3</version>
    <configuration><java><palantirJavaFormat><version>${palantir}</version></palantirJavaFormat></java></configuration>
  </plugin></plugins></build>""")


class CoordinatesTest(unittest.TestCase):

    def test_reads_plugins_dependencies_and_processor_paths_with_properties_resolved(self):
        self.assertEqual(versions.coordinates([PARENT, CHILD], ["net.ladenthin"]), {
            "org.junit.jupiter:junit-jupiter": {"6.1.3"},
            "a:nested": {"6.1.3"},
            "org.apache.maven.plugins:maven-jar-plugin": {"3.5.0"},
            "com.google.errorprone:error_prone_core": {"2.50.0"},
            "com.diffplug.spotless:spotless-maven-plugin": {"3.10.3"},
            "com.palantir.javaformat:palantir-java-format": {"2.99.0"},
        })

    def test_a_repository_using_two_versions_of_one_coordinate_keeps_both(self):
        other = pom('<dependencies><dependency><groupId>org.junit.jupiter</groupId>'
                    '<artifactId>junit-jupiter</artifactId><version>6.0.0</version></dependency></dependencies>')
        self.assertEqual(versions.coordinates([PARENT, CHILD, other])["org.junit.jupiter:junit-jupiter"],
                         {"6.0.0", "6.1.3"})


class CompareTest(unittest.TestCase):

    def test_only_coordinates_both_use_and_only_differences(self):
        own = {"j:u": {"1"}, "s:p": {"2"}, "only:here": {"3"}}
        others = {"r1": {"j:u": {"1"}, "s:p": {"9"}}, "r2": {"only:there": {"4"}}}
        self.assertEqual(versions.compare(own, others),
                         ["s:p is 2 here and 9 in r1: bump it in every repository"])


class GradleKotlinTest(unittest.TestCase):

    def test_plugins_are_read_from_every_kts_file_outside_build_output(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "app", "build"))
            with open(os.path.join(root, "app", "build.gradle.kts"), "w", encoding="utf-8") as f:
                f.write('plugins {\n    id("com.android.application") version "9.4.0"\n'
                        '    id("org.jetbrains.kotlin.plugin.compose") version "2.4.20"\n'
                        '    kotlin("jvm") version "2.4.21"\n}\n')
            with open(os.path.join(root, "app", "build", "stale.gradle.kts"), "w", encoding="utf-8") as f:
                f.write('kotlin("jvm") version "1.0.0"\n')
            self.assertEqual(versions.gradle_kotlin_plugins(root), [
                ("app/build.gradle.kts", "org.jetbrains.kotlin.plugin.compose", "2.4.20"),
                ("app/build.gradle.kts", "org.jetbrains.kotlin.jvm", "2.4.21"),
            ])

    def test_a_plugin_behind_the_poms_kotlin_is_a_warning_and_nothing_else_is(self):
        own = {"org.jetbrains.kotlin:kotlin-stdlib": {"2.4.21"}, "org.jetbrains.kotlinx:kotlinx-coroutines-core": {"1.11.0"}}
        plugins = [("app/build.gradle.kts", "org.jetbrains.kotlin.plugin.compose", "2.4.20"),
                   ("app/build.gradle.kts", "org.jetbrains.kotlin.jvm", "2.4.21")]
        self.assertEqual(versions.compare_gradle_kotlin(own, plugins), [
            "org.jetbrains.kotlin.plugin.compose is 2.4.20 in app/build.gradle.kts and the POMs use Kotlin 2.4.21: "
            "Dependabot bumps only the POMs, bump the Gradle plugin with them"])
        self.assertEqual(versions.compare_gradle_kotlin({"a:b": {"1"}}, plugins), [])
        self.assertEqual(versions.compare_gradle_kotlin(own, []), [])


class MainTest(unittest.TestCase):
    """main() over a repository with its configuration; the siblings come through the fetcher."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name
        os.makedirs(os.path.join(self.root, ".github"))
        os.makedirs(os.path.join(self.root, "child"))
        with open(os.path.join(self.root, "pom.xml"), "w") as f:
            f.write(PARENT)
        with open(os.path.join(self.root, "child", "pom.xml"), "w") as f:
            f.write(CHILD)
        self.configure({"poms": ["pom.xml", "child/pom.xml"], "siblings": ["o/sb", "o/srcmorph", "o/baf"],
                        "ignore-groups": ["net.ladenthin"]})
        environment = mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "o/sb"})
        environment.start()
        self.addCleanup(environment.stop)

    def configure(self, section):
        with open(os.path.join(self.root, ".github", "actions-config.json"), "w") as f:
            json.dump({"maven-versions": section}, f)

    @staticmethod
    def sibling(poms):
        return json.dumps({"maven-versions": {"poms": list(poms)}})

    def run_main(self, files, argv=()):
        out, err = io.StringIO(), io.StringIO()

        def fetcher(repo, path):
            if (repo, path) not in files:
                raise OSError("404")
            return files[(repo, path)]
        code = versions.main(["--root", self.root, *argv], fetcher, out, err)
        return code, out.getvalue(), err.getvalue()

    def test_differences_are_warnings_and_never_fail(self):
        bumped = pom('<dependencies><dependency><groupId>org.junit.jupiter</groupId>'
                     '<artifactId>junit-jupiter</artifactId><version>6.1.4</version></dependency></dependencies>')
        same = CHILD.replace("${junit.version}", "6.1.3").replace("${nested}", "6.1.3")
        code, out, err = self.run_main({
            ("o/srcmorph", ".github/actions-config.json"): self.sibling(["pom.xml"]),
            ("o/srcmorph", "pom.xml"): bumped,
            ("o/baf", ".github/actions-config.json"): self.sibling(["pom.xml"]),
            ("o/baf", "pom.xml"): same})
        self.assertEqual(code, 0)
        self.assertIn("::warning::org.junit.jupiter:junit-jupiter is 6.1.3 here and 6.1.4 in srcmorph", err)
        self.assertNotIn("in baf", err)
        self.assertNotIn("o/sb", err)
        self.assertIn("1 version differences (compared with baf, srcmorph)", out)

    def test_a_sibling_without_a_configuration_is_a_warning(self):
        code, out, err = self.run_main({})
        self.assertEqual(code, 0)
        self.assertIn("could not read o/srcmorph's .github/actions-config.json", err)
        self.assertIn("compared with no other repository", out)

    def test_a_missing_pom_skips_that_file_not_the_repository(self):
        bumped = pom('<dependencies><dependency><groupId>org.junit.jupiter</groupId>'
                     '<artifactId>junit-jupiter</artifactId><version>6.1.4</version></dependency></dependencies>')
        code, _, err = self.run_main({
            ("o/srcmorph", ".github/actions-config.json"): self.sibling(["pom.xml", "srcmorph/pom.xml"]),
            ("o/srcmorph", "pom.xml"): bumped})
        self.assertEqual(code, 0)
        self.assertIn("could not read o/srcmorph's srcmorph/pom.xml", err)
        self.assertIn("6.1.4 in srcmorph", err)

    def test_a_gradle_kotlin_plugin_behind_the_poms_is_reported_with_the_other_warnings(self):
        kotlin = pom('<dependencies><dependency><groupId>org.jetbrains.kotlin</groupId>'
                     '<artifactId>kotlin-stdlib</artifactId><version>2.4.21</version></dependency></dependencies>')
        with open(os.path.join(self.root, "pom.xml"), "w") as f:
            f.write(kotlin)
        self.configure({"poms": ["pom.xml"], "siblings": []})
        os.makedirs(os.path.join(self.root, "app"))
        with open(os.path.join(self.root, "app", "build.gradle.kts"), "w") as f:
            f.write('plugins { id("org.jetbrains.kotlin.plugin.compose") version "2.4.20" }')
        code, out, err = self.run_main({})
        self.assertEqual(code, 0)
        self.assertIn("::warning::org.jetbrains.kotlin.plugin.compose is 2.4.20 in app/build.gradle.kts "
                      "and the POMs use Kotlin 2.4.21", err)
        self.assertIn("1 version differences", out)

    def test_the_family_names_the_siblings(self):
        families = os.path.join(self.root, "families")
        os.makedirs(families)
        with open(os.path.join(families, "f.json"), "w") as f:
            json.dump({"repositories": ["o/sb", "o/srcmorph"]}, f)
        self.configure({"poms": ["pom.xml"], "family": "f"})
        with mock.patch.object(versions, "FAMILIES", families):
            code, out, err = self.run_main({})
        self.assertEqual(code, 0)
        self.assertIn("could not read o/srcmorph's", err)
        self.assertNotIn("o/sb's", err)
        self.configure({"poms": ["pom.xml"], "family": "nothing"})
        with mock.patch.object(versions, "FAMILIES", families):
            code, _, err = self.run_main({})
        self.assertEqual(code, 2)
        self.assertIn("no family 'nothing'; known: f", err)

    def test_the_configuration_is_required_and_checked(self):
        self.configure({"family": "java-maven"})
        code, _, err = self.run_main({})
        self.assertEqual(code, 2)
        self.assertIn("maven-versions: missing poms", err)
        self.configure({"poms": ["pom.xml"], "family": "java-maven", "siblings": ["o/x"]})
        code, _, err = self.run_main({})
        self.assertEqual(code, 2)
        self.assertIn("either family or siblings", err)
        os.remove(os.path.join(self.root, ".github", "actions-config.json"))
        code, _, err = self.run_main({})
        self.assertEqual(code, 2)
        self.assertIn("not found", err)


class FamilyTest(unittest.TestCase):

    def test_the_families_of_this_repository_are_valid(self):
        for path in sorted(Path(REPO, "families").glob("*.json")):
            with self.subTest(path.name):
                repositories = versions.siblings({"family": path.stem, "siblings": []})
                self.assertTrue(repositories)
                self.assertTrue(all(len(r.split("/")) == 2 for r in repositories))


if __name__ == "__main__":
    unittest.main()
