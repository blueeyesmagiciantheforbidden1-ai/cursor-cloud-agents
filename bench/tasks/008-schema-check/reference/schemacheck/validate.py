"""Validate dicts against a tiny schema."""

from schemacheck.errors import ValidationError

_TYPE_MAP = {
    'string': str,
    'int': int,
    'bool': bool,
    'dict': dict,
}


def validate(data, schema):
    if not isinstance(data, dict):
        raise ValidationError('data must be a dict')
    if not isinstance(schema, dict):
        raise ValidationError('schema must be a dict')
    _validate_fields(data, schema)


def _validate_fields(data, schema):
    for name, rule in schema.items():
        required = rule.get('required', True)
        typ = rule['type']
        if name not in data:
            if required:
                raise ValidationError(f'missing field: {name}')
            continue
        value = data[name]
        if typ == 'bool':
            if type(value) is not bool:
                raise ValidationError(f'field {name} must be bool')
        elif typ == 'int':
            if type(value) is not int:
                raise ValidationError(f'field {name} must be int')
        elif typ == 'string':
            if type(value) is not str:
                raise ValidationError(f'field {name} must be string')
        elif typ == 'dict':
            if type(value) is not dict:
                raise ValidationError(f'field {name} must be dict')
            fields = rule.get('fields')
            if fields is None:
                raise ValidationError(f'field {name} dict rule needs fields')
            _validate_fields(value, fields)
        else:
            raise ValidationError(f'unknown type: {typ}')
