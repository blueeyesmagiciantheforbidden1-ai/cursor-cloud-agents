"""Offline archive boundary checks; fixtures are not registered app IDs."""
import json
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from build_plugin import FILES, PLUGIN, build


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "runcrew-hub"
        for relative in FILES:
            target = self.source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((PLUGIN / relative).read_bytes())

    def test_preview_has_no_connection_or_extra_files(self):
        (self.source / "secrets.env").write_text("must-not-ship", encoding="utf-8")
        output = self.root / "preview.zip"
        build(output, skills_only=True, plugin=self.source)
        with ZipFile(output) as archive:
            self.assertEqual(set(archive.namelist()), set(FILES))
            self.assertNotIn("apps", json.loads(archive.read(FILES[0])))

    def test_binding_writes_documented_app_mapping_only_inside_archive(self):
        fixture_id = "plugin_asdk_app_" + "1" * 32
        output = self.root / "bound-fixture.zip"
        build(output, fixture_id, plugin=self.source)
        with ZipFile(output) as archive:
            self.assertEqual(set(archive.namelist()), set(FILES) | {".app.json"})
            self.assertEqual(json.loads(archive.read(".app.json")),
                             {"apps": {"runcrew-hub": {"id": fixture_id}}})
            self.assertEqual(json.loads(archive.read(FILES[0]))["apps"], "./.app.json")
        self.assertFalse((self.source / ".app.json").exists())

    def test_bad_or_missing_connection_is_rejected(self):
        for value in (None, "", "plugin_asdk_app_placeholder", "https://chatgpt.com/app", "sk-secret"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                build(self.root / "bad.zip", value, plugin=self.source)
        self.assertFalse((self.root / "bad.zip").exists())

    def test_cannot_inherit_an_unreviewed_connection(self):
        path = self.source / FILES[0]
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["mcpServers"] = {"unexpected": {"url": "https://example.invalid"}}
        path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(ValueError):
            build(self.root / "bad.zip", skills_only=True, plugin=self.source)

    def test_cannot_replace_release_or_write_inside_source(self):
        output = self.root / "existing.zip"
        output.write_bytes(b"existing")
        with self.assertRaises(FileExistsError):
            build(output, skills_only=True, plugin=self.source)
        self.assertEqual(output.read_bytes(), b"existing")
        with self.assertRaises(ValueError):
            build(self.source / "nested.zip", skills_only=True, plugin=self.source)


if __name__ == "__main__":
    unittest.main()
