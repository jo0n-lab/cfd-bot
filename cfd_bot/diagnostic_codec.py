"""Schema 2 numeric diagnostic batches. Only already-sanitized values enter here."""

EVENTS = {1: 'function.call', 2: 'function.return', 3: 'function.raise',
          4: 'function.cancelled', 5: 'sequence.step', 6: 'sequence.steps',
          100: 'service.start', 101: 'log.closed', 110: 'function.definition',
          201: 'ui.telegram.received', 202: 'ui.telegram.routed',
          211: 'http.request', 212: 'http.response',
          301: 'db.statement.start', 302: 'db.statement.end', 303: 'db.statement.error',
          311: 'db.transaction.acquired', 312: 'db.transaction.commit',
          313: 'db.transaction.rollback', 314: 'db.transaction.error', 315: 'db.context.exit',
          401: 'subprocess.request', 402: 'subprocess.response', 403: 'subprocess.error',
          404: 'subprocess.spawn', 405: 'subprocess.started', 406: 'process.signal',
          501: 'python.warning', 502: 'python.logging', 503: 'python.uncaught',
          504: 'python.thread_exception',
          601: 'shell.start', 602: 'shell.call', 603: 'shell.return',
          604: 'shell.error', 605: 'shell.exit', 606: 'log.rotation',
          701: 'ui.click', 702: 'ui.change', 703: 'ui.input', 704: 'ui.navigation',
          710: 'browser.start', 711: 'browser.error', 712: 'browser.unhandledrejection',
          713: 'browser.console.warn', 714: 'browser.console.error'}
EVENT_CODES = {name: number for number, name in EVENTS.items()}
_SCALARS = (str, int, bool, float, type(None))


class Values:
    """Bounded to one batch; share immutable summaries without JSON/hash work."""
    def __init__(self):
        self.values = []
        self.identities = {}
        self.scalars = {}

    def ref(self, value):
        kind = type(value)
        if kind in _SCALARS:
            key = (kind, value)
        else:
            prior = self.identities.get(id(value))
            if prior is not None:
                return prior[1]
            key = None
            if kind is dict and len(value) <= 8 and all(type(v) in _SCALARS for v in value.values()):
                key = (dict, tuple((k, type(v), v) for k, v in value.items()))
        if key is not None:
            prior = self.scalars.get(key)
            if prior is not None:
                return prior
        index = len(self.values)
        self.values.append(value)
        if key is not None:
            self.scalars[key] = index
        if kind not in _SCALARS:
            self.identities[id(value)] = (value, index)
        return index


def encode(records, function_codes, event_codes=EVENT_CODES):
    origin_time, origin_seq = records[0][1:3]
    values, contexts, context_ids, calls, call_ids = Values(), [], {}, [], {}
    errors, error_versions = [], {}

    def call_ref(value):
        if value is None:
            return None
        result = call_ids.get(value)
        if result is None:
            result = len(calls); call_ids[value] = result; calls.append(value)
        return result

    def exception_ref(chain):
        result = []
        for item in chain:
            frames = tuple(values.ref(frame) for frame in item['frames'])
            meta = {k: v for k, v in item.items() if k != 'frames'}
            identity = item.get('error_id')
            prior = error_versions.get(identity) if identity else None
            if prior and prior[1] == frames and prior[2] == meta:
                result.append(prior[0]); continue
            base = None
            prefix = frames
            if prior and prior[1] and len(frames) >= len(prior[1]) and frames[-len(prior[1]):] == prior[1]:
                base = prior[0]; prefix = frames[:-len(prior[1])]
            index = len(errors)
            errors.append([values.ref(meta), base, list(prefix)])
            if identity: error_versions[identity] = (index, frames, meta)
            result.append(index)
        return result

    def steps_ref(steps):
        return [[s[0], s[1]-origin_seq, s[2]-origin_time] + ([values.ref(s[3])] if len(s)>3 else []) for s in steps]

    rows = []
    for event, stamp, sequence, thread, call, parent, trace, actor, fields in records:
        key = (thread, trace, id(actor))
        context = context_ids.get(key)
        if context is None:
            context = len(contexts); context_ids[key] = context
            contexts.append([thread, trace, actor])
        code = event_codes.get(event, event)
        row = [code, stamp-origin_time, sequence-origin_seq, context, call_ref(call), call_ref(parent)]
        if code == 1:
            row += [function_codes.get(fields['function'], fields['function']),
                    values.ref(fields['caller']), values.ref(fields['input']), fields.get('parent_trace_id')]
        elif code in (2, 3, 4):
            row += [function_codes.get(fields['function'], fields['function']), fields['duration_ns'],
                    exception_ref(fields['exception']) if code == 3 else values.ref(fields.get('result')),
                    steps_ref(fields.get('steps', []))]
        else:
            # Rare/domain events retain their explicit field names in the value dictionary.
            data = {k: v for k, v in fields.items() if k not in ('steps', 'exception')}
            row += [values.ref(data), steps_ref(fields.get('steps', [])),
                    exception_ref(fields['exception']) if fields.get('exception') else None]
        rows.append(row)
    return {'event': 'log.batch.v2', 'base': [str(origin_time), origin_seq], 'contexts': contexts,
            'calls': calls, 'values': values.values, 'errors': errors, 'records': rows}


def decode(batch, header):
    origin_time, origin_seq = int(batch['base'][0]), batch['base'][1]
    values, calls = batch['values'], batch['calls']
    functions = header.get('function_codes', [])
    events = header.get('event_codes', {})
    errors = []
    for meta, base, frames in batch['errors']:
        item = dict(values[meta])
        item['frames'] = [values[f] for f in frames] + (errors[base]['frames'] if base is not None else [])
        errors.append(item)

    def function_name(code):
        return functions[code] if isinstance(code, int) else code

    def steps(items):
        return [[s[0], s[1]+origin_seq, s[2]+origin_time] + ([values[s[3]]] if len(s)>3 else []) for s in items]

    common = {k: header[k] for k in ('instance', 'pid', 'component') if k in header}
    for row in batch['records']:
        code, stamp, seq, context, call, parent = row[:6]
        thread, trace, actor = batch['contexts'][context]
        result = dict(common, event=events.get(str(code), EVENTS.get(code, code)),
                      ts_ns=origin_time+stamp, seq=origin_seq+seq, thread=thread, trace_id=trace,
                      initiator=actor, call_id=calls[call] if call is not None else None,
                      parent_call_id=calls[parent] if parent is not None else None)
        if code == 1:
            result.update(function=function_name(row[6]), caller=values[row[7]], input=values[row[8]], parent_trace_id=row[9])
        elif code in (2, 3, 4):
            result.update(function=function_name(row[6]), duration_ns=row[7])
            if code == 3: result['exception'] = [errors[e] for e in row[8]]
            else: result['result'] = values[row[8]]
            if row[9]: result['steps'] = steps(row[9])
        else:
            result.update(values[row[6]])
            if row[7]: result['steps'] = steps(row[7])
            if row[8] is not None: result['exception'] = [errors[e] for e in row[8]]
        yield result
