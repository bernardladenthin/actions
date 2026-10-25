# actions

The CI building blocks of bernardladenthin's repositories, in one place instead of a copy per
repository: composite actions for single steps, reusable workflows for whole jobs, templates for
the files every repository has to carry itself, and the Python library behind them. A repository
pins this repository by commit and configures it in one file, `.github/actions-config.json`;
nothing about a particular repository is written into this code.

Until 2026-10 the same code lived as four identical copies in java-llama.cpp,
BitcoinAddressFinder, srcmorph and streambuffer, held together by a checksum manifest
(`.github/shared-files.sha256`) per repository. That worked, but every change was four pull
requests plus four manifest rewrites, and every new repository or language would have been one
more copy.

## Layout

| Path | What |
|---|---|
| `common/<action>/` | composite actions that know no language or package format |
| `java/<action>/` | composite actions for Maven and the JVM |
| `.github/workflows/*.yml` | reusable workflows (`workflow_call`), flat because GitHub allows no subfolders there: `java-*` for Java, the rest language-neutral; `ci.yml` is this repository's own CI |
| `templates/<set>/` | files a repository must carry, at the path they take there (`common`, `java`) |
| `lib/actionlib/` | the Python library (standard library only, 3.9+): `common/` and `java/` like the actions |
| `families/<name>.json` | groups of repositories that compare tool versions with each other |
| `docs/` | `publication.md` (the signed Maven publication), `dependency-license-review.md` |

A future ecosystem (Rust, Python, ...) gets `rust/<action>/`, `lib/actionlib/rust/`,
`templates/rust/` and `rust-*.yml` workflows next to the Java ones and reuses everything in
`common`.

## Composite actions

Every action carries what it runs (`github.action_path`) and calls no other action of this
repository: a composite action can reach a sibling only through a hard-coded ref.

| Action | Does |
|---|---|
| `common/setup` | puts `lib/` on `PYTHONPATH` and exports `ACTIONS_DIR`, for a repository's own checks built on actionlib and for Maven builds that run one of its tools |
| `common/check-templates` | the carried files equal their templates (sets, exclusions, relocations from the config) |
| `common/check-release-gate` | every job of the publish workflow gates every publish job, or the exemptions file says why |
| `common/check-run-scripts` | `bash -n` over every bash `run:` script of the workflows and actions |
| `common/print-host-info` | prints the runner's CPU (Linux, macOS, Windows) |
| `common/verify-gpg-key` | imports the signing key and signs and verifies a throwaway payload, printing only public metadata |
| `java/finalize-publication` | the gate of the signed Maven publication: collect, verify, freeze, secure ([docs/publication.md](docs/publication.md)) |
| `java/check-release-ref` | a release only from the tag `v<version>`, a snapshot never from a tag |
| `java/publish-central` | transfers the frozen Central files (release: one Portal bundle; snapshot: deploy-file, skipping superseded runs) |
| `java/publish-github` | attaches the frozen GitHub files to the tag release or the rolling snapshot pre-release |
| `java/attest-publication` | attests build provenance for every frozen GitHub file |
| `java/check-publication` | the publication jobs are wired as documented, and the release asset contract collects what it promises (dry run, every pull request) |
| `java/check-maven-versions` | warns where a dependency or plugin differs from a sibling repository's default branch |
| `java/maven-code-style` | spotless:check, spotbugs:check and the jdeps package graph |
| `java/verify-bytecode` | no class above the allowed class-file version in the given jars |
| `java/verify-signing-key-gradle` | signs a throwaway artifact through Gradle's in-memory PGP keys (BouncyCastle) |
| `java/print-crash-logs` | echoes JVM crash logs and surefire dumps of a failed job |
| `java/smoke-fatjar-cli` | `java -jar` a fat jar, exit 0 and a success marker required |

## Reusable workflows

| Workflow | Caller template |
|---|---|
| `reuse.yml` | `templates/common/.github/workflows/reuse.yml` (also runs on this repository) |
| `osv-scanner.yml` | `templates/common/.github/workflows/osv-scanner.yml` |
| `java-codeql.yml` | `templates/java/.github/workflows/codeql.yml` |
| `java-maven-dependency-submission.yml` | `templates/java/.github/workflows/dependency-submission.yml` |
| `java-sonarqube.yml` | `templates/java/.github/workflows/sonarqube.yml` |

The triggers stay in the caller, so a repository decides when a workflow runs. Three workflows
stay plain templates and are not reusable: `scorecard.yml` (scorecard-action verifies the workflow
file itself and allows no `run:` steps and only listed actions in its job), and `claude.yml` /
`claude-code-review.yml` (the Claude action exchanges an app token for the calling repository's
workflow, which is not verified to work from a reusable workflow).

## Using it in a repository

1. **Pin by commit.** `uses: bernardladenthin/actions/java/finalize-publication@<40-hex commit> # v1.0.0`.
   Dependabot's `github-actions` ecosystem (`templates/java/.github/dependabot.yml`) moves the pin
   with every release of this repository, like any other action.
2. **Configure** in `.github/actions-config.json`; every section is optional where it has
   defaults, unknown keys fail:

   ```json
   {
     "templates": {"sets": ["common", "java"], "exclude": [".mvn/settings.xml"],
                   "relocate": {"lombok.config": "llama/lombok.config"}},
     "release-gate": {"workflow": ".github/workflows/publish.yml",
                      "gates": ["publish-snapshot", "publish-release"],
                      "exemptions": ".github/release-gate-exemptions.txt"},
     "run-scripts": {"patterns": [".github/workflows/*.yml", ".github/actions/*/action.yml"]},
     "maven-versions": {"poms": ["pom.xml"], "family": "java-maven", "ignore-groups": ["net.ladenthin"]},
     "publication-wiring": {"workflow": ".github/workflows/publish.yml", "channels": ["snapshot", "release"]}
   }
   ```

   Complex input lives there (and in `.github/release-assets.json` for the publication); simple
   input is an action input.
3. **Templates.** `python3 -m actionlib.common.templates --write` (with `lib/` of a checkout of this
   repository on `PYTHONPATH`) writes every template the configuration selects into a new
   repository; `common/check-templates` keeps them equal afterwards. `{{owner}}` and `{{repo}}` in
   a template become the repository's owner and name; the ref after `@` in a `uses:` line may
   differ (each repository's Dependabot moves its own pins).
4. **Maven builds that run a tool of this repository** (the license collector of a fat jar) find
   it through `ACTIONS_DIR` (`common/setup`, the reusable SonarQube workflow) or, locally, in a
   checkout next to the repository (`../actions`).

## Versions

`main` is always releasable; a release is a tag `v<major>.<minor>.<patch>` on it, and the tag is
what Dependabot proposes to the repositories. A change a repository has to adapt to (an input
renamed, a check that fails more) is a new major version, and the release notes say what to do.
Repositories may sit on different releases for a while; `java/check-maven-versions` does not
compare those pins, the Dependabot pull requests are the reminder.

## Development

```bash
python3 -m unittest discover -s lib/tests -t lib     # the library, the actions' structure, the templates
PYTHONPATH=lib python3 -m actionlib.common.runscripts # bash -n over this repository's run scripts
```

`lib/tests/test_actions.py` holds every action and reusable workflow to what a consumer relies on:
each script and module an action names exists next to it, each input it reads is declared, every
third-party action is pinned to a commit, and this README lists every action and workflow.

## License

`MIT OR Apache-2.0` (see `LICENSES/`), like the `.github` directories of the repositories it serves.
