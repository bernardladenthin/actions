<!--
SPDX-FileCopyrightText: 2026 Bernard Ladenthin <bernard.ladenthin@gmail.com>
SPDX-License-Identifier: MIT OR Apache-2.0
-->

# Signed publication: one gate, three independent consumers

The Maven publication of every repository that uses these actions: `java/finalize-publication`
(the gate), `java/publish-central`, `java/publish-github` and `java/attest-publication` (the
consumers), `java/check-release-ref` (the ref check) and `java/check-publication` (the wiring and
the contract, checked in every pull request). The code is `lib/actionlib/java/publication.py` and
`releaseassets.py`, the rolling snapshot `lib/actionlib/common/snapshot.py`.

## Invariants

Build and sign every intended output locally. Complete the native, classifier and Android
checks applicable to those outputs. Before any publication-package upload, collect the exact
repository contract and run one complete local gate. Missing files, invalid signatures,
version mismatches or differing Central/GitHub bytes must fail before either publisher runs.
A release publishes only from the tag `v<version>` of a non-SNAPSHOT version; a snapshot only a
`-SNAPSHOT` version, and never from a tag.

After the gate, neither publisher compiles, packages nor signs artifacts again. Both use the
same frozen package from GitHub Actions. The Central, GitHub Release and provenance attestation
jobs depend only on that secured handoff, never on each other's success. A Central outage must
not hide the GitHub files. Internal compiler caches, test reports and native-build intermediates
are not publication packages.

## Preparation

Maven's Central publishing plugin 0.12.0 supports `-DskipUpload=true deploy`: it stages signed
release files under `target/central-staging` or snapshots under `target/central-deferred`,
without a remote upload. This is different from `skipPublishing`, which omits artifacts.
Gradle publications are similarly written to their local staging repository first.

Preserve the signed Maven files before any later fat-jar rebuild can overwrite `target/`
(after `common/setup`, which puts the library on `PYTHONPATH`):

```bash
python3 -m actionlib.java.releaseassets central-assets --maven-only
# Build and sign additional GitHub-only variants.
```

Finish both publication channels with the gate action:

```yaml
- name: Finalize and secure the signed publication
  id: finalize
  uses: bernardladenthin/actions/java/finalize-publication@<commit> # v1.x.y
  with:
    channel: release # snapshot for the rolling snapshot
    maven-assets: central-assets # omit when no outputs need preserving
```

The action collects the exact contract, runs `publication prepare --channel <channel>`, and
uploads the frozen `publication-package/` as `signed-release-assets` or `signed-snapshot-assets`.
The preparation job exports `assets_complete: ${{ steps.finalize.outputs.complete }}`.

The ref is checked twice. `check-snapshot` and `check-tag` run `java/check-release-ref` with the
channel after the build and test jobs and before anything is staged, signed or published
(`check-snapshot` only on a run that publishes: `main` carries the release version for the commit
a release is tagged on). `prepare` repeats the check against `GITHUB_REF` and refuses a channel
the version does not have.

The gate checks exact file membership, nonempty artifacts/signatures, cryptographic GPG
verification, and byte equality between Central staging and the corresponding GitHub files.
Central signatures pair by name: every artifact has its own `.asc`, and no `.asc` lacks its
artifact. GitHub renames release assets with characters outside `[A-Za-z0-9._-]` (or a leading
dot), so such a name fails the gate rather than reaching a release under another name.
It freezes `central/` in Maven layout, `github/` as flat release files, and a `manifest.json`
with commit, version, channel, file sizes and SHA-256. The action's `complete=true` output
is emitted only after both the local gate and the Actions handoff upload succeed. A failed
collection, gate or handoff upload cannot release a consumer. No incomplete publication package
is attached to a release.

## Repository configuration

`.github/release-assets.json` is the source of truth. File rules can set `central: true`
for additional Central publications/classifiers. The default is false, so runnable fat jars
and source archives do not accidentally become Central artifacts. Maven reactor outputs,
configured standalone Maven projects and enabled reactor BOMs are always Central outputs.

Optional `publication.central_roots` lists repository-relative staging directories. The default
is `["target/{staging}"]`; `{staging}` becomes `central-staging` or `central-deferred` according
to the project version. This supports separate Maven or Gradle staging without repository names
in the shared implementation. Unexpected or missing Central members fail the gate.

