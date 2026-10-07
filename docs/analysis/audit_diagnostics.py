"""Issue #30: isolated logging attribution and lossless offline codec experiment.

Run from repository root: python3 docs/analysis/audit_diagnostics.py --output /tmp/cfd-log-audit
No Telegram/solver execution. Ablations are temporary mocks, never product changes.
"""
import argparse
from collections import Counter, defaultdict
from contextlib import ExitStack
import cProfile
import gzip
import hashlib
import json
import os
from pathlib import Path
import pstats
import random
import statistics
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ['CFD_BOT_DIAGNOSTICS'] = '0'
from cfd_bot import diagnostics as d
from cfd_bot.config import load_bot
from cfd_bot.storage import Store
from cfd_bot.web import WebApp
from cfd_bot.logs import recent_log


def packed(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()


def codecs(raw):
    keys, strings = {}, {}
    frequencies = Counter()
    def count(v):
        if isinstance(v, dict):
            for key, item in v.items():
                keys.setdefault(key, len(keys)); count(item)
        elif isinstance(v, list):
            for item in v: count(item)
        elif isinstance(v, str): frequencies[v] += 1
    count(raw)
    for value, occurrences in frequencies.items():
        if len(value) >= 12 and occurrences >= 3:
            strings[value] = len(strings)
    def code(v):
        if isinstance(v, dict): return [0, *[x for k, item in v.items() for x in (keys[k], code(item))]]
        if isinstance(v, list): return [1, *map(code, v)]
        if isinstance(v, str) and v in strings: return [2, strings[v]]
        return v
    key_list, string_list = list(keys), list(strings)
    def decode(v):
        if not isinstance(v, list): return v
        if v[0] == 0: return {key_list[v[i]]: decode(v[i+1]) for i in range(1, len(v), 2)}
        if v[0] == 1: return [decode(x) for x in v[1:]]
        return string_list[v[1]]
    result = {}
    for mode in ('current', 'numeric_dictionary', 'sha256_payload_dictionary', 'integer_payload_dictionary'):
        start = time.process_time()
        dictionary = {}
        if mode == 'current': encoded = raw
        elif mode == 'numeric_dictionary':
            encoded = code(raw); dictionary = {'keys': key_list, 'strings': string_list}
            assert decode(encoded) == raw
        else:
            values, index = {}, {}
            def replace(v):
                if isinstance(v, dict):
                    out = {}
                    for key, value in v.items():
                        if key in ('input', 'result', 'caller') and isinstance(value, (list, dict)) and len(packed(value)) >= 120:
                            canonical = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
                            digest = hashlib.sha256(canonical.encode()).hexdigest() if mode.startswith('sha256') else str(index.setdefault(canonical, len(index)))
                            if digest in values: assert values[digest] == value
                            values[digest] = value
                            out[key] = {'$audit_payload': digest}
                        else: out[key] = replace(value)
                    return out
                if isinstance(v, list): return [replace(x) for x in v]
                return v
            def restore(v):
                if isinstance(v, dict):
                    if set(v) == {'$audit_payload'}: return values[v['$audit_payload']]
                    return {k: restore(x) for k, x in v.items()}
                if isinstance(v, list): return [restore(x) for x in v]
                return v
            encoded = replace(raw); dictionary = values
            assert restore(encoded) == raw
        encode_verify_ms = (time.process_time() - start) * 1000
        start = time.process_time(); data = packed(encoded); sidecar = packed(dictionary)
        serialize_ms = (time.process_time() - start) * 1000
        start = time.process_time(); zipped = gzip.compress(data + b'\n' + sidecar, compresslevel=1, mtime=0)
        result[mode] = dict(data_bytes=len(data), dictionary_bytes=len(sidecar), total_bytes=len(data)+len(sidecar),
                            gzip1_bytes=len(zipped), serialize_cpu_ms=serialize_ms,
                            transform_and_roundtrip_cpu_ms=encode_verify_ms,
                            gzip_cpu_ms=(time.process_time()-start)*1000, roundtrip=True)
    return result


def analyze(directory):
    raw = [json.loads(line) for path in directory.glob('*.jsonl') for line in path.read_text().splitlines()]
    records = [r for path in directory.glob('*.jsonl') for r in d.read_records(path)]
    events, calls, steps, payloads = Counter(), Counter(), Counter(), Counter()
    field_bytes = Counter()
    for r in records:
        events[r['event']] += 1
        if r['event'] == 'function.call': calls[r.get('function')] += 1
        for step in r.get('steps', []): steps[str(step[0])] += 1
        for k in ('input', 'result', 'caller', 'function'):
            if k in r:
                value = packed(r[k]); field_bytes[k] += len(value)
                if k in ('input', 'result', 'caller'): payloads[(k, value)] += 1
        if r.get('exception'):
            field_bytes['exception'] += len(packed(r['exception']))
    repeated = sum((count-1)*len(value) for (key, value), count in payloads.items())
    return dict(physical_lines=len(raw), file_bytes=sum(p.stat().st_size for p in directory.glob('*.jsonl')),
                events=events, top_calls=calls.most_common(16), steps=sum(steps.values()), top_steps=steps.most_common(12),
                expanded_field_bytes=field_bytes, repeated_payload_occurrences=sum(n-1 for n in payloads.values()),
                repeated_payload_bytes_upper_bound=repeated,
                top_repeated_payloads=[dict(field=k,count=n,bytes_each=len(v)) for (k,v),n in payloads.most_common(8)],
                codecs=codecs(raw))


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--valid-cpu', action='store_true')
    parser.add_argument('--modes', nargs='+', default=['off', 'full', 'encode_no_disk', 'no_write', 'no_summary'])
    parser.add_argument('--scenario', choices=['web_overview', 'log_tail_2000_lines'])
    args = parser.parse_args(); args.output.mkdir(parents=True, exist_ok=True)
    results = {'revision': '0306c0d', 'samples_per_mode': 3, 'members': 921, 'active': 40,
               'valid_cpu_settings': args.valid_cpu, 'ablation': {}}
    with tempfile.TemporaryDirectory(prefix='cfd-log-audit-') as directory, ExitStack() as outer:
        root = Path(directory); (root/'tickets').mkdir(); observed = {}
        for i in range(921):
            case = root/f'case-{i}'; (case/'system').mkdir(parents=True)
            (case/'system/controlDict').write_text('startTime 0; stopAt endTime; endTime 10;\n')
            if args.valid_cpu:
                (case/'.process-core').write_text('NP=1\nCPU_SET="0"\nexport NP CPU_SET\n')
                (case/'Allrun').write_text('#!/bin/sh\nexit 0\n')
            (case/'log.solver').write_text('Time = 1\nExecutionTime = 1 s  ClockTime = 1 s\nTime = 2\nExecutionTime = 2 s  ClockTime = 2 s\n')
            (root/'tickets'/f'alone-{i}.json').write_text(json.dumps(dict(version=1,case_dir=str(case),name=f'fixture-{i}',watcher={'logs':['log.solver']})))
            if i < 40: observed[str(case)] = dict(root=str(case),processes=[],supervisors=[],actual_cores=1,actual_cpu_list='0',owner='fixture')
        (root/'bot.json').write_text(json.dumps(dict(version=1,state_dir='state',case_globs=['tickets/*.json'],scheduler={'enabled':False})))
        config = load_bot(root/'bot.json'); app = WebApp(config, Store(config['state_dir']))
        outer.enter_context(patch('cfd_bot.ticket_run.snapshot', return_value={'raw':'synthetic','cases':observed}))
        outer.enter_context(patch('subprocess.Popen', side_effect=AssertionError('no worker')))
        outer.enter_context(patch('urllib.request.urlopen', side_effect=AssertionError('no network')))
        huge = root/'long.log'; huge.write_text('Time = 1\nExecutionTime = 1 s  ClockTime = 1 s\n'*1000)
        operations = {'web_overview': app.overview, 'log_tail_2000_lines': lambda:recent_log(huge)}
        def configure(name, active=True):
            d.configure({'diagnostic_logging': {'directory': str(args.output/name), 'max_bytes': 1024**3}}, active=active, component='audit')
        for scenario, operation in operations.items():
            if args.scenario and args.scenario != scenario: continue
            configure('warmup', False); operation()
            timing = defaultdict(list)
            for sample in range(3):
                modes = args.modes.copy()
                random.Random(30+sample).shuffle(modes)
                for mode in modes:
                    configure('scratch', mode != 'off')
                    with ExitStack() as patches:
                        if mode == 'encode_no_disk':
                            d.flush(); stream = open(os.devnull, 'w'); patches.callback(stream.close)
                            patches.enter_context(patch.object(d._sink, 'stream', stream))
                        if mode == 'no_write': patches.enter_context(patch.object(d, '_write', lambda *a, **k:None))
                        if mode == 'no_summary':
                            patches.enter_context(patch.object(d, 'summary', lambda *a, **k:None))
                            patches.enter_context(patch.object(d, 'call_value', lambda *a, **k:None))
                        start = time.perf_counter(); cpu = time.process_time(); operation(); d.flush()
                        timing[mode].append(dict(wall_ms=(time.perf_counter()-start)*1000, cpu_ms=(time.process_time()-cpu)*1000))
                    d.close()
            results['ablation'][scenario] = {mode:dict(samples=v, median_ms=statistics.median(x['wall_ms'] for x in v)) for mode,v in timing.items()}
            configure(scenario); operation(); d.close()
            results[scenario] = analyze(args.output/scenario)
            configure('profile'); profiler = cProfile.Profile(); profiler.runcall(operation); d.close()
            stats = pstats.Stats(profiler)
            rows = [dict(file=Path(key[0]).name, line=key[1], function=key[2], primitive_calls=v[0], calls=v[1], self_seconds=v[2], cumulative_seconds=v[3]) for key,v in stats.stats.items()]
            results[scenario]['profile_top_self'] = sorted(rows,key=lambda x:x['self_seconds'],reverse=True)[:20]
            results[scenario]['profile_total_seconds'] = stats.total_tt
        d.configure(active=False)
    (args.output/'result.json').write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
    print(args.output/'result.json')


if __name__ == '__main__': main()
