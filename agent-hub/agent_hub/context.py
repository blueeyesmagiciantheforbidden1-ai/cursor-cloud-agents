"""Deterministic context packs from explicitly allowed repository files only.

This module does not discover repositories, execute code, call providers, or
replace source with a generated summary. AST facts are navigation aids whose
source hashes must still be checked before making claims about current code.
"""
from __future__ import annotations

import ast
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import time
from typing import Mapping, Sequence

COMPILER_VERSION = "explicit-context/1"
HARD_ARTIFACT_LIMIT = 8 * 1024 * 1024
DENIED_PARTS = frozenset({"api_keys", ".git", ".ssh", ".gnupg", ".aws", ".azure", ".gcloud"})
SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore")


class ContextError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(value: bytes):
    return hashlib.sha256(value).hexdigest()


def _secret_name(name):
    name = name.casefold()
    return (name in DENIED_PARTS or name == ".env" or name.startswith(".env.")
            or name.startswith(("id_rsa", "id_ed25519", "service-account", "service_account"))
            or any(term in name for term in ("credential", "secret", "password", "passwd", "api_key", "apikey", "access_token", "refresh_token"))
            or name in ("auth.json", "tokens.json", "token.json") or name.endswith(SECRET_SUFFIXES))


def _no_links(path: Path):
    # Check lexical ancestors too: resolving first would hide a symlink/junction.
    for item in (path, *path.parents):
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise ContextError("Symlinks and directory junctions are not accepted")


def _root(path):
    result = Path(path).absolute()
    _no_links(result)
    if any(part.casefold() in DENIED_PARTS for part in result.parts):
        raise ContextError("Repository/cache root is inside an excluded directory")
    return result.resolve()


def _relative(value):
    if not isinstance(value, str) or not value or len(value) > 400:
        raise ContextError("File names must be bounded repository-relative strings")
    value = value.replace("\\", "/")
    parts = value.split("/")
    if value.startswith("/") or any(part in ("", ".", "..") or ":" in part or "\x00" in part for part in parts):
        raise ContextError("Only normalized relative paths are accepted")
    if any(_secret_name(part) for part in parts):
        raise ContextError("Selected path matches the secret/directory exclusion policy")
    return PurePosixPath(value).as_posix()


def _source_path(root: Path, relative):
    path = root.joinpath(*PurePosixPath(relative).parts)
    _no_links(path)
    if not path.resolve().is_relative_to(root):
        raise ContextError("Selected source escapes the repository")
    return path


