"""Diagnostics made only from fixed tokens, never exception messages or inputs."""
from pydantic import ValidationError
from .config import Settings

ERROR_CLASSES = frozenset({
    'ValidationError', 'DatabaseConfigurationError', 'OperationalError',
    'ProgrammingError', 'IntegrityError', 'DataError', 'InterfaceError',
    'ImportError', 'ModuleNotFoundError', 'CommandError', 'RuntimeError',
    'ValueError', 'TypeError', 'UnknownError',
})
SQLSTATES = frozenset({'28P01', '28000', '3D000', '42501', '42P01', '42P07',
                       '42710', '23505', '23503', '08001', '08006', '42601'})


def safe_migration_error(exc):
    name = type(exc).__name__
    lines = ['Migration error: ' + (name if name in ERROR_CLASSES else 'UnknownError')]
    if isinstance(exc, ValidationError):
        for error in exc.errors(include_input=False, include_context=False, include_url=False):
            loc = error['loc']
            if loc and loc[0] in Settings.model_fields:
                lines.append('Configuration field: ' + loc[0])
    code = getattr(getattr(exc, 'orig', exc), 'sqlstate', None)
    if code in SQLSTATES:
        lines.append('SQLSTATE: ' + code)
    return '\n'.join(dict.fromkeys(lines))
