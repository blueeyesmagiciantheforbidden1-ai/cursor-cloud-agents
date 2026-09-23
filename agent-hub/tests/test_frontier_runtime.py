"""Verify the actual finite runner boundary; no Google or provider requests."""
import io
import copy
import json
import os
from pathlib import Path
import platform
import sys
import tarfile
import tempfile
from unittest import TestCase, mock, skipUnless

from agent_hub import frontier_runtime as runtime


class RuntimeTests(TestCase):
    def test_pristine_vendor_source_is_bound_to_verified_archive(self):
        runtime.verify_vendor()

    def test_vendor_changes_fail_before_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'ryan_frontier').mkdir()
            (root / 'ryan_frontier' / 'domain.py').write_text('changed')
            (root / 'IMPORT_PROVENANCE.json').write_text(json.dumps({
                'archive_sha256': runtime.ARCHIVE_SHA, 'extracted': [
                    {'path': 'ryan_frontier/domain.py', 'sha256': '0' * 64}]}))
            with self.assertRaisesRegex(ValueError, 'provenance identity mismatch'):
                runtime.verify_vendor(root)

    def test_pinned_manifest_cannot_hide_modified_vendor_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); package = root / 'ryan_frontier'; package.mkdir()
            (root / 'IMPORT_PROVENANCE.json').write_bytes((runtime.VENDOR / 'IMPORT_PROVENANCE.json').read_bytes())
            for path in (runtime.VENDOR / 'ryan_frontier').glob('*.py'):
                (package / path.name).write_bytes(path.read_bytes())
            (package / 'domain.py').write_bytes(b'changed source')
            with self.assertRaisesRegex(ValueError, 'source identity mismatch'):
                runtime.verify_vendor(root)

    def test_archive_is_bounded_and_regular(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'report.json').write_text('{"scope":"finite"}')
            payload = runtime.pack_output(root)
            with tarfile.open(fileobj=io.BytesIO(payload), mode='r:gz') as archive:
                self.assertEqual(archive.getnames(), ['report.json'])
                self.assertTrue(all(item.isfile() for item in archive))
            with mock.patch.object(runtime, 'MAX_ARCHIVE', 1):
                with self.assertRaisesRegex(ValueError, 'limit'):
                    runtime.pack_output(root)

    def test_upload_requires_exact_provider_receipt_and_uses_create_only(self):
        content = b'finite evidence'
        metadata = mock.MagicMock()
        metadata.headers.get.return_value = 'Google'
        metadata.read.return_value = b'{"access_token":"synthetic-short"}'
        upload = mock.MagicMock()
        name = 'frontier/runs/check/' + runtime.digest(content) + '.tar.gz'
        import base64, hashlib
        receipt = {'bucket': runtime.BUCKET, 'name': name, 'size': str(len(content)),
            'generation': '1', 'md5Hash': base64.b64encode(hashlib.md5(content).digest()).decode()}
        upload.read.return_value = json.dumps(receipt).encode()
        opener = mock.MagicMock()
        first, second = mock.MagicMock(), mock.MagicMock()
        first.__enter__.return_value = metadata
        second.__enter__.return_value = upload
        opener.open.side_effect = [first, second]
        with mock.patch.object(runtime, 'build_opener', return_value=opener):
            result = runtime.upload_artifact('check', content)
        self.assertEqual(result['generation'], '1')
        self.assertIn('ifGenerationMatch=0', opener.open.call_args.args[0].full_url)
        self.assertEqual(result['sha256'], runtime.digest(content))
        self.assertEqual(opener.open.call_count, 2)

    @skipUnless(os.name == 'posix', 'Reviewed prototype requires POSIX fsync; exercised in cloud build')
    def test_real_child_report_gate_and_corruption_detection(self):
        parameters = {'workspace': 'verification', 'seed': 20260921, 'units': 1, 'budget': 2}
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'run'
            runtime.run_child(parameters, output)
            result = runtime.validate_output(output, parameters)
            self.assertEqual(result['model_calls'], 0)
            self.assertEqual(result['api_spend_microdollars'], 0)
            self.assertIsNone(result['active_release'])
            self.assertEqual(result['confirmation'], 'inconclusive')
            self.assertGreater(len(runtime.pack_output(output)), 0)
            sha = result['report_sha256']
            (output / 'objects' / sha).write_bytes(b'corrupted')
            with self.assertRaisesRegex(ValueError, 'digest mismatch'):
                runtime.validate_output(output, parameters)