def _read_bounded(path: Path, limit: int):
    _no_links(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ContextError("File is not regular or exceeds the byte budget")
            chunks, total = [], 0
            while True:
                chunk = handle.read(min(65536, limit - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise ContextError("File grew beyond the byte budget")
                chunks.append(chunk)
            _no_links(path)
            return b"".join(chunks)
    except OSError as exc:
        raise ContextError("Selected file could not be read") from exc


@dataclass(frozen=True)
class ContextPolicy:
    max_files: int = 64
    max_file_bytes: int = 256_000
    max_input_bytes: int = 2_000_000
    max_pack_bytes: int = 512_000
    max_symbols: int = 512
    max_references: int = 2048
    max_ast_nodes: int = 50_000
    cache_ttl_seconds: int = 300

    def __post_init__(self):
        limits = {"max_files": 256, "max_file_bytes": 1_000_000, "max_input_bytes": 8_000_000,
                  "max_pack_bytes": 4_000_000, "max_symbols": 4096, "max_references": 16_384,
                  "max_ast_nodes": 200_000, "cache_ttl_seconds": 86400}
        if any(type(getattr(self, key)) is not int or not 1 <= getattr(self, key) <= upper
               for key, upper in limits.items()):
            raise ContextError("Context policy exceeds its supported bounds")


class ArtifactStore:
    """Private local SHA-256 blob store and bounded TTL cache index.

    Existing blobs are immutable and verified on reuse. The store is not a
    security boundary against another process modifying the same directories.
    """
    def __init__(self, directory):
        self.root = _root(directory)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in ("blobs", "cache"):
            child = self.root / name
            _no_links(child)
            child.mkdir(exist_ok=True, mode=0o700)

    def _path(self, kind, key):
        if kind not in ("blobs", "cache") or len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise ContextError("Invalid artifact address")
        path = self.root / kind / key
        _no_links(path)
        return path

    def put(self, content: bytes):
        if not isinstance(content, bytes) or len(content) > HARD_ARTIFACT_LIMIT:
            raise ContextError("Artifact exceeds its byte budget")
        key = digest(content)
        path = self._path("blobs", key)
        temporary = path.with_name(path.name + "." + os.urandom(8).hex())
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
            # Publish an already complete inode without replacing an existing
            # addressed blob. Concurrent compilers either publish or verify it.
            _no_links(path)
            try:
                os.link(temporary, path)
            except FileExistsError:
                if self.get(key) != content:
                    raise ContextError("Existing artifact content does not match its address")
        except OSError as exc:
            raise ContextError("Artifact store requires writable local hard-link support") from exc
        finally:
            if temporary.exists():
                temporary.unlink()
        return key

    def get(self, key, *, max_bytes=HARD_ARTIFACT_LIMIT):
        if type(max_bytes) is not int or not 1 <= max_bytes <= HARD_ARTIFACT_LIMIT:
            raise ContextError("Invalid artifact read bound")
        content = _read_bounded(self._path("blobs", key), max_bytes)
        if digest(content) != key:
            raise ContextError("Artifact hash verification failed")
        return content

    def cached(self, key, now, max_bytes):
        path = self._path("cache", key)
        if not path.exists():
            return None
        try:
            index = json.loads(_read_bounded(path, 1024))
            expires = index.get("expires_at", 0) if isinstance(index, dict) else 0
            if type(expires) not in (int, float) or not math.isfinite(expires):
                raise ContextError("Invalid cached context expiry")
            if expires <= now:
                return None
            blob = index["blob"]
            return blob, json.loads(self.get(blob, max_bytes=max_bytes))
        except (ValueError, KeyError, TypeError) as exc:
            raise ContextError("Invalid cached context artifact") from exc

    def cache(self, key, blob, expires_at):
        path = self._path("cache", key)
        encoded = canonical({"blob": blob, "expires_at": expires_at})
        temporary = path.with_name(path.name + "." + os.urandom(8).hex())
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
            _no_links(path)
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()


class _Facts(ast.NodeVisitor):
    def __init__(self, policy):
        self.policy = policy
        self.stack, self.symbols, self.references, self.imports = [], [], [], []

    def _symbol(self, item, kind):
        name = ".".join([*self.stack, item.name])
        start = min([item.lineno, *(node.lineno for node in item.decorator_list)])
        self.symbols.append({"name": name, "kind": kind, "start_line": start, "end_line": item.end_lineno})
        if len(self.symbols) > self.policy.max_symbols:
            raise ContextError("Python symbol budget exceeded")
        self.stack.append(item.name)
        self.generic_visit(item)
        self.stack.pop()

    def visit_FunctionDef(self, item):
        self._symbol(item, "function")

    def visit_AsyncFunctionDef(self, item):
        self._symbol(item, "async_function")

    def visit_ClassDef(self, item):
        self._symbol(item, "class")

    def _reference(self, kind, name, line):
        self.references.append({"kind": kind, "name": name, "caller": ".".join(self.stack) or "<module>", "line": line})
        if len(self.references) > self.policy.max_references:
            raise ContextError("Python reference budget exceeded")

    def visit_Call(self, item):
        def name(expression):
            if isinstance(expression, ast.Name):
                return expression.id
            if isinstance(expression, ast.Attribute):
                parent = name(expression.value)
                return parent + "." + expression.attr if parent else None
            return None
        target = name(item.func)
        if target:
            self._reference("call", target, item.lineno)
        self.generic_visit(item)

    def visit_Name(self, item):
        if isinstance(item.ctx, ast.Load):
            self._reference("name", item.id, item.lineno)

    def visit_Import(self, item):
        self.imports.extend({"module": alias.name, "level": 0, "names": [], "line": item.lineno}
                            for alias in item.names)

    def visit_ImportFrom(self, item):
        self.imports.append({"module": item.module or "", "level": item.level,
                             "names": [alias.name for alias in item.names], "line": item.lineno})


def _facts(path, text, source_hash, policy):
    result = {"source_hash": source_hash, "language": "python" if path.endswith(".py") else "text",
              "symbols": [], "references": [], "imports": [], "parse_status": "not_applicable", "ast_nodes": 0}
    if result["language"] == "python":
        try:
            tree = ast.parse(text, filename=path)
        except (SyntaxError, ValueError, RecursionError):
            result["parse_status"] = "unparsed"
            return result
        count = sum(1 for _ in ast.walk(tree))
        if count > policy.max_ast_nodes:
            raise ContextError("Python AST node budget exceeded")
        visitor = _Facts(policy)
        try:
            visitor.visit(tree)
        except RecursionError as exc:
            raise ContextError("Python AST nesting is too deep") from exc
        result.update(symbols=visitor.symbols, references=visitor.references,
                      imports=visitor.imports, parse_status="parsed", ast_nodes=count)
    return result


def _module(path):
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _dependencies(path, facts, modules):
    result = set()
    package = list(PurePosixPath(path).parent.parts)
    if package == ["."]:
        package = []
    for entry in facts["imports"]:
        if entry["level"]:
            if entry["level"] > len(package):
                continue
            base = package[:len(package) - entry["level"] + 1]
            module = ".".join([*base, *([entry["module"]] if entry["module"] else [])])
        else:
            module = entry["module"]
        candidates = {module, *(module + "." + name for name in entry["names"] if name != "*")}
        # Loading an explicit package initializer is also an import dependency.
        pieces = module.split(".")
        candidates.update(".".join(pieces[:n]) for n in range(1, len(pieces)))
        for candidate in candidates:
            result.update(modules.get(candidate, ()))
    result.discard(path)
    return sorted(result)


def _metadata(value, label):
    if not isinstance(value, Mapping) or len(value) > 64:
        raise ContextError(label + " must be a bounded explicit mapping")
    result = {}
    for key, item in value.items():
        if (not isinstance(key, str) or not 1 <= len(key) <= 80 or _secret_name(key)
                or any(term in key.casefold() for term in ("token", "private_key", "authorization", "cookie"))):
            raise ContextError(label + " contains an excluded key")
        if not isinstance(item, str) or len(item) > 1000:
            raise ContextError(label + " values must be bounded strings")
        result[key] = item
    return result


@dataclass(frozen=True)
class CompiledContext:
    manifest: dict
    artifact_hash: str
    cache_key: str
    cache_hit: bool


def compile_context(repo_root, selected_files: Sequence[str], *, store: ArtifactStore,
                    allowed_files: Sequence[str] = (), selected_symbols: Mapping[str, Sequence[str]] | None = None,
                    test_files: Sequence[str] = (), config_files: Sequence[str] = (),
                    repo_revision: str, toolchain: Mapping[str, str], environment: Mapping[str, str] | None = None,
                    policy: ContextPolicy | None = None, clock=time.time):
    """Compile selected source plus one import hop inside an explicit allowlist.

    All allowlisted files are hashed to detect changed dependencies. Only
    selected files, one-hop dependencies, and explicit tests/config enter the
    context pack. A selected_symbols mapping restricts source excerpts to exact
    Python qualified definition names; navigation facts still describe the file.
    """
    policy = policy or ContextPolicy()
    root = _root(repo_root)
    if not root.is_dir():
        raise ContextError("Repository root must be an existing directory")
    if not isinstance(repo_revision, str) or not 1 <= len(repo_revision) <= 160:
        raise ContextError("An explicit repository revision is required")
    def paths(values):
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence) or len(values) > policy.max_files:
            raise ContextError("File selections must be bounded lists")
        return sorted({_relative(item) for item in values})
    selected, allowed, tests, configs = map(paths, (selected_files, allowed_files, test_files, config_files))
    if not selected:
        raise ContextError("Select at least one source file")
    all_paths = sorted(set(selected + allowed + tests + configs))
    if len(all_paths) > policy.max_files:
        raise ContextError("File count budget exceeded")
    symbols = {}
    if selected_symbols is not None and (not isinstance(selected_symbols, Mapping) or len(selected_symbols) > policy.max_files):
        raise ContextError("Symbol selection must be a bounded mapping")
    for path, names in (selected_symbols or {}).items():
        path = _relative(path)
        if path not in selected or isinstance(names, (str, bytes)) or not isinstance(names, Sequence) or not 1 <= len(names) <= policy.max_symbols:
            raise ContextError("Symbols can only select explicitly selected source files")
        if any(not isinstance(name, str) or not 1 <= len(name) <= 240 for name in names):
            raise ContextError("Symbol names must be bounded strings")
        symbols[path] = sorted(set(names))
    chain = _metadata(toolchain, "toolchain")
    env = _metadata(environment or {}, "environment")
    chain = dict(chain, context_python=sys.implementation.name + " " + ".".join(map(str, sys.version_info[:3])))
    policy_description = {"limits": asdict(policy), "excluded_directories": sorted(DENIED_PARTS),
                          "secret_suffixes": list(SECRET_SUFFIXES), "exclusion_rules_version": 1}
    environment_hash, policy_hash = digest(canonical(env)), digest(canonical(policy_description))
    sources, used_bytes, modules = {}, 0, {}
    total_symbols = total_references = total_nodes = 0
    for path in all_paths:
        content = _read_bounded(_source_path(root, path), min(policy.max_file_bytes, policy.max_input_bytes - used_bytes))
        used_bytes += len(content)
        if b"\x00" in content:
            raise ContextError("Binary source files are not supported")
        try:
            text = content.decode("utf-8")
        except UnicodeError as exc:
            raise ContextError("Only UTF-8 source files are supported") from exc
        source_hash = digest(content)
        sources[path] = {"hash": source_hash, "bytes": content, "text": text,
                         "facts": _facts(path, text, source_hash, policy)}
        total_symbols += len(sources[path]["facts"]["symbols"])
        total_references += len(sources[path]["facts"]["references"])
        total_nodes += sources[path]["facts"]["ast_nodes"]
        if total_symbols > policy.max_symbols or total_references > policy.max_references or total_nodes > policy.max_ast_nodes:
            raise ContextError("Combined Python navigation budget exceeded")
        if path.endswith(".py"):
            modules.setdefault(_module(path), []).append(path)
    graph = {path: _dependencies(path, item["facts"], modules) for path, item in sources.items()}
    inputs = {"compiler": COMPILER_VERSION, "repo_revision": repo_revision, "toolchain": chain,
              "environment_hash": environment_hash, "policy_hash": policy_hash,
              "selected": selected, "allowed": allowed, "tests": tests, "configs": configs,
              "selected_symbols": symbols, "source_hashes": {path: item["hash"] for path, item in sources.items()},
              "dependencies": graph}
    key = digest(canonical(inputs))
    now = clock()
    if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
        raise ContextError("Cache clock must return a finite nonnegative timestamp")
    cached = store.cached(key, now, policy.max_pack_bytes)
    if cached:
        blob, manifest = cached
        if manifest.get("cache_key") != key or manifest.get("schema_version") != 1:
            raise ContextError("Cached manifest does not match its inputs")
        return CompiledContext(manifest, blob, key, True)
    expanded = set(selected + tests + configs)
    for path in selected:
        expanded.update(graph[path])
    entries = []
    # Keep writes until after validation so a byte/symbol failure leaves no source blobs.
    blobs = {}
    for path in sorted(expanded):
        item = sources[path]
        ranges = []
        if path in symbols:
            definitions = {}
            for symbol in item["facts"]["symbols"]:
                definitions.setdefault(symbol["name"], []).append(symbol)
            if any(name not in definitions for name in symbols[path]):
                raise ContextError("Selected Python symbol was not found")
            for name in symbols[path]:
                if len(definitions[name]) != 1:
                    raise ContextError("Selected Python symbol is ambiguous")
                symbol = definitions[name][0]
                start, end = symbol["start_line"], symbol["end_line"]
                ranges.append((start, end))
            merged = []
            for start, end in sorted(ranges):
                if merged and start <= merged[-1][1] + 1:
                    merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
                else:
                    merged.append((start, end))
            ranges = merged
        else:
            ranges = [(1, len(item["text"].splitlines()))] if item["text"] else []
        lines = item["text"].splitlines(keepends=True)
        excerpts = [{"start_line": start, "end_line": end, "text": "".join(lines[start - 1:end])} for start, end in ranges]
        reachable, pending = set(), list(graph[path])
        while pending:
            dependency = pending.pop()
            if dependency not in reachable and dependency != path:
                reachable.add(dependency)
                pending.extend(graph[dependency])
        dependencies = {dependency: sources[dependency]["hash"] for dependency in sorted(reachable)}
        navigation = {"schema_version": 1, "kind": "source_navigation_only", "source_hash": item["hash"],
                      "facts": item["facts"], "dependency_hashes": dependencies, "repo_revision": repo_revision,
                      "toolchain_hash": digest(canonical(chain)), "environment_hash": environment_hash,
                      "test_config_hashes": {name: sources[name]["hash"] for name in sorted(set(tests + configs))},
                      "policy_hash": policy_hash, "compiler": COMPILER_VERSION}
        navigation_bytes = canonical(navigation)
        navigation_hash = digest(navigation_bytes)
        blobs[navigation_hash], blobs[item["hash"]] = navigation_bytes, item["bytes"]
        entries.append({"path": path, "role": "selected" if path in selected else "test" if path in tests else "config" if path in configs else "dependency",
                        "source_hash": item["hash"], "source_blob": item["hash"], "source_bytes": len(item["bytes"]),
                        "navigation_blob": navigation_hash, "facts": item["facts"], "dependencies": graph[path], "excerpts": excerpts})
    manifest = {"schema_version": 1, "compiler": COMPILER_VERSION, "cache_key": key,
                "repo_revision": repo_revision, "toolchain": chain, "environment_hash": environment_hash,
                "policy_hash": policy_hash, "policy": policy_description, "input_source_hashes": inputs["source_hashes"],
                "notice": "Navigation facts are source-linked aids, not replacements for source verification.",
                "input_bytes": used_bytes, "files": entries}
    encoded = canonical(manifest)
    if len(encoded) > policy.max_pack_bytes:
        raise ContextError("Compiled context exceeds the pack byte budget; select fewer files or symbols")
    for content in blobs.values():
        store.put(content)
    blob = store.put(encoded)
    store.cache(key, blob, now + policy.cache_ttl_seconds)
    return CompiledContext(manifest, blob, key, False)
