from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import unittest

from agent_hub.context import ArtifactStore, ContextError, ContextPolicy, compile_context, digest


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.repo = self.base / "repository"
        self.repo.mkdir()
        self.store = ArtifactStore(self.base / "artifacts")

    def write(self, name, content):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="")

    def compile(self, files=("main.py",), **kwargs):
        options = dict(store=self.store, repo_revision="test-revision", toolchain={"compiler": "test"},
                       environment={"mode": "test"}, clock=lambda: 1000)
        options.update(kwargs)
        return compile_context(self.repo, files, **options)

    def by_path(self, result):
        return {item["path"]: item for item in result.manifest["files"]}

    def test_dependency_change_invalidates_pack_and_dependent_navigation(self):
        self.write("main.py", "import helper\n\ndef run():\n    return helper.answer()\n")
        self.write("helper.py", "def answer():\n    return 1\n")
        first = self.compile(allowed_files=["helper.py"])
        cached = self.compile(allowed_files=["helper.py"])
        self.assertTrue(cached.cache_hit)
        self.assertEqual(first.artifact_hash, cached.artifact_hash)
        self.write("helper.py", "def answer():\n    return 2\n")
        changed = self.compile(allowed_files=["helper.py"])
        self.assertFalse(changed.cache_hit)
        self.assertNotEqual(first.cache_key, changed.cache_key)
        a, b = self.by_path(first)["main.py"], self.by_path(changed)["main.py"]
        self.assertEqual(a["source_hash"], b["source_hash"])
        self.assertNotEqual(a["navigation_blob"], b["navigation_blob"])
        navigation = json.loads(self.store.get(b["navigation_blob"]))
        self.assertEqual(navigation["dependency_hashes"]["helper.py"], self.by_path(changed)["helper.py"]["source_hash"])

    def test_transitive_change_invalidates_navigation_but_expansion_is_one_hop(self):
        self.write("main.py", "import helper\n")
        self.write("helper.py", "import third\n")
        self.write("third.py", "value = 1\n")
        first = self.compile(allowed_files=["helper.py", "third.py"])
        self.assertEqual(set(self.by_path(first)), {"main.py", "helper.py"})
        self.write("third.py", "value = 2\n")
        changed = self.compile(allowed_files=["helper.py", "third.py"])
        self.assertNotEqual(self.by_path(first)["main.py"]["navigation_blob"], self.by_path(changed)["main.py"]["navigation_blob"])

    def test_only_explicit_files_are_read_and_unlisted_import_is_not_expanded(self):
        self.write("main.py", "import unrelated\n")
        self.write("unrelated.py", "raise RuntimeError('must never execute')\n")
        self.write(".env", "PRIVATE=yes\n")
        result = self.compile()
        self.assertEqual(set(result.manifest["input_source_hashes"]), {"main.py"})
        self.assertEqual(self.by_path(result)["main.py"]["dependencies"], [])

    def test_symbol_selection_includes_decorator_and_records_callers(self):
        self.write("main.py", "def other():\n    return 'excluded body'\n\n@decorate\ndef chosen(value):\n    return helper.call(value)\n")
        result = self.compile(selected_symbols={"main.py": ["chosen"]})
        entry = self.by_path(result)["main.py"]
        self.assertEqual(entry["excerpts"], [{"start_line": 4, "end_line": 6,
                          "text": "@decorate\ndef chosen(value):\n    return helper.call(value)\n"}])
        self.assertIn({"kind": "call", "name": "helper.call", "caller": "chosen", "line": 6}, entry["facts"]["references"])
        self.assertEqual(entry["facts"]["source_hash"], entry["source_hash"])
        self.assertIn(b"excluded body", self.store.get(entry["source_blob"]))

    def test_unknown_and_ambiguous_symbols_fail(self):
        self.write("main.py", "def repeated():\n    pass\ndef repeated():\n    pass\n")
        for name in ("missing", "repeated"):
            with self.subTest(name=name), self.assertRaises(ContextError):
                self.compile(selected_symbols={"main.py": [name]})

    def test_relative_import_and_explicit_test_config(self):
        self.write("pkg/main.py", "from .helper import answer\n")
        self.write("pkg/helper.py", "def answer():\n    return 1\n")
        self.write("tests/test_main.py", "from pkg.main import answer\n")
        self.write("pyproject.toml", "[tool.example]\nmode = 'safe'\n")
        result = self.compile(["pkg/main.py"], allowed_files=["pkg/helper.py"],
                              test_files=["tests/test_main.py"], config_files=["pyproject.toml"])
        entries = self.by_path(result)
        self.assertEqual(entries["pkg/main.py"]["dependencies"], ["pkg/helper.py"])
        self.assertEqual(entries["tests/test_main.py"]["role"], "test")
        self.assertEqual(entries["pyproject.toml"]["role"], "config")
        self.assertEqual(entries["pyproject.toml"]["facts"]["parse_status"], "not_applicable")

    def test_explicit_config_change_invalidates_source_navigation(self):
        self.write("main.py", "value = 1\n")
        self.write("settings.toml", "mode = 'first'\n")
        first = self.compile(config_files=["settings.toml"])
        self.write("settings.toml", "mode = 'second'\n")
        changed = self.compile(config_files=["settings.toml"])
        self.assertNotEqual(self.by_path(first)["main.py"]["navigation_blob"], self.by_path(changed)["main.py"]["navigation_blob"])

    def test_duplicate_source_blobs_are_deduplicated(self):
        content = "def identity(value):\n    return value\n"
        self.write("main.py", content)
        self.write("duplicate.py", content)
        result = self.compile(["main.py", "duplicate.py"])
        entries = self.by_path(result)
        self.assertEqual(entries["main.py"]["source_blob"], entries["duplicate.py"]["source_blob"])
        key = digest(content.encode())
        self.assertEqual(len(list((self.store.root / "blobs").glob(key))), 1)
        self.assertEqual(self.store.get(key), content.encode())

    def test_concurrent_identical_blobs_publish_complete_content_once(self):
        content = b"test fixture\n" * 1000
        with ThreadPoolExecutor(max_workers=4) as executor:
            addresses = list(executor.map(self.store.put, [content] * 8))
        self.assertEqual(set(addresses), {digest(content)})
        self.assertEqual(self.store.get(addresses[0]), content)
        self.assertEqual(len(list((self.store.root / "blobs").iterdir())), 1)

    def test_secret_names_and_path_escapes_are_rejected_before_read(self):
        for path in (".env", ".env.production", "credentials.json", "API_KEYS/main.py", ".git/config",
                     "nested/private.key", "passwords.txt", "../outside.py", "C:/outside.py", "/outside.py", "folder/./main.py"):
            with self.subTest(path=path), self.assertRaises(ContextError):
                self.compile([path])
        with self.assertRaises(ContextError):
            ArtifactStore(self.base / "API_KEYS" / "cache")

    def test_symlink_escape_is_rejected(self):
        outside = self.base / "outside.py"
        outside.write_text("private_value = 42\n", encoding="utf-8")
        try:
            (self.repo / "main.py").symlink_to(outside)
        except OSError as exc:
            self.skipTest("OS does not permit symlink creation: " + str(exc.errno))
        with self.assertRaises(ContextError):
            self.compile()

    def test_symlinked_directory_is_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "main.py").write_text("value = 42\n", encoding="utf-8")
        try:
            (self.repo / "linked").symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest("OS does not permit symlink creation: " + str(exc.errno))
        with self.assertRaises(ContextError):
            self.compile(["linked/main.py"])

    def test_byte_budgets_reject_without_partial_source_blobs(self):
        self.write("main.py", "value = '" + "a" * 300 + "'\n")
        policies = [replace(ContextPolicy(), max_file_bytes=100), replace(ContextPolicy(), max_input_bytes=100),
                    replace(ContextPolicy(), max_pack_bytes=100)]
        for policy in policies:
            with self.subTest(policy=policy), self.assertRaises(ContextError):
                self.compile(policy=policy)
            self.assertEqual(list((self.store.root / "blobs").iterdir()), [])

    def test_combined_navigation_budget_is_enforced(self):
        self.write("main.py", "def a():\n    return value\n")
        self.write("second.py", "def b():\n    return value\n")
        with self.assertRaises(ContextError):
            self.compile(["main.py", "second.py"], policy=replace(ContextPolicy(), max_symbols=1))

    def test_binary_and_invalid_utf8_are_rejected(self):
        for content in (b"value\x00", b"\xff\xfe"):
            (self.repo / "main.py").write_bytes(content)
            with self.assertRaises(ContextError):
                self.compile()

    def test_ttl_recomputes_deterministically_and_input_changes_miss(self):
        self.write("main.py", "value = 1\n")
        first = self.compile()
        self.assertTrue(self.compile(clock=lambda: 1299).cache_hit)
        expired = self.compile(clock=lambda: 1300)
        self.assertFalse(expired.cache_hit)
        self.assertEqual(first.artifact_hash, expired.artifact_hash)
        for changed in ({"toolchain": {"compiler": "next"}}, {"environment": {"mode": "prod"}}, {"repo_revision": "next"},
                        {"policy": replace(ContextPolicy(), max_files=63)}):
            with self.subTest(changed=changed):
                value = self.compile(**changed)
                self.assertFalse(value.cache_hit)
                self.assertNotEqual(first.cache_key, value.cache_key)

    def test_selection_order_does_not_change_pack(self):
        self.write("main.py", "value = 1\n")
        self.write("other.py", "value = 2\n")
        first = self.compile(["main.py", "other.py"])
        second = self.compile(["other.py", "main.py", "main.py"])
        self.assertEqual(first.artifact_hash, second.artifact_hash)
        self.assertTrue(second.cache_hit)

    def test_environment_values_are_hashed_and_secret_keys_rejected(self):
        self.write("main.py", "value = 1\n")
        result = self.compile(environment={"build_mode": "a_private_configuration_label"})
        self.assertNotIn("a_private_configuration_label", json.dumps(result.manifest))
        for key in ("OPENAI_API_KEY", "HUB_TOKEN", "PASSWORD", "AUTHORIZATION"):
            with self.subTest(key=key), self.assertRaises(ContextError):
                self.compile(environment={key: "never stored"})

    def test_corrupt_blob_is_not_returned_as_verified_source(self):
        self.write("main.py", "value = 1\n")
        result = self.compile()
        blob = self.by_path(result)["main.py"]["source_blob"]
        (self.store.root / "blobs" / blob).write_bytes(b"corrupted")
        with self.assertRaises(ContextError):
            self.store.get(blob)

    def test_syntax_error_is_explicit_and_source_remains_available(self):
        self.write("main.py", "def broken(\n")
        result = self.compile()
        entry = self.by_path(result)["main.py"]
        self.assertEqual(entry["facts"]["parse_status"], "unparsed")
        self.assertEqual(self.store.get(entry["source_blob"]), b"def broken(\n")


if __name__ == "__main__":
    unittest.main()
