import unittest

from schemacheck import ValidationError, validate


class TestSchemaHidden(unittest.TestCase):
    def test_missing_required(self):
        with self.assertRaises(ValidationError):
            validate({}, {'name': {'type': 'string'}})

    def test_optional_absent_ok(self):
        schema = {'nick': {'type': 'string', 'required': False}}
        self.assertIsNone(validate({}, schema))

    def test_optional_wrong_type(self):
        schema = {'nick': {'type': 'string', 'required': False}}
        with self.assertRaises(ValidationError):
            validate({'nick': 1}, schema)

    def test_bool_rejects_int(self):
        schema = {'flag': {'type': 'bool'}}
        with self.assertRaises(ValidationError):
            validate({'flag': 1}, schema)
        with self.assertRaises(ValidationError):
            validate({'flag': 0}, schema)

    def test_int_rejects_bool(self):
        schema = {'n': {'type': 'int'}}
        with self.assertRaises(ValidationError):
            validate({'n': True}, schema)

    def test_nested(self):
        schema = {
            'user': {
                'type': 'dict',
                'fields': {
                    'id': {'type': 'int'},
                    'name': {'type': 'string'},
                },
            }
        }
        self.assertIsNone(validate({'user': {'id': 1, 'name': 'x'}}, schema))
        with self.assertRaises(ValidationError):
            validate({'user': {'id': 1}}, schema)

    def test_extra_keys_allowed(self):
        schema = {'a': {'type': 'int'}}
        self.assertIsNone(validate({'a': 1, 'b': 2}, schema))

    def test_validation_error_is_value_error(self):
        self.assertTrue(issubclass(ValidationError, ValueError))


if __name__ == '__main__':
    unittest.main()