class GraphFixture:
    """Portable synthetic CLI-shaped evidence using the real bounded report/compiler.

    This is test data, never a cloud receipt or production research claim. POSIX
    tests above separately exercise the unmodified CLI's actual disk contract.
    """
    def __init__(self, root, report):
        self.root, self.report = root, copy.deepcopy(report)
        self.parameters = {'workspace': 'verification', 'seed': 20260921, 'units': 1, 'budget': 2}
        self.run_id, self.task_id, self.proposal_id = 'a' * 32, 'b' * 32, 'c' * 32
        self.run = root / 'runs' / self.run_id
        self.objects = root / 'objects'
        self.objects.mkdir(parents=True)
        self.run.mkdir(parents=True)

    @staticmethod
    def encode(value):
        return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n').encode()

    def put(self, data):
        sha = runtime.digest(data)
        (self.objects / sha).write_bytes(data)
        return sha

    def document(self, value):
        return self.put(self.encode(value))

    def write(self, *, pipeline_edit=None, evaluator_edit=None, candidate_edit=None,
              receipt_edit=None, summary_edit=None, frozen_edit=None):
        from ryan_frontier import __version__
        sources = {p.name: self.put(p.read_bytes()) for p in (runtime.VENDOR / 'ryan_frontier').glob('*.py')}
        native_runtime = {'python': platform.python_version(), 'implementation': platform.python_implementation(),
                          'package_version': __version__, 'third_party_dependencies': []}
        pipeline = {'schema_version': 1, 'sources': sources, 'runtime': native_runtime,
                    'parameters': {k: self.parameters[k] for k in ('seed', 'units', 'budget')}}
        if pipeline_edit: pipeline_edit(pipeline)
        pipeline_sha = self.document(pipeline)
        generated = {}
        for identifier, artifact in self.report['artifacts'].items():
            for suffix, data in (('.py', artifact['source'].encode()), ('.json', self.encode(artifact['spec']))):
                name = identifier + suffix
                generated[name] = self.put(data)
                for directory in (self.run / 'generated', self.root / 'generated'):
                    directory.mkdir(exist_ok=True); (directory / name).write_bytes(data)
        report_data = self.encode(self.report); report_sha = self.put(report_data)
        (self.run / 'report.json').write_bytes(report_data); (self.root / 'report.json').write_bytes(report_data)
        evaluator = {'schema_version': 1, 'pipeline_sha256': pipeline_sha,
                     'design': self.report['design'], 'design_digest': self.report['design_digest']}
        if evaluator_edit: evaluator_edit(evaluator)
        evaluator_sha = self.document(evaluator)
        frozen = copy.deepcopy(self.report['frozen_methods'])
        if frozen_edit: frozen_edit(frozen)
        frozen_sha = self.document(frozen)
        candidate = {'schema_version': 1, 'kind': 'frozen_lineage_policy_bundle', 'frozen_methods_sha256': frozen_sha,
                     'executable_artifacts': {entry['artifact_id']: {
                         'source_sha256': generated[entry['artifact_id'] + '.py'],
                         'spec_sha256': generated[entry['artifact_id'] + '.json']}
                         for entry in self.report['frozen_methods']}}
        if candidate_edit: candidate_edit(candidate)
        candidate_sha = self.document(candidate)
        receipt = {'schema_version': 1, 'run_id': self.run_id, 'task_id': self.task_id,
                   'proposal_id': self.proposal_id, 'pipeline_sha256': pipeline_sha, 'report_sha256': report_sha,
                   'candidate_sha256': candidate_sha, 'evaluator_sha256': evaluator_sha,
                   'frozen_methods_sha256': frozen_sha, 'generated_artifacts': generated,
                   'model_calls': 0, 'api_spend_microdollars': 0}
        if receipt_edit: receipt_edit(receipt)
        receipt_data = self.encode(receipt); receipt_sha = self.put(receipt_data)
        (self.run / 'receipt.json').write_bytes(receipt_data)
        summary = {'version': __version__, **{k: self.parameters[k] for k in ('seed', 'units', 'budget')},
                   'runtime': native_runtime, 'elapsed_seconds': 0.01, 'model_calls': 0, 'api_spend_microdollars': 0,
                   'run_id': self.run_id, 'task_id': self.task_id, 'proposal_id': self.proposal_id,
                   'pipeline_sha256': pipeline_sha, 'report_sha256': report_sha, 'candidate_sha256': candidate_sha,
                   'evaluator_sha256': evaluator_sha, 'receipt_sha256': receipt_sha, 'frozen_methods_sha256': frozen_sha,
                   'generated_artifacts': generated, 'confirmation': self.report['confirmation']['decision'],
                   'promotion_status': 'inconclusive', 'active_release': None, 'lifecycle_cost': self.report['lifecycle_cost'],
                   'factorial': {c: {a: v['mean_solved_fraction'] for a, v in arms.items()}
                                 for c, arms in self.report['factorial']['summary'].items()},
                   'fixed_baseline': self.report['fixed_baseline']['summary'],
                   'output': str(self.root), 'run_directory': str(self.run), 'generated_directory': str(self.run / 'generated')}
        if summary_edit: summary_edit(summary)
        data = self.encode(summary)
        (self.run / 'summary.json').write_bytes(data); (self.root / 'summary.json').write_bytes(data)
        return summary


