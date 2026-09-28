"""Validated Telegram UI resources, similar to Android string resources."""
from copy import deepcopy
import json
from pathlib import Path
import string
import threading


class UiResourceError(ValueError):
    pass


DEFAULT_UI_DIR = Path(__file__).resolve().parent.parent / 'telegram-ui'
MANIFEST_NAME = 'manifest.json'
_FORMATTER = string.Formatter()
_LOCK = threading.RLock()
_CACHE = {}


def _walk(value, prefix=''):
    if isinstance(value, dict):
        for name, child in value.items():
            if not isinstance(name, str) or not name:
                raise UiResourceError('UI resource keys must be nonempty strings')
            yield from _walk(child, prefix + ('.' if prefix else '') + name)
    else:
        yield prefix, value


def _validate_templates(value, label):
    if isinstance(value, str):
        try:
            list(_FORMATTER.parse(value))
        except ValueError as exc:
            raise UiResourceError(f'{label}: invalid format template: {exc}') from exc
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_templates(child, f'{label}[{index}]')
    elif isinstance(value, dict):
        for name, child in value.items():
            _validate_templates(child, f'{label}.{name}')
    elif value is not None and not isinstance(value, (bool, int, float)):
        raise UiResourceError(f'{label}: unsupported JSON value')


class UiCatalog:
    """Load a directory of namespaced JSON resources and format them strictly."""

    def __init__(self, root=None):
        self.root = Path(root or DEFAULT_UI_DIR).expanduser().resolve()
        if not self.root.is_dir():
            raise UiResourceError(f'UI resource directory not found: {self.root}')
        manifest_path = self.root / MANIFEST_NAME
        self._values = {}
        files = sorted(path for path in self.root.rglob('*.json')
                       if path.is_file() and path != manifest_path)
        if not files:
            raise UiResourceError(f'no UI JSON resources found: {self.root}')
        for path in files:
            namespace = '.'.join(path.relative_to(self.root).with_suffix('').parts)
            try:
                body = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError) as exc:
                raise UiResourceError(f'{path}: {exc}') from exc
            if not isinstance(body, dict):
                raise UiResourceError(f'{path}: JSON object required')
            _validate_templates(body, namespace)
            for key, value in _walk(body, namespace):
                if key in self._values:
                    raise UiResourceError(f'duplicate UI resource: {key}')
                self._values[key] = value
        try:
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise UiResourceError(f'{manifest_path}: invalid or missing UI manifest: {exc}') from exc
        required = manifest.get('required') if isinstance(manifest, dict) else None
        if (manifest.get('version') if isinstance(manifest, dict) else None) != 1:
            raise UiResourceError(f'{manifest_path}: manifest version 1 required')
        if (not isinstance(required, list) or not required
                or any(not isinstance(key, str) or not key for key in required)
                or len(required) != len(set(required))):
            raise UiResourceError(f'{manifest_path}: unique nonempty required keys expected')
        missing = sorted(set(required) - self._values.keys())
        if missing:
            raise UiResourceError('missing required UI resources: ' + ', '.join(missing))

    def has(self, key):
        return key in self._values

    def value(self, key):
        try:
            return deepcopy(self._values[key])
        except KeyError as exc:
            raise UiResourceError(f'missing UI resource: {key}') from exc

    def text(self, key, **values):
        template = self.value(key)
        if not isinstance(template, str):
            raise UiResourceError(f'UI resource is not text: {key}')
        try:
            return template.format(**values)
        except (KeyError, ValueError, IndexError) as exc:
            raise UiResourceError(f'{key}: cannot format resource: {exc}') from exc

    def keys(self):
        return set(self._values)


def load_ui(root=None):
    resolved = str(Path(root or DEFAULT_UI_DIR).expanduser().resolve())
    with _LOCK:
        catalog = _CACHE.get(resolved)
        if catalog is None:
            catalog = UiCatalog(resolved)
            _CACHE[resolved] = catalog
        return catalog
