import unittest

from schemacheck import ValidationError, validate


class TestSchemaVisible(unittest.TestCase):
    def test_ok(self):
        schema = {'name': {'type': 'string'}, 'age': {'type': 'int'}}
        self.assertIsNone(validate({'name': 'Ada', 'age': 36}, schema))


if __name__ == '__main__':
    unittest.main()
