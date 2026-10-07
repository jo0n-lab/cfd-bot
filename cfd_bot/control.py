"""Read controlDict time limits without loading OpenFOAM or executing directives."""
from . import diagnostics as _diagnostics
import math
import re
from pathlib import Path


TOKEN = re.compile(r'//[^\n]*|/\*.*?\*/|#\{.*?#\}|"(?:\\.|[^"\\])*"|'
                   r'\$\{[^}]*\}|[{};()]|[^\s{};()"]+', re.S)


@_diagnostics.trace
def _entries(path, root, seen, budget):
    path = path.resolve()
    path.relative_to(root)
    if path in seen or len(seen) >= 8 or budget[0] <= 0:
        if _diagnostics.enabled: _diagnostics.step('control._entries:L14:then')
        raise ValueError('controlDict include cycle or size limit')
    with path.open(encoding='utf-8', errors='replace') as source:
        text = source.read(budget[0] + 1)
    budget[0] -= len(text)
    if budget[0] < 0:
        if _diagnostics.enabled: _diagnostics.step('control._entries:L19:then')
        raise ValueError('controlDict is too large')
    tokens = [token for token in TOKEN.findall(text)
              if not token.startswith(('//', '/*'))]
    result, index = {}, 0
    while index < len(tokens):
        if _diagnostics.enabled: _diagnostics.step('control._entries:L24:loop')
        key = tokens[index]
        index += 1
        if key == ';':
            if _diagnostics.enabled: _diagnostics.step('control._entries:L27:then')
            continue
        if index >= len(tokens):
            if _diagnostics.enabled: _diagnostics.step('control._entries:L29:then')
            raise ValueError('incomplete controlDict entry')
        if key in ('#include', '#includeIfPresent'):
            if _diagnostics.enabled: _diagnostics.step('control._entries:L31:then')
            name = tokens[index].strip('"')
            index += 1
            name = name.replace('${FOAM_CASE}', str(root)).replace('$FOAM_CASE', str(root))
            if '$' in name:
                if _diagnostics.enabled: _diagnostics.step('control._entries:L35:then')
                raise ValueError('unresolved include path')
            included = path.parent / name
            try:
                result.update(_entries(included, root, seen | {path}, budget))
            except FileNotFoundError:
                if _diagnostics.enabled: _diagnostics.step('control._entries:L40:except')
                if key != '#includeIfPresent':
                    if _diagnostics.enabled: _diagnostics.step('control._entries:L41:then')
                    raise
            continue
        if key.startswith(('#', '$')):
            if _diagnostics.enabled: _diagnostics.step('control._entries:L44:then')
            raise ValueError('unsupported top-level controlDict directive')
        if tokens[index] == '{':
            # endTime inside functions/FoamFile must not override the case limit.
            if _diagnostics.enabled: _diagnostics.step('control._entries:L46:then')
            depth = 1
            index += 1
            while index < len(tokens) and depth:
                if _diagnostics.enabled: _diagnostics.step('control._entries:L50:loop')
                depth += (tokens[index] == '{') - (tokens[index] == '}')
                index += 1
            if depth:
                if _diagnostics.enabled: _diagnostics.step('control._entries:L53:then')
                raise ValueError('unclosed controlDict dictionary')
            continue
        start = index
        while index < len(tokens) and tokens[index] != ';':
            if _diagnostics.enabled: _diagnostics.step('control._entries:L57:loop')
            index += 1
        if index == len(tokens):
            if _diagnostics.enabled: _diagnostics.step('control._entries:L59:then')
            raise ValueError('unterminated controlDict entry')
        result[key.strip('"')] = tokens[start:index]
        index += 1
    return result


@_diagnostics.trace
def _value(entries, key, seen=()):
    if key in seen or len(seen) >= 8:
        if _diagnostics.enabled: _diagnostics.step('control._value:L67:then')
        raise ValueError('controlDict variable cycle')
    tokens = entries[key]
    if len(tokens) != 1:
        if _diagnostics.enabled: _diagnostics.step('control._value:L70:then')
        raise ValueError('controlDict value requires evaluation')
    value = tokens[0].strip('"')
    if value.startswith('$'):
        if _diagnostics.enabled: _diagnostics.step('control._value:L73:then')
        return _value(entries, value[1:].strip('{}'), (*seen, key))
    return value


@_diagnostics.trace
def control_times(case):
    """Return literal/include/variable limits; unresolved inputs yield no estimate.

    Only case-local includes are read. #calc/#codeStream and other executable
    directives are never evaluated to display an ETA.
    """
    try:
        root = Path(case['_root']).resolve()
        entries = _entries(root / 'system/controlDict', root, set(), [1024 * 1024])
        stop = _value(entries, 'stopAt') if 'stopAt' in entries else 'endTime'
        if stop != 'endTime':
            if _diagnostics.enabled: _diagnostics.step('control.control_times:L88:then')
            return {'stop_at': stop}
        end = (float(case['end_time']) if case.get('end_time') is not None
               else float(_value(entries, 'endTime')))
        start = float(_value(entries, 'startTime')) if 'startTime' in entries else 0.0
        if not math.isfinite(end) or not math.isfinite(start) or end <= start:
            if _diagnostics.enabled: _diagnostics.step('control.control_times:L93:then')
            return None
        return {'start': start, 'end': end, 'stop_at': stop}
    except (KeyError, OSError, ValueError, RuntimeError):
        if _diagnostics.enabled: _diagnostics.step('control.control_times:L96:except')
        return None
