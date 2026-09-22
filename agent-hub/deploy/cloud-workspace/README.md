# On-demand browser coding workspace

This package restores reviewed source into Cloud Shell Editor. The hub and workers
continue running separately on Cloud Run. It creates no VM, service, scheduler,
provider session, package installation, background process, or model request.

Cloud Shell provides a temporary interactive machine and a per-user 5 GB persistent
home directory. The default quota is 50 hours per week. Sessions terminate, and
Google can remove the home directory after 120 days without access; it is not the
only backup. This is not an always-running worker host. See Google's
[Cloud Shell architecture](https://docs.cloud.google.com/shell/docs/how-cloud-shell-works)
and [quotas](https://docs.cloud.google.com/shell/docs/quotas-limits).

## Uploader contract

Run this only against the application source tree, with a new output directory
outside that tree. Python's standard library is sufficient:

```text
python bootstrap.py package --source /absolute/source --output /absolute/new-artifacts
```

The reusable API is `package_snapshot(source, output, fixture_exceptions=[])`.
It returns `archive_path`, `archive_size`, `snapshot_sha256`, `manifest_path`,
`manifest_sha256`, `source_file_count`, `excluded_entries`, and `object_prefix`.
The snapshot ID is the SHA256 of the complete deterministic, uncompressed archive.
The archive includes `.runcrew-source-manifest.json`, which binds every included
path, byte count, and SHA256. Timestamps, ownership, and executable modes are not
inherited from the packaging machine.

The separate reviewed uploader must:

1. Freeze source changes, call the packaging API, and review the included manifest
   and any excluded material before publishing a release.
2. Create `source.tar` and `source-manifest.json` under
   `gs://runcrew-496481413971-source/cloud-workspaces/<snapshot_sha256>/` using a
   create-only precondition. Do not overwrite existing objects or use `latest`.
3. Upload this reviewed `bootstrap.py` separately under that same prefix. Record
   its exact object generation and SHA256. It must be verified before execution;
   verifying the source archive does not authenticate a substituted bootstrap.
4. Record the returned exact generation for each cloud object, plus the archive
   SHA and length, in the trusted release receipt. Give the browser owner read
   access to the approved source object/prefix, not credentials or project-wide
   administration. Restore needs `storage.objects.get`, not bucket listing or
   `storage.buckets.get`. Grant new-object creation separately only for approved
   cloud backup work.

This package performs none of those uploads or IAM mutations. Object storage and
operations can incur ordinary Cloud Storage charges; no always-on compute is
introduced by this package.

## Source boundaries and synthetic fixtures

The fixed allowlist covers application modules, tests, docs, deployment source,
ChatGPT package source, connector examples, and named root source files. It allows
text source/configuration extensions and Dockerfiles. It excludes runtime state,
collaboration transcripts, private/auth folders, `.env` files, known credential
filenames, Git data, caches, virtual environments, `node_modules`, distribution
archives, databases, and native binaries. Exclusions are not a backup of those
resources. Public release provenance is included when it is an allowed UTF-8 file.

Included files are scanned for recognizable credential markers. This backstop is
not a general secret detector; a source review remains required. Errors report a
path, never the matched value. A credential-like literal in a test stops packaging
unless an operator has reviewed that exact synthetic fixture. There is no blanket
test-directory exemption. The optional `--fixture-exceptions reviewed-fixtures.json`
accepts this bounded JSON list:

```json
[
  {
    "path": "tests/test_example.py",
    "sha256": "<64 lowercase hexadecimal characters from the reviewed file>",
    "reason": "Reviewed nonfunctional synthetic credential used by an offline test."
  }
]
```

Only individual test/fixture files qualify. A changed digest, production-source
exception, unknown path, or unused exception fails closed. Approved exceptions are
embedded in the manifest and checked again during restore. Never exempt actual
credentials, even when someone has accidentally placed them in a test file.

## Browser restore

In the intended owner's existing Cloud Shell session, use the reviewed bootstrap
whose bytes were checked against the trusted release receipt. No local Google
credentials or provider keys are transferred. Cloud Shell may present its normal
Google authorization prompt. Then run with the real release values:

```text
python3 bootstrap.py restore --snapshot-sha256 <approved-archive-sha256> --generation <exact-archive-generation>
```

The helper uses an argument array for one bounded
`gcloud storage cat gs://runcrew-496481413971-source/cloud-workspaces/<sha>/source.tar#<generation>`
read. It pins the project and inherits Cloud Shell's existing Google identity.
It never calls login, prints access tokens, uses an API key, changes identity,
lists the bucket, or retries a failed download automatically. Google documents
[generation-qualified object URLs](https://docs.cloud.google.com/storage/docs/using-versioned-objects)
and [`gcloud storage cat`](https://docs.cloud.google.com/sdk/gcloud/reference/storage/cat).

Before writing source, restore checks the entire archive SHA, manifest, file hashes,
path policy, UTF-8 content, credential markers, and fixed size/count limits. It
refuses symlinks, hard links, special members, traversal, duplicate/case-colliding
paths, and existing destination workspaces. It does not call `extractall` or run
anything from the archive. Limits are 2,048 source files, 4 MiB per file, 32 MiB of
source, 1 MiB manifest, 40 MiB download, and a 180-second default download deadline.

The resulting path is:

```text
$HOME/myhero/workspaces/<snapshot_sha256>
```

Use Cloud Shell Editor **File > Open Folder** to open the printed path. To open an
individual file, Google documents `cloudshell edit <file>`; no undocumented folder
flag is assumed. See the [Editor interface](https://docs.cloud.google.com/shell/docs/editor-overview).
No custom extensions are installed by this bootstrap.

All bootstrap writes stay in this fresh workspace. An interrupted extraction leaves
a partial directory that cannot be overwritten on rerun; inspect it before any
manual cleanup. The helper never deletes existing files. Cloud Shell itself is a
general-purpose user environment, not an OS sandbox against a malicious process
running as the same owner. Open code, tests, and terminal commands still need their
normal review and authorization.

## Cloud persistence and backup

Edits inside the workspace persist between normal Cloud Shell sessions. They are
not uploaded automatically and do not update Cloud Run. Keep source edits inside
the opened project folder. Before ending important work, call the same packaging
API against that workspace into a new folder such as `$HOME/myhero/exports/<new-id>`.
Have the reviewed uploader create a new immutable cloud snapshot and record its
generations; preserve the previous snapshot. If unchanged synthetic fixtures need
exceptions, explicitly reuse their reviewed path/digest records from the original
manifest; changed fixtures require new review. The origin receipt and embedded
manifest are metadata, not source files, so they do not recursively enter backups.

An uploaded archive is the durable recovery source. Until the uploader confirms
the new generation and SHA, recent edits exist only in that owner's Cloud Shell
home. Runtime deployment remains a separate reviewed Cloud Run release.

## Offline verification

```text
python -W error::ResourceWarning -m unittest discover -s deploy/cloud-workspace -p test_bootstrap.py -v
```

Tests use temporary synthetic trees and synthetic Python subprocesses in place of
gcloud. They cover deterministic round trips, exclusions, fixture binding, malformed
archives, traversal/links, overwrite refusal, pinned download arguments, timeouts,
output limits, and diagnostic redaction. No cloud or provider call is part of the
test suite. Actual browser restore and IAM access require separate commissioning.