In a staging directory the gate reads `.pom`, `.jar`, `.aar` and `.json` files and their `.asc`.
Repository metadata (`maven-metadata*.xml`), checksums (`.md5`, `.sha1`, `.sha256`, `.sha512`) and
the Central plugin's `.index` are regenerated by the transfer and ignored; any other file fails the
gate instead of silently staying behind. Gradle writes a SNAPSHOT with a timestamp even into a file
repository (`app-1.0-20261025.085646-1.aar` under `1.0-SNAPSHOT/`); the gate maps such a name back
to `app-1.0-SNAPSHOT.aar`, the name the GitHub file and the Maven transfer use.

The actions take another contract inside the repository through their `config` input (a path
outside it is refused).

**Checked in every pull request.** `java/check-publication` runs the contract against the real
POMs and `release-assets.json` with every expected output faked in a scratch copy
(`releaseassets --dry-run`): a rule naming a missing CSV, a module the reactor no longer has or
a name the gate would refuse fails there, not in the first publication after the change. It also
reads the publish workflow and fails when the jobs are not wired as described below
(`lib/actionlib/java/wiring.py`; the `publication-wiring` section of `.github/actions-config.json`).

## Independent consumers

```yaml
central-snapshot:
  needs: [publish-snapshot]
  if: ${{ !cancelled() && needs.publish-snapshot.outputs.assets_complete == 'true' }}
  environment: maven-central
  concurrency: { group: central-snapshot-${{ github.repository }}, cancel-in-progress: false }
  permissions: { contents: read, actions: read }
  steps:
    - uses: actions/checkout@<commit> # with persist-credentials: false
    - uses: bernardladenthin/actions/java/publish-central@<commit> # v1.x.y
      with:
        channel: snapshot
        assets-complete: ${{ needs.publish-snapshot.outputs.assets_complete }}
        username: ${{ secrets.CENTRAL_USERNAME }}
        password: ${{ secrets.CENTRAL_TOKEN }}
```

`github-<channel>` runs `java/publish-github` (contents: write; the snapshot job in the
concurrency group `snapshot-release-${{ github.repository }}`), `attest-<channel>` runs
`java/attest-publication` (id-token: write, attestations: write). Each needs only the matching
preparation job and requires its successful handoff output.

Each consumer verifies the complete handoff manifest after download. This is a transport
integrity check, not a second build/signing pass. GitHub publishes only the exported `github/`
files; transport manifests and Central repository directories are not release attachments.

The rolling snapshot pre-release holds exactly the current upload: no older commit or run over a
newer one, obsolete assets pruned only after a complete upload, and the first snapshot of a
repository is a draft until its upload is complete, so a failed first upload leaves neither a
public pre-release nor a tag.

The attestation action verifies the downloaded manifest first, then attests every frozen file in
`publication-package/github/*`, including detached signatures and configured BOMs. It runs in the
caller's job, so the attestation names the caller's workflow. Its job alone among the
finalization/attestation jobs requests `id-token: write` and `attestations: write`; it needs no
signing key. Attestation failure remains visible but cannot prevent either publisher from running.

For releases, the Central consumer forms a Portal bundle from the frozen Central files with
checksums and requests automatic publication. It waits only for HTTP upload acceptance and
records the deployment ID. There is no validation/publication polling. The Maven profiles also
use `autoPublish=true` and `waitUntil=uploaded` for manual deployments.

For snapshots, a transfer-only Maven POM invokes pinned `maven-deploy-plugin:3.1.4:deploy-file`
executions during `validate`. The `central-snapshot` jobs of all runs share one concurrency group,
and the transfer is skipped when one of the 30 most recent dispatched runs already transferred the
snapshot of a newer commit (one that descends from this run's; the job is found by the name the
`job-name` input gives); Central serves the snapshot transferred last as the latest. The job reads
the runs with `actions: read` and nothing else. Maven maintains snapshot timestamps and metadata;
signatures are transferred in the same component request with their proper extensions. There are
no source build phases. For a POM-only component its signature is the primary transfer artifact
and the signed POM is supplied through `pomFile`, avoiding an empty-classifier parameter edge case.

Local gate failures stop both publication paths. Authentication or transfer failures remain
visible in the affected upload job, but never gate the other publisher. Slow Portal processing
does not fail the build. Upload acceptance is not a claim of successful Central validation;
inspect the recorded deployment in Portal if later processing fails. Re-running a transfer
uses the same frozen package. A network-interrupted GitHub release update is not atomic.
