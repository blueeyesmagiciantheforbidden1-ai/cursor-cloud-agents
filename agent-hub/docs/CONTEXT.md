# Explicit source context packs

`agent_hub.context` compiles a bounded, deterministic context pack from files the caller explicitly selects. It has no filesystem discovery, provider calls, command execution, automatic repository checkout, or network access. It is a local library; integrating its artifacts into model prompts or a remote storage service is separate work.

```python
from agent_hub.context import ArtifactStore, compile_context

pack = compile_context(
    "C:/Projects/example",
    ["app/service.py"],
    allowed_files=["app/models.py", "app/helpers.py"],
    selected_symbols={"app/service.py": ["Service.run"]},
    test_files=["tests/test_service.py"],
    config_files=["pyproject.toml"],
    repo_revision="EXACT_REVISION_FROM_CALLER",
    toolchain={"application_python": "3.12.10", "lockfile_digest": "CALLER_SUPPLIED_DIGEST"},
    environment={"build_mode": "development"},
    store=ArtifactStore("C:/PrivateContextCache/example"),
)
```

The example paths are placeholders. The caller supplies the revision; the compiler does not invoke Git or verify that label. Exact file SHA-256 hashes, including uncommitted contents, are authoritative for what was read. Toolchain values are caller-declared except for the compiler's actual Python implementation/version, which is added automatically. Environment inputs must be explicit, nonsecret build settings; only their digest appears in artifacts. Do not pass provider credentials or a complete process environment.

## Selection and evidence

The selected sources enter the pack. Python imports add at most one dependency hop, and only to files already present in the explicit allowlist. Tests and configuration enter only through their explicit arguments. All allowed files are read and hashed within the input budget so changes cannot silently reuse a stale dependency cache. Unrelated allowed-file changes conservatively invalidate the pack. No directory traversal or search is performed.

Python AST facts include qualified function/class names, definition line ranges, imports, and syntactic name/call references with their enclosing scope. These are navigation facts, not a runtime call graph: dynamic imports, dispatch, aliases, generated code, custom source roots, and language-specific module resolution are not fully resolved. Module matching uses paths relative to the selected repository root. Syntax errors are labeled `unparsed`; the source remains available. Non-Python UTF-8 files are plain source text. This does not yet index every language.

An exact `selected_symbols` name selects its definition and decorators as source excerpts. Unknown or ambiguous names fail rather than choosing an arbitrary definition. Overlapping excerpts merge. Each fact set and excerpt entry links to the original file's SHA-256, and the complete selected source is retained as an immutable addressed blob. Navigation metadata must not be presented as a substitute for checking that source before making a code claim.

## Cache and provenance

The versioned manifest includes compiler version, repository revision, declared toolchain, actual parser version, environment digest, policy digest, complete input source hashes, and entries for each selected/expanded file. Each entry points to a source blob and a navigation blob. Navigation blobs include the source hash, explicit reachable dependency hashes, and test/configuration hashes. Changes to an allowed dependency invalidate dependent navigation, including through cycles and transitive paths, even though source expansion remains one hop.

The cache key covers every source/dependency input, file/symbol selection, tests/configuration, toolchain, environment digest, revision, policy, and compiler version. TTL defaults to five minutes. Every compile rechecks source contents before looking up this exact-input cache; TTL is an additional freshness bound, not a reason to trust old hashes. Expiry regenerates identical artifact bytes when inputs remain identical. Source blobs deduplicate by SHA-256 even when two selected paths have the same contents. Reads verify the blob hash. There is no fuzzy semantic cache or stale-summary fallback.

## Bounds and exclusions

Defaults allow 64 files, 256,000 bytes per file, 2,000,000 input bytes, and a 512,000-byte encoded pack. Combined Python navigation is capped at 512 symbols, 2,048 references, and 50,000 AST nodes. `ContextPolicy` can tighten these values or raise them only within hard maxima. Source reads use bounded chunks and reject oversize files before loading them; growing files also hit the read limit. Budget failures do not write partial source packs. Binary/NUL-containing and non-UTF-8 inputs are rejected.

Paths must be normalized repository-relative names. Traversal, absolute paths, symlinks, directory junctions, `.git`, `API_KEYS`, credential directories, common secret/key filenames, and private key/certificate-store extensions are rejected. The exclusion policy is conservative: a benign file whose name contains `secret` or `credential` is also excluded. Metadata keys that resemble credentials are rejected. This is not a comprehensive content secret scanner; select only a trusted, sanitized source repository. Cache blobs contain real source and need private filesystem permissions and the same retention controls as that source.

The cache directory must be outside excluded directories, on a local filesystem with hard-link support (such as NTFS or ext4). Completed blobs are published atomically with a hard link so concurrent writers never expose a partially written addressed blob. On POSIX, newly created cache directories/files use owner-only modes; on Windows, place the store in an already protected directory because POSIX mode bits do not establish a Windows ACL. The implementation checks links and regular files but is not a sandbox against an adversarial process concurrently replacing ancestor directories. Only use a trusted local workspace and cache owner.

Run targeted verification with `python -m unittest discover -s tests -p test_context.py -v`. Tests cover changed dependencies/configuration, transitive invalidation, one-hop expansion, exact symbol selection, path and symlink escapes, byte/navigation limits, secret paths, deduplication, TTL, metadata changes, and corrupt blobs. Symlink tests explicitly skip on systems that prohibit creating them; a skipped test is not proof of platform enforcement.
