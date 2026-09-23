"""Offline Cursor release and no-inference package boundary tests."""
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def load(name):
    spec = importlib.util.spec_from_file_location('cursor_' + name, HERE / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build = load('build_native')
entry = load('entrypoint')


class CursorPackagingTests(unittest.TestCase):
    def receipt(self):
        return json.loads((HERE / 'provenance/release.json').read_text())

    def test_recorded_official_archive_is_pinned(self):
        record = build.validate_release(self.receipt())
        self.assertEqual(record['sha256'], entry.ARCHIVE_SHA256)
        self.assertEqual(record['version'], entry.VERSION)
        self.assertFalse(record['publisher_signature_verified'])
        self.assertFalse(record['executed'])

    def test_release_substitution_is_rejected(self):
        for key, value in (('sha256', '0' * 64), ('version', 'latest'), ('platform', 'win32-x64'),
                           ('url', 'https://other.invalid/file'), ('bytes', 1),
                           ('publisher_signature_verified', True), ('provenance', 'unverified')):
            with self.subTest(key=key), self.assertRaises(build.ReleaseError):
                build.validate_release({**self.receipt(), key: value})

    def member(self, name, kind=tarfile.REGTYPE):
        row = tarfile.TarInfo(name)
        row.type = kind
        return row

    def test_archive_path_escape_and_special_files_rejected(self):
        for row in (self.member('/etc/passwd'), self.member('dist-package/../../escape'),
                    self.member('dist-package\\evil'), self.member('dist-package/link', tarfile.SYMTYPE),
                    self.member('dist-package/hardlink', tarfile.LNKTYPE),
                    self.member('dist-package/device', tarfile.CHRTYPE)):
            with self.subTest(name=row.name), self.assertRaises(build.ReleaseError):
                build.safe_members([row])

    def test_duplicate_and_multiple_distribution_roots_rejected(self):
        for rows in ([self.member('dist-package/node'), self.member('dist-package/node')],
                     [self.member('dist-package/node'), self.member('other/file')],
                     [self.member('dist-package/a//b'), self.member('dist-package/a/b')]):
            with self.assertRaises(build.ReleaseError):
                build.safe_members(rows)

    def test_archive_expansion_bound_enforced(self):
        row = self.member('dist-package/big')
        row.size = build.MAX_EXPANDED_BYTES + 1
        with self.assertRaises(build.ReleaseError):
            build.safe_members([row])

    def test_observed_distribution_contains_expected_native_elf(self):
        audit = json.loads((HERE / 'provenance/native-audit.json').read_text())
        self.assertEqual(audit['safe_extraction_member_count'], len(self.receipt()['files']))
        self.assertTrue(audit['artifacts']['node']['elf'])
        self.assertTrue(audit['artifacts']['cursor-agent-sea']['elf'])
        self.assertFalse(audit['artifacts']['cursor-agent']['elf'])
        self.assertIn('index.js', audit['artifacts']['cursor-agent']['text'])

    def test_heartbeat_config_cannot_enable_task_execution_or_change_hub(self):
        account = {'expected_account_ref': 'a' * 64}
        value = entry.prepare_config(account)
        self.assertEqual(value['agent_id'], 'cursor')
        self.assertEqual(value['billing_policy'], {'mode': 'subscription_only'})
        self.assertEqual(value['execution_mode'], 'read_only')
        self.assertTrue(value['model_policy_required'])
        for key in ('hub_url', 'executable', 'execution_mode', 'model_policy_required', 'token_env'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                entry.prepare_config({**account, key: 'override'})

    def test_owner_and_worker_labels_are_bounded(self):
        for record in ({}, {'expected_account_ref': 'owner@example.com'},
                       {'expected_account_ref': 'a'*64, 'worker_id': '../bad'}):
            with self.assertRaises(ValueError):
                entry.prepare_config(record)

    def test_heartbeat_environment_never_receives_provider_secrets(self):
        environment = entry.private_environment({'HUB_AGENT_TOKEN': 'fixture-hub',
            'CURSOR_API_KEY': 'never-copy', 'ANTHROPIC_API_KEY': 'never-copy',
            'NODE_OPTIONS': '--require evil', 'CURSOR_CONFIG_DIR': '/bad', 'HTTPS_PROXY': 'bad',
            'GOOGLE_APPLICATION_CREDENTIALS': '/bad/key', 'HUB_ADMIN_TOKEN': 'never-copy'})
        self.assertEqual(set(environment), {'HOME', 'PATH', 'LANG', 'TMPDIR', 'HUB_AGENT_TOKEN'})

    def test_missing_malformed_hub_token_rejected(self):
        for token in ('', 'bad token', 'a\n', '\x00', None):
            with self.subTest(token=token), self.assertRaises(ValueError):
                entry.private_environment({'HUB_AGENT_TOKEN': token})

    def test_default_does_not_claim_or_launch_provider(self):
        with patch.object(entry, 'image_check', return_value={}), patch.object(entry, 'private_environment') as environment, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(entry.main([]), 2)
        environment.assert_not_called()
        record = json.loads(output.getvalue())
        self.assertEqual(record['status'], 'task_execution_blocked')
        self.assertFalse(record['claims_tasks'])
        self.assertEqual(record['provider_calls'], 0)

    def test_check_is_offline_and_does_not_read_credentials(self):
        with patch.object(entry, 'image_check', return_value={'image_profile_verified': True}), patch.object(entry, 'private_environment') as environment, redirect_stdout(io.StringIO()):
            self.assertEqual(entry.main(['--check']), 0)
        environment.assert_not_called()

    def test_ignore_list_excludes_cache_and_auth_sources(self):
        text = (HERE / 'Dockerfile.dockerignore').read_text()
        self.assertEqual(text.splitlines()[0], '**')
        self.assertNotIn('!.cache', text)
        self.assertNotIn('cursor.txt', text)
        self.assertNotIn('!runcrew-private', text)

    def test_native_cli_has_no_assumed_effort_switch(self):
        audit = json.loads((HERE / 'provenance/cli-contract-audit.json').read_text())
        self.assertEqual(audit['--effort'], [])
        self.assertEqual(audit['--reasoning-effort'], [])

    def test_source_contract_is_audit_only_not_fixed_effort_proof(self):
        audit = json.loads((HERE / 'provenance/metadata-contract.json').read_text())
        self.assertEqual(audit['archive_sha256'], entry.ARCHIVE_SHA256)
        self.assertFalse(audit['executed'])
        self.assertFalse(audit['fixed_composer_effort_proven'])
        self.assertFalse(audit['sea_execution_equivalence_verified'])
        self.assertEqual(audit['acp_methods'], ['initialize', 'cursor/list_available_models'])
        self.assertEqual(sum(len(row['verified_source_offsets']) for row in audit['observations'].values()), 23)

    def test_metadata_entrypoint_does_not_require_hub_token_or_claim(self):
        config = Mock()
        config.is_symlink.return_value = False
        config.stat.return_value.st_size = 100
        config.read_text.return_value = json.dumps({'expected_account_ref': 'a' * 64})
        collector = Mock(return_value={'task_execution_enabled': False, 'inference_performed': False})
        module = SimpleNamespace(collect_metadata=collector, MetadataError=type('FixtureMetadataError', (ValueError,), {}))
        with patch.object(entry, 'image_check', return_value={}), patch.object(entry, 'CONFIG', config), \
                patch.dict(sys.modules, {'metadata': module}), patch.dict(entry.os.environ, {'CURSOR_API_KEY': 'fixture-secret'}, clear=True), \
                patch.object(entry.os, 'umask'), patch.object(entry, 'private_environment') as heartbeat, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(entry.main(['--metadata-only']), 0)
            self.assertNotIn('CURSOR_API_KEY', entry.os.environ)
        collector.assert_called_once_with('fixture-secret', 'a' * 64)
        heartbeat.assert_not_called()
        self.assertFalse(json.loads(out.getvalue())['task_execution_enabled'])


if __name__ == '__main__':
    unittest.main()
