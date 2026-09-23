"""Offline source/restore tests. The synthetic gcloud process has no network."""
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('cloud_workspace_bootstrap', Path(__file__).with_name('bootstrap.py'))
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'source'
        self.source.mkdir()
        self.home = self.root / 'home'
        self.home.mkdir()
        self.write('README.md', b'Example project.\n')
        self.write('agent_hub/main.py', b'print("not automatically executed")\n')

    def tearDown(self):
        self.assertTrue(self.source.resolve().is_relative_to(self.root))
        self.temp.cleanup()

    def write(self, relative, data):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def package(self, name='package', **kwargs):
        return bootstrap.package_snapshot(self.source, self.root / name, **kwargs)

    def archive(self, manifest, entries):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w', format=tarfile.USTAR_FORMAT) as tar:
            for name, data, kind in [(bootstrap.MANIFEST, bootstrap.canonical(manifest), tarfile.REGTYPE), *entries]:
                member = tarfile.TarInfo(name)
                member.type, member.mode = kind, 0o644
                member.size = len(data) if kind == tarfile.REGTYPE else 0
                if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                    member.linkname = '../../outside'
                tar.addfile(member, io.BytesIO(data) if member.size else None)
        return output.getvalue()

    def minimal_manifest(self):
        data = b'safe'
        return dict(schema_version=1, policy=bootstrap.POLICY,
            files=[dict(path='README.md', size=len(data), sha256=bootstrap.sha256(data))],
            total_bytes=len(data), fixture_exceptions=[])

    def synthetic_gcloud(self, program, calls):
        real_popen = subprocess.Popen
        def launch(argv, **kwargs):
            calls.append((argv, dict(kwargs)))
            return real_popen([sys.executable, '-c', program], **kwargs)
        return launch

    def test_deterministic_snapshot_and_verified_fresh_restore(self):
        first, second = self.package('first'), self.package('second')
        self.assertEqual(first['snapshot_sha256'], second['snapshot_sha256'])
        data = Path(first['archive_path']).read_bytes()
        destination = bootstrap.restore_archive(data, first['snapshot_sha256'], '12345', home=self.home)
        self.assertEqual(destination, self.home / 'myhero' / 'workspaces' / first['snapshot_sha256'])
        self.assertEqual((destination / 'README.md').read_bytes(), b'Example project.\n')
        receipt = json.loads((destination / bootstrap.ORIGIN).read_bytes())
        self.assertEqual(receipt['source_url'], first['object_prefix'] + 'source.tar#12345')
        self.assertEqual(receipt['model_calls'], 0)
        self.assertEqual(receipt['services_started'], 0)

    def test_excludes_runtime_credentials_binaries_caches_and_unrelated_trees(self):
        blocked = ['runtime/usage.json', '.git/config', 'runcrew-private/credentials.json',
                   'agent_hub/.env', 'agent_hub/auth.json', 'agent_hub/private.key',
                   'agent_hub/node_modules/a.js', 'tests/__pycache__/test.py',
                   'chatgpt/dist/plugin.zip', 'collaboration/provider-output.json',
                   'agent_hub/image.exe']
        for name in blocked:
            self.write(name, b'must not be included')
        result = self.package()
        files, _ = bootstrap.inspect_archive(Path(result['archive_path']).read_bytes(), result['snapshot_sha256'])
        self.assertEqual(set(files), {'README.md', 'agent_hub/main.py'})

    def test_detected_credential_never_appears_in_error_or_artifact(self):
        secret = 'gh' + 'p_' + 'SYNTHETIC' * 4
        self.write('agent_hub/unsafe.json', json.dumps(dict(api_key=secret)).encode())
        with self.assertRaises(bootstrap.WorkspaceError) as stopped:
            self.package()
        self.assertNotIn(secret, str(stopped.exception))
        self.assertFalse((self.root / 'package').exists())

    def test_only_reviewed_vendor_tree_is_packaged_with_same_secret_checks(self):
        self.write('vendor/ryan-frontier/ryan_frontier/domain.py', b'# reviewed finite grammar\n')
        self.write('vendor/unrelated/private.py', b'# unrelated package\n')
        files, _, _ = bootstrap.source_files(self.source)
        self.assertIn('vendor/ryan-frontier/ryan_frontier/domain.py', files)
        self.assertNotIn('vendor/unrelated/private.py', files)
        secret = 'sk' + '-' + 'SYNTHETIC' * 4
        self.write('vendor/ryan-frontier/unsafe.json', json.dumps({'api_key': secret}).encode())
        with self.assertRaises(bootstrap.WorkspaceError):
            bootstrap.source_files(self.source)

    def test_fixture_exception_is_exact_reviewed_file_not_directory_bypass(self):
        data = ('TOKEN = ' + repr('sk' + '-' + 'SYNTHETIC' * 4) + '\n').encode()
        self.write('tests/test_synthetic.py', data)
        exception = dict(path='tests/test_synthetic.py', sha256=bootstrap.sha256(data),
                         reason='Reviewed synthetic, nonfunctional test credential literal.')
        result = self.package(fixture_exceptions=[exception])
        files, raw = bootstrap.inspect_archive(Path(result['archive_path']).read_bytes(), result['snapshot_sha256'])
        self.assertEqual(files[exception['path']], data)
        self.assertEqual(json.loads(raw)['fixture_exceptions'], [exception])
        self.write(exception['path'], data + b'# edited\n')
        with self.assertRaises(bootstrap.WorkspaceError):
            self.package('changed', fixture_exceptions=[exception])
        with self.assertRaises(bootstrap.WorkspaceError):
            bootstrap.exceptions_map([dict(exception, path='agent_hub/main.py')])
        with self.assertRaises(bootstrap.WorkspaceError):
            bootstrap.exceptions_map([dict(exception, path='tests')])

    def test_refuses_existing_output_and_source_internal_artifact_folder(self):
        self.package()
        with self.assertRaises(FileExistsError):
            self.package()
        with self.assertRaises(bootstrap.WorkspaceError):
            bootstrap.package_snapshot(self.source, self.source / 'new-artifact')

    def test_symbolic_and_hard_links_are_not_packaged(self):
        outside = self.root / 'outside.py'
        outside.write_bytes(b'outside')
        linked = self.source / 'agent_hub' / 'linked.py'
        try:
            linked.symlink_to(outside)
        except (OSError, NotImplementedError):
            pass  # Windows developer-mode symlinks may be unavailable.
        else:
            with self.assertRaises(bootstrap.WorkspaceError):
                self.package('symlink')
            linked.unlink()
        os.link(outside, linked)
        with self.assertRaises(bootstrap.WorkspaceError):
            self.package('hardlink')

    def test_archive_path_traversal_links_special_files_and_duplicates_rejected(self):
        unsafe = ['../escape.py', '/tmp/escape.py', 'C:/escape.py', 'agent_hub/../escape.py',
                  'agent_hub\\escape.py', 'agent_hub//escape.py', 'agent_hub/con.py']
        for name in unsafe:
            with self.subTest(name=name):
                data = self.archive(self.minimal_manifest(), [(name, b'safe', tarfile.REGTYPE)])
                with self.assertRaises(bootstrap.WorkspaceError):
                    bootstrap.restore_archive(data, bootstrap.sha256(data), '1', home=self.home)
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.DIRTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE):
            with self.subTest(kind=kind):
                data = self.archive(self.minimal_manifest(), [('README.md', b'safe', kind)])
                with self.assertRaises(bootstrap.WorkspaceError):
                    bootstrap.inspect_archive(data, bootstrap.sha256(data))
        for names in [('README.md', 'README.md'), ('agent_hub/a.py', 'agent_hub/A.py')]:
            data = self.archive(self.minimal_manifest(), [(name, b'safe', tarfile.REGTYPE) for name in names])
            with self.assertRaises(bootstrap.WorkspaceError):
                bootstrap.inspect_archive(data, bootstrap.sha256(data))
        self.assertFalse((self.home / 'myhero').exists())

    def test_archive_manifest_digest_and_member_set_must_match(self):
        manifest = self.minimal_manifest()
        cases = [dict(manifest, total_bytes=99), dict(manifest, files=[]),
                 dict(manifest, fixture_exceptions=[dict(path='tests/missing.py', sha256='a' * 64,
                     reason='Synthetic reviewed fixture that is absent.')])]
        for candidate in cases:
            data = self.archive(candidate, [('README.md', b'safe', tarfile.REGTYPE)])
            with self.assertRaises(bootstrap.WorkspaceError):
                bootstrap.inspect_archive(data, bootstrap.sha256(data))
        data = self.archive(manifest, [('README.md', b'evil', tarfile.REGTYPE)])
        with self.assertRaises(bootstrap.WorkspaceError):
            bootstrap.inspect_archive(data, bootstrap.sha256(data))
        with self.assertRaises(bootstrap.WorkspaceError):
            bootstrap.inspect_archive(data, 'a' * 64)

    def test_restore_never_overwrites_modified_or_incomplete_workspace(self):
        result = self.package()
        data = Path(result['archive_path']).read_bytes()
        destination = bootstrap.restore_archive(data, result['snapshot_sha256'], '8', home=self.home)
        (destination / 'README.md').write_bytes(b'user edits')
        with self.assertRaises(FileExistsError):
            bootstrap.restore_archive(data, result['snapshot_sha256'], '8', home=self.home)
        self.assertEqual((destination / 'README.md').read_bytes(), b'user edits')

    def test_restore_refuses_linked_parent(self):
        result = self.package()
        outside = self.root / 'elsewhere'
        outside.mkdir()
        try:
            (self.home / 'myhero').symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('Host cannot create a test directory symlink')
        with self.assertRaises(bootstrap.WorkspaceError):
            bootstrap.restore_archive(Path(result['archive_path']).read_bytes(),
                                      result['snapshot_sha256'], '8', home=self.home)
        self.assertEqual(list(outside.iterdir()), [])

    def test_source_and_archive_size_and_json_bounds(self):
        with patch.object(bootstrap, 'MAX_FILE_BYTES', 3):
            with self.assertRaises(bootstrap.WorkspaceError):
                self.package()
        with self.assertRaises(bootstrap.WorkspaceError):
            bootstrap.strict_json(b'{"a":1,"a":2}')
        with self.assertRaises(bootstrap.WorkspaceError):
            bootstrap.strict_json(b'{"a":NaN}')
        self.write('agent_hub/nul.py', b'x\x00y')
        with self.assertRaises(bootstrap.WorkspaceError):
            self.package()

    def test_generation_bound_download_uses_argument_array_and_browser_identity(self):
        calls = []
        with patch.object(bootstrap.subprocess, 'Popen', side_effect=self.synthetic_gcloud(
                'import sys;sys.stdout.buffer.write(b"archive")', calls)):
            result = bootstrap.download_archive('a' * 64, '99887', executable='/synthetic/gcloud')
        self.assertEqual(result, b'archive')
        argv, kwargs = calls[0]
        self.assertEqual(argv, ['/synthetic/gcloud', 'storage', 'cat',
            'gs://' + bootstrap.BUCKET + '/cloud-workspaces/' + 'a' * 64 + '/source.tar#99887',
            '--project=' + bootstrap.PROJECT, '--quiet', '--verbosity=error'])
        self.assertIs(kwargs['shell'], False)
        self.assertNotIn('env', kwargs)
        self.assertFalse(any('token' in arg or 'key' in arg or 'account=' in arg for arg in argv))
        for snapshot, generation in [('a' * 64, 'latest'), ('../x', '1'), ('a' * 64, '1;echo')]:
            with self.assertRaises(bootstrap.WorkspaceError):
                bootstrap.object_url(snapshot, generation)

    def test_download_overflow_error_and_timeout_are_bounded_without_raw_logs(self):
        programs = [
            ('import sys;sys.stdout.buffer.write(b"x"*2048)', 20, 1024),
            ('import sys;sys.stderr.write("SYNTHETIC_PRIVATE_DIAGNOSTIC");sys.exit(9)', 20, 1024),
            ('import time;time.sleep(20)', 1, 1024),
        ]
        for program, timeout, cap in programs:
            calls = []
            with self.subTest(program=program), patch.object(bootstrap, 'MAX_ARCHIVE_BYTES', cap), \
                    patch.object(bootstrap.subprocess, 'Popen', side_effect=self.synthetic_gcloud(program, calls)):
                with self.assertRaises(bootstrap.WorkspaceError) as stopped:
                    bootstrap.download_archive('a' * 64, '9', timeout=timeout, executable='/synthetic/gcloud')
                self.assertNotIn('SYNTHETIC_PRIVATE_DIAGNOSTIC', str(stopped.exception))
                self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
