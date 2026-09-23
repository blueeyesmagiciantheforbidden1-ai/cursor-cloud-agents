"""Guards the invariant behind provider_errors, not just today's five providers.

Provider modules need image-only dependencies, so the enumeration is static.
"""
import ast
from pathlib import Path
import unittest

from provider_errors import ProviderCodeError, error_code

PROVIDERS = Path(__file__).resolve().parent / 'providers'
BUILTIN_EXCEPTIONS = {'BaseException', 'Exception', 'RuntimeError', 'ValueError', 'OSError',
                      'KeyError', 'TypeError', 'LookupError', 'ArithmeticError'}


def base_name(node):
    return node.attr if isinstance(node, ast.Attribute) else getattr(node, 'id', None)


def exception_classes(path):
    """{class name: base names} for every exception class defined at module level."""
    found = {}
    for node in ast.parse(path.read_text(encoding='utf-8')).body:
        if isinstance(node, ast.ClassDef):
            bases = [base_name(base) for base in node.bases]
            if any(base in BUILTIN_EXCEPTIONS or base in found for base in bases):
                found[node.name] = bases
    return found


class ProviderErrorTests(unittest.TestCase):
    def test_every_provider_exception_opts_into_the_marker(self):
        # A new provider that forgets the marker would have its codes flattened
        # to native_or_connection_failure again; this names the class instead.
        seen = 0
        for path in sorted(PROVIDERS.glob('*.py')):
            classes = exception_classes(path)
            for name, bases in classes.items():
                if any(base in classes for base in bases):
                    continue  # inherits from a checked class in the same module
                seen += 1
                self.assertIn('ProviderCodeError', bases,
                              path.name + ':' + name + ' must inherit provider_errors.ProviderCodeError')
        self.assertGreaterEqual(seen, 5)

    def test_marker_keeps_original_base_and_handlers(self):
        class Runtime(ProviderCodeError, RuntimeError): pass
        class Value(ProviderCodeError, ValueError): pass
        self.assertIsInstance(Runtime('code'), RuntimeError)
        self.assertIsInstance(Value('code'), ValueError)
        self.assertNotIsInstance(Runtime('code'), ValueError)
        self.assertEqual(error_code(Runtime('grok_period_shape')), 'grok_period_shape')

    def test_only_fixed_codes_cross_the_boundary(self):
        class Code(ProviderCodeError, RuntimeError): pass
        for text in ('codex_failed\n/home/worker/.codex/auth.json', 'Bad', 'x' * 101, '', '1abc', 'a-b'):
            with self.subTest(text=text):
                self.assertIsNone(error_code(Code(text)))
        self.assertEqual(error_code(Code('a' * 100)), 'a' * 100)
        self.assertIsNone(error_code(RuntimeError('task_deadline_out_of_bounds')))
        self.assertIsNone(error_code(ValueError('native_not_running')))
