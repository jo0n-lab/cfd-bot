"""Read controlDict time limits without loading OpenFOAM or executing directives."""
import math
import re
from pathlib import Path


TOKEN = re.compile(r'//[^\n]*|/\*.*?\*/|#\{.*?#\}|"(?:\\.|[^"\\])*"|'
                   r'\$\{[^}]*\}|[{};()]|[^\s{};()"]+', re.S)


def _entries(path, root, seen, budget):
    path = path.resolve()
    path.relative_to(root)
    if path in seen or len(seen) >= 8 or budget[0] <= 0:
        raise ValueError('controlDict include cycle or size limit')
    with path.open(encoding='utf-8', errors='replace') as source:
        text = source.read(budget[0] + 1)
    budget[0] -= len(text)
    if budget[0] < 0:
        raise ValueError('controlDict is too large')
    tokens = [token for token in TOKEN.findall(text)
              if not token.startswith(('//', '/*'))]
    result, index = {}, 0
    while index < len(tokens):
        key = tokens[index]
        index += 1
        if key == ';':
            continue
        if index >= len(tokens):
            raise ValueError('incomplete controlDict entry')
        if key in ('#include', '#includeIfPresent'):
            name = tokens[index].strip('"')
            index += 1
            name = name.replace('${FOAM_CASE}', str(root)).replace('$FOAM_CASE', str(root))
            if '$' in name:
                raise ValueError('unresolved include path')
            included = path.parent / name
            try:
                result.update(_entries(included, root, seen | {path}, budget))
            except FileNotFoundError:
                if key != '#includeIfPresent':
                    raise
            continue
        if key.startswith(('#', '$')):
            raise ValueError('unsupported top-level controlDict directive')
        if tokens[index] == '{':
            # endTime inside functions/FoamFile must not override the case limit.
            depth = 1
            index += 1
            while index < len(tokens) and depth:
                depth += (tokens[index] == '{') - (tokens[index] == '}')
                index += 1
            if depth:
                raise ValueError('unclosed controlDict dictionary')
            continue
        start = index
        while index < len(tokens) and tokens[index] != ';':
            index += 1
        if index == len(tokens):
            raise ValueError('unterminated controlDict entry')
        result[key.strip('"')] = tokens[start:index]
        index += 1
    return result


def _value(entries, key, seen=()):
    if key in seen or len(seen) >= 8:
        raise ValueError('controlDict variable cycle')
    tokens = entries[key]
    if len(tokens) != 1:
        raise ValueError('controlDict value requires evaluation')
    value = tokens[0].strip('"')
    if value.startswith('$'):
        return _value(entries, value[1:].strip('{}'), (*seen, key))
    return value


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
            return {'stop_at': stop}
        end = (float(case['end_time']) if case.get('end_time') is not None
               else float(_value(entries, 'endTime')))
        start = float(_value(entries, 'startTime')) if 'startTime' in entries else 0.0
        if not math.isfinite(end) or not math.isfinite(start) or end <= start:
            return None
        return {'start': start, 'end': end, 'stop_at': stop}
    except (KeyError, OSError, ValueError, RuntimeError):
        return None
