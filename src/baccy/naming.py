import string

_FIELDS = {
    'project',
    'session',
    'device',
    'track',
    'channels',
    'timestamp',
    'rule',
    'extension',
}


def validate_filename_template(value: str) -> str:
    if not value:
        raise ValueError('upload filename must not be empty')
    try:
        fields = list(string.Formatter().parse(value))
    except ValueError as error:
        raise ValueError(f'invalid upload filename template: {error}') from error
    for _, field, specification, conversion in fields:
        if field is not None and field not in _FIELDS:
            raise ValueError(f'unknown upload filename field: {field}')
        if specification or conversion:
            raise ValueError('upload filename fields do not support formatting')
    return value