class GraphValidationTests(TestCase):
    @classmethod
    def setUpClass(cls):
        runtime.verify_vendor()
        sys.path.insert(0, str(runtime.VENDOR))
        from ryan_frontier.research import run_pilot
        cls.report = run_pilot(seed=20260921, units=1, budget=2)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = GraphFixture(Path(self.temporary.name), self.report)

    def validate(self):
        return runtime.validate_output(self.fixture.root, self.fixture.parameters)

    def test_complete_real_report_graph_is_accepted_without_exposing_paths(self):
        self.fixture.write()
        result = self.validate()
        self.assertEqual(result['confirmation'], 'inconclusive')
        self.assertNotIn('run_directory', result)
        self.assertNotIn('output', result)
        self.assertEqual(result['model_calls'], 0)

    def test_pipeline_omitted_source_rejected_even_with_rebound_top_level_hashes(self):
        self.fixture.write(pipeline_edit=lambda value: value['sources'].pop('routing.py'))
        with self.assertRaisesRegex(ValueError, 'pipeline source'):
            self.validate()

    def test_pipeline_request_parameters_must_match(self):
        self.fixture.write(pipeline_edit=lambda value: value['parameters'].update(seed=1))
        with self.assertRaisesRegex(ValueError, 'pipeline source or parameter'):
            self.validate()

    def test_missing_transitive_source_object_is_rejected(self):
        self.fixture.write()
        sha = runtime.verify_vendor()['domain.py']; (self.fixture.objects / sha).unlink()
        with self.assertRaisesRegex(ValueError, 'artifact missing'):
            self.validate()

    def test_evaluator_cannot_refer_to_another_pipeline(self):
        self.fixture.write(evaluator_edit=lambda value: value.update(pipeline_sha256='0' * 64))
        with self.assertRaisesRegex(ValueError, 'evaluator binding'):
            self.validate()

    def test_evaluator_design_digest_must_match_report(self):
        self.fixture.write(evaluator_edit=lambda value: value.update(design_digest='0' * 64))
        with self.assertRaisesRegex(ValueError, 'evaluator binding'):
            self.validate()

    def test_candidate_must_cover_every_frozen_executable(self):
        self.fixture.write(candidate_edit=lambda value: value.update(executable_artifacts={}))
        with self.assertRaisesRegex(ValueError, 'candidate bundle binding'):
            self.validate()

    def test_frozen_method_object_must_equal_report_lineages(self):
        self.fixture.write(frozen_edit=lambda value: value[0].update(method_digest='0' * 64))
        with self.assertRaisesRegex(ValueError, 'frozen lineage'):
            self.validate()

    def test_generated_file_bytes_cannot_differ_from_committed_spec(self):
        self.fixture.write()
        path = next((self.fixture.run / 'generated').glob('*.json'))
        path.write_bytes(b'{}\n')
        with self.assertRaisesRegex(ValueError, 'generated bytes'):
            self.validate()

    def test_generated_source_recompiled_from_spec_without_executing_it(self):
        entry = next(iter(self.fixture.report['artifacts'].values()))
        entry['source'] = 'raise RuntimeError("must never execute")\n'
        entry['sha256'] = runtime.digest(entry['source'].encode())
        self.fixture.write()
        with self.assertRaisesRegex(ValueError, 'compiled source'):
            self.validate()

    def test_candidate_spec_reference_must_match_generated_object(self):
        def change(value):
            next(iter(value['executable_artifacts'].values()))['spec_sha256'] = '0' * 64
        self.fixture.write(candidate_edit=change)
        with self.assertRaisesRegex(ValueError, 'candidate bundle binding'):
            self.validate()

    def test_receipt_task_and_proposal_and_run_identities_are_bound(self):
        self.fixture.write(receipt_edit=lambda value: value.update(task_id='f' * 32))
        with self.assertRaisesRegex(ValueError, 'receipt binding'):
            self.validate()

    def test_unreferenced_object_cannot_hide_in_valid_archive(self):
        self.fixture.write(); self.fixture.put(b'unrelated object')
        with self.assertRaisesRegex(ValueError, 'unbound objects'):
            self.validate()

    def test_total_output_bound_applies_before_reading_graph(self):
        self.fixture.write()
        with mock.patch.object(runtime, 'MAX_ARCHIVE', 10):
            with self.assertRaisesRegex(ValueError, 'archive limit'):
                self.validate()

    def test_inconclusive_report_cannot_claim_review_eligibility(self):
        self.fixture.write(summary_edit=lambda value: value.update(promotion_status='review_required'))
        with self.assertRaisesRegex(ValueError, 'decision differs'):
            self.validate()

    def test_report_metrics_cannot_be_replaced_by_summary_claims(self):
        self.fixture.write(summary_edit=lambda value: value.update(factorial={}))
        with self.assertRaisesRegex(ValueError, 'summary metrics'):
            self.validate()
