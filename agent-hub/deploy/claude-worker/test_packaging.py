"""Offline negative cases for the release and secret-isolation boundary."""
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build = load("build_profile")
entry = load("entrypoint")


class PackagingTests(unittest.TestCase):
    def manifest(self):
        return {"version": build.VERSION, "platforms": {build.PLATFORM: {
            "binary": "claude", "checksum": build.SHA256, "size": build.SIZE}}}

    def test_manifest_rejects_binary_substitution(self):
        body = self.manifest()
        body["platforms"][build.PLATFORM]["checksum"] = "0" * 64
        with self.assertRaises(build.BuildError):
            build.verify_manifest(json.dumps(body).encode())

    def test_manifest_rejects_version_drift(self):
        body = self.manifest()
        body["version"] = "2.1.276"
        with self.assertRaises(build.BuildError):
            build.verify_manifest(json.dumps(body).encode())

    def test_manifest_rejects_duplicate_keys(self):
        with self.assertRaises(build.BuildError):
            build.verify_manifest(b'{"version":"wrong","version":"2.1.275"}')

    def test_metadata_matches_explicit_pin(self):
        body = self.manifest()
        self.assertEqual(body, build.verify_manifest(json.dumps(body).encode()))

    def test_signature_rejects_other_signing_key(self):
        other = "A" * 40
        public = "pub:::::::::\nfpr:::::::::" + other + ":\n"
        with patch.object(build, "run_gpg", return_value=public):
            with self.assertRaises(build.BuildError):
                build.verify_signature("gpg", Path("home"), Path("key"), Path("sig"), Path("manifest"))

    def test_signature_requires_matching_validsig(self):
        public = "pub:::::::::\nfpr:::::::::" + build.FINGERPRINT + ":\n"
        for status in ("", "[GNUPG:] GOODSIG some-key\n", "[GNUPG:] VALIDSIG " + "A" * 40):
            with self.subTest(status=status), patch.object(build, "run_gpg", side_effect=[public, ""]), patch.object(build, "run_gpgv", return_value=status):
                with self.assertRaises(build.BuildError):
                    build.verify_signature("gpg", Path("home"), Path("key"), Path("sig"), Path("manifest"))

    def test_signature_accepts_pinned_primary(self):
        public = "pub:::::::::\nfpr:::::::::" + build.FINGERPRINT + ":\n"
        status = "[GNUPG:] VALIDSIG " + build.FINGERPRINT + " rest\n"
        with patch.object(build, "run_gpg", side_effect=[public, ""]), patch.object(build, "run_gpgv", return_value=status):
            build.verify_signature("gpg", Path("home"), Path("key"), Path("sig"), Path("manifest"))

    def test_config_keeps_pinned_policy(self):
        result = entry.prepare_config({"expected_account_ref": "a" * 64})
        self.assertTrue(result["model_policy_required"])
        self.assertEqual(result["model_policy_bundle"], "/run/config/model-catalog.json")
        self.assertEqual(result["billing_policy"], {"mode": "subscription_only"})
        self.assertEqual(result["workspaces"], {"default": "/workspace/default"})

    def test_staged_fleet_requires_explicit_valid_configuration(self):
        account = {"expected_account_ref": "a" * 64}
        self.assertEqual(entry.prepare_config(account)["model_policy_agents"],
                         ["codex", "claude", "cursor", "copilot", "grok"])
        self.assertEqual(entry.prepare_config({**account, "model_policy_agents": ["claude"]})
                         ["model_policy_agents"], ["claude"])
        for fleet in ([], ["codex"], ["claude", "claude"], ["claude", "unknown"],
                      "claude", ["claude", {}], None):
            with self.subTest(fleet=fleet), self.assertRaises(ValueError):
                entry.prepare_config({**account, "model_policy_agents": fleet})

    def test_project_work_is_fixed_by_the_cloud_package(self):
        account = {"expected_account_ref": "a" * 64}
        self.assertEqual(entry.prepare_config(account)["execution_mode"], "project_work")
        with self.assertRaises(ValueError):
            entry.prepare_config({**account, "execution_mode": "unrestricted"})

    def test_config_rejects_hub_route_and_policy_changes(self):
        for key, value in (("hub_url", "https://elsewhere.invalid"),
                           ("billing_policy", {"mode": "provider_default"}),
                           ("model_policy_required", False),
                           ("executable", "/tmp/claude"),
                           ("token_env", "OTHER_SECRET"),
                           ("model_policy_bundle", "/tmp/catalog.json")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                entry.prepare_config({"expected_account_ref": "a" * 64, key: value})

    def test_config_rejects_unbounded_execution(self):
        for timeout in (True, 0, 301, "180"):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                entry.prepare_config({"expected_account_ref": "a" * 64, "timeout_seconds": timeout})

    def test_environment_is_constructed_not_inherited(self):
        result = entry.private_environment({
            "CLAUDE_CODE_OAUTH_TOKEN": "fixture-only-provider-token", "HUB_AGENT_TOKEN": "fixture-only-hub-token",
            "ANTHROPIC_API_KEY": "must-not-inherit", "PYTHONPATH": "/tmp/evil",
            "HUB_ADMIN_TOKEN": "must-not-inherit", "HTTPS_PROXY": "https://untrusted.invalid",
            "PATH": "/tmp", "GOOGLE_APPLICATION_CREDENTIALS": "/tmp/credential.json",
        })
        self.assertEqual(set(result), {"HOME", "CLAUDE_CONFIG_DIR", "PATH", "LANG", "TMPDIR",
                                     "DISABLE_AUTOUPDATER", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
                                     "CLAUDE_CODE_OAUTH_TOKEN", "HUB_AGENT_TOKEN"})
        self.assertEqual(result["PATH"], "/usr/local/bin:/usr/bin:/bin")

    def test_missing_tokens_rejected(self):
        for values in ({}, {"HUB_AGENT_TOKEN": "fixture"}, {"CLAUDE_CODE_OAUTH_TOKEN": "fixture"}):
            with self.subTest(keys=list(values)), self.assertRaises(ValueError):
                entry.private_environment(values)

    def test_heartbeat_only_needs_no_provider_credential(self):
        result = entry.private_environment({'HUB_AGENT_TOKEN': 'fixture-hub-token'}, heartbeat_only=True)
        self.assertEqual(set(result), {'HOME', 'PATH', 'LANG', 'TMPDIR', 'HUB_AGENT_TOKEN'})
        self.assertNotIn('CLAUDE_CODE_OAUTH_TOKEN', result)

    def test_heartbeat_discards_even_supplied_provider_credentials(self):
        result = entry.private_environment({'HUB_AGENT_TOKEN': 'fixture-hub-token',
                                           'CLAUDE_CODE_OAUTH_TOKEN': 'must-not-inherit',
                                           'ANTHROPIC_API_KEY': 'must-not-inherit',
                                           'GOOGLE_APPLICATION_CREDENTIALS': '/bad/key',
                                           'HTTPS_PROXY': 'https://bad.example'}, heartbeat_only=True)
        self.assertEqual(set(result), {'HOME', 'PATH', 'LANG', 'TMPDIR', 'HUB_AGENT_TOKEN'})
        with self.assertRaises(ValueError):
            entry.private_environment({}, heartbeat_only=True)

    def test_heartbeat_flag_cannot_fall_back_to_task_polling(self):
        path = Path('/tmp/worker.json')
        self.assertEqual(entry.worker_arguments(path, heartbeat_only=True), ['--config', str(path), '--heartbeat-only'])
        self.assertEqual(entry.worker_arguments(path, heartbeat_only=False), ['--config', str(path), '--once'])


if __name__ == "__main__":
    unittest.main()
