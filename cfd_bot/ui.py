"""Validated Telegram UI resources, similar to Android string resources."""
from . import diagnostics as _diagnostics
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


@_diagnostics.trace
def _walk(value, prefix=''):
    if isinstance(value, dict):
        if _diagnostics.detailed: _diagnostics.step('ui._walk:L21:then')
        for name, child in value.items():
            if _diagnostics.detailed: _diagnostics.step('ui._walk:L22:loop', name=name, child=child)
            if not isinstance(name, str) or not name:
                if _diagnostics.detailed: _diagnostics.step('ui._walk:L23:then')
                raise UiResourceError('UI resource keys must be nonempty strings')
            yield from _walk(child, prefix + ('.' if prefix else '') + name)
    else:
        if _diagnostics.detailed: _diagnostics.step('ui._walk:L21:else')
        yield prefix, value


@_diagnostics.trace
def _validate_templates(value, label):
    if isinstance(value, str):
        if _diagnostics.detailed: _diagnostics.step('ui._validate_templates:L31:then')
        try:
            list(_FORMATTER.parse(value))
        except ValueError as exc:
            if _diagnostics.enabled: _diagnostics.step('ui._validate_templates:L34:except')
            raise UiResourceError(f'{label}: invalid format template: {exc}') from exc
    elif isinstance(value, list):
        if _diagnostics.detailed: _diagnostics.step('ui._validate_templates:L36:then')
        for index, child in enumerate(value):
            if _diagnostics.detailed: _diagnostics.step('ui._validate_templates:L37:loop', index=index, child=child)
            _validate_templates(child, f'{label}[{index}]')
    elif isinstance(value, dict):
        if _diagnostics.detailed: _diagnostics.step('ui._validate_templates:L39:then')
        for name, child in value.items():
            if _diagnostics.detailed: _diagnostics.step('ui._validate_templates:L40:loop', name=name, child=child)
            _validate_templates(child, f'{label}.{name}')
    elif value is not None and not isinstance(value, (bool, int, float)):
        if _diagnostics.detailed: _diagnostics.step('ui._validate_templates:L42:then')
        raise UiResourceError(f'{label}: unsupported JSON value')


class UiCatalog:
    """Load a directory of namespaced JSON resources and format them strictly."""

    @_diagnostics.trace
    def __init__(self, root=None):
        self.root = Path(root or DEFAULT_UI_DIR).expanduser().resolve()
        if not self.root.is_dir():
            if _diagnostics.detailed: _diagnostics.step('ui.UiCatalog.__init__:L51:then')
            raise UiResourceError(f'UI resource directory not found: {self.root}')
        manifest_path = self.root / MANIFEST_NAME
        self._values = {}
        files = sorted(path for path in self.root.rglob('*.json')
                       if path.is_file() and path != manifest_path)
        if not files:
            if _diagnostics.detailed: _diagnostics.step('ui.UiCatalog.__init__:L57:then')
            raise UiResourceError(f'no UI JSON resources found: {self.root}')
        for path in files:
            if _diagnostics.detailed: _diagnostics.step('ui.UiCatalog.__init__:L59:loop', path=path)
            namespace = '.'.join(path.relative_to(self.root).with_suffix('').parts)
            try:
                body = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError) as exc:
                if _diagnostics.enabled: _diagnostics.step('ui.UiCatalog.__init__:L63:except')
                raise UiResourceError(f'{path}: {exc}') from exc
            if not isinstance(body, dict):
                if _diagnostics.detailed: _diagnostics.step('ui.UiCatalog.__init__:L65:then')
                raise UiResourceError(f'{path}: JSON object required')
            _validate_templates(body, namespace)
            for key, value in _walk(body, namespace):
                if _diagnostics.detailed: _diagnostics.step('ui.UiCatalog.__init__:L68:loop', key=key, value=value)
                if key in self._values:
                    if _diagnostics.detailed: _diagnostics.step('ui.UiCatalog.__init__:L69:then')
                    raise UiResourceError(f'duplicate UI resource: {key}')
                self._values[key] = value
        try:
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            if _diagnostics.enabled: _diagnostics.step('ui.UiCatalog.__init__:L74:except')
            raise UiResourceError(f'{manifest_path}: invalid or missing UI manifest: {exc}') from exc
        required = manifest.get('required') if isinstance(manifest, dict) else None
        if (manifest.get('version') if isinstance(manifest, dict) else None) != 1:
            if _diagnostics.detailed: _diagnostics.step('ui.UiCatalog.__init__:L77:then')
            raise UiResourceError(f'{manifest_path}: manifest version 1 required')
        if (not isinstance(required, list) or not required
                or any(not isinstance(key, str) or not key for key in required)
                or len(required) != len(set(required))):
            if _diagnostics.detailed: _diagnostics.step('ui.UiCatalog.__init__:L79:then')
            raise UiResourceError(f'{manifest_path}: unique nonempty required keys expected')
        missing = sorted(set(required) - self._values.keys())
        if missing:
            if _diagnostics.detailed: _diagnostics.step('ui.UiCatalog.__init__:L84:then')
            raise UiResourceError('missing required UI resources: ' + ', '.join(missing))

    @_diagnostics.trace
    def has(self, key):
        return key in self._values

    @_diagnostics.trace
    def value(self, key):
        try:
            return deepcopy(self._values[key])
        except KeyError as exc:
            if _diagnostics.enabled: _diagnostics.step('ui.UiCatalog.value:L93:except')
            raise UiResourceError(f'missing UI resource: {key}') from exc

    @_diagnostics.trace
    def text(self, key, **values):
        template = self.value(key)
        if not isinstance(template, str):
            if _diagnostics.detailed: _diagnostics.step('ui.UiCatalog.text:L98:then')
            raise UiResourceError(f'UI resource is not text: {key}')
        try:
            return template.format(**values)
        except (KeyError, ValueError, IndexError) as exc:
            if _diagnostics.enabled: _diagnostics.step('ui.UiCatalog.text:L102:except')
            raise UiResourceError(f'{key}: cannot format resource: {exc}') from exc

    @_diagnostics.trace
    def keys(self):
        return set(self._values)


@_diagnostics.trace
def load_ui(root=None):
    resolved = str(Path(root or DEFAULT_UI_DIR).expanduser().resolve())
    with _LOCK:
        catalog = _CACHE.get(resolved)
        if catalog is None:
            if _diagnostics.detailed: _diagnostics.step('ui.load_ui:L113:then')
            catalog = UiCatalog(resolved)
            _CACHE[resolved] = catalog
        return catalog
