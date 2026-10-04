"""Global, user-editable Telegram text templates."""
import json
import string
from pathlib import Path

from .ui import load_ui


FIELDS = {
    'case_name', 'status', 'case_root', 'run_id', 'owner', 'cores', 'cpu_list',
    'started_at', 'finished_at', 'elapsed', 'simulation_time', 'clock_time',
    'eta', 'progress', 'return_code', 'reason', 'observed', 'log_path',
    'residuals', 'errors', 'tail', 'postprocess_errors', 'monitor_errors'
}

def _validate(name, template):
    if not isinstance(template, str) or not template.strip():
        raise ValueError(f'text.{name}: nonempty string required')
    try:
        parsed = string.Formatter().parse(template)
        for _, field, spec, conversion in parsed:
            if field is None:
                continue
            if field not in FIELDS or spec or conversion:
                raise ValueError(f'text.{name}: unsupported placeholder: {field}')
    except ValueError:
        raise
    return template


def load_text(path=None, ui=None):
    catalog = ui or load_ui()
    if path is None:
        return {
            'version': 1,
            'detail': _validate('detail', catalog.value('scenarios.run.detail')),
            'completion': _validate('completion', catalog.value('scenarios.run.completion')),
        }
    path = Path(path)
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError(f'{path}: {exc}') from exc
    if not isinstance(value, dict):
        raise ValueError(f'{path}: JSON object required')
    unknown = set(value) - {'version', 'detail', 'completion'}
    if unknown:
        raise ValueError(f'{path}: unknown keys: {", ".join(sorted(unknown))}')
    if value.get('version') != 1:
        raise ValueError(f'{path}: version must be 1')
    return {
        'version': 1,
        'detail': _validate('detail', value.get('detail')),
        'completion': _validate('completion', value.get('completion')),
    }
