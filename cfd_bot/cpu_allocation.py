"""Allocate idle physical cores, preferring locality without limiting sockets."""
import os
from collections import defaultdict
from pathlib import Path

from .config import cpu_set
from .ui import load_ui


def format_cpus(cpus):
    ranges = []
    for cpu in sorted(cpus):
        if ranges and cpu == ranges[-1][1] + 1:
            ranges[-1][1] = cpu
        else:
            ranges.append([cpu, cpu])
    return ','.join(str(lo) if lo == hi else f'{lo}-{hi}' for lo, hi in ranges)


def topology(root=Path('/sys/devices/system/cpu')):
    """Read all online CPUs, including siblings outside the service affinity."""
    ui = load_ui()
    result = {}
    for cpu in sorted(cpu_set((root / 'online').read_text().strip())):
        folder = root / f'cpu{cpu}'
        socket = int((folder / 'topology/physical_package_id').read_text())
        core = int((folder / 'topology/core_id').read_text())
        nodes = list(folder.glob('node[0-9]*'))
        # Non-NUMA Linux systems need only the socket restriction.
        node = int(nodes[0].name[4:]) if len(nodes) == 1 else -1
        if socket < 0 or core < 0 or len(nodes) > 1:
            raise ValueError(ui.text('scenarios.runtime.cpu.topology_missing', cpu=cpu))
        result[cpu] = (socket, node, core)
    return result


def occupied_cpus(observed, active):
    ui = load_ui()
    busy = set()
    for record in observed.values():
        processes = record.get('processes', [])
        values = [p.get('cpu_list') for p in processes] if processes else [record.get('actual_cpu_list')]
        for value in values:
            if not value or value == ui.text('strings.common.unspecified'):
                raise ValueError(ui.text('scenarios.runtime.cpu.busy_unknown'))
            busy.update(cpu_set(value))
    for job in active:
        busy.update(cpu_set(job['case']['cpu_set']))
        monitor_cpu = job['case'].get('monitor_cpu')
        if monitor_cpu:
            busy.update(cpu_set(monitor_cpu))
    return busy


def select_cpus(count, layout, allowed, busy):
    """Prefer one node; otherwise spread across enough nodes, without overlap."""
    ui = load_ui()
    groups = defaultdict(dict)
    occupied_cores = {(layout[c][0], layout[c][2]) for c in busy if c in layout}
    for cpu in sorted(set(allowed) & layout.keys()):
        socket, node, core = layout[cpu]
        groups[socket, node].setdefault(core, cpu)
    capacity = sum(len(cores) for cores in groups.values())
    if count < 1 or count > capacity:
        raise ValueError(ui.text('scenarios.runtime.cpu.capacity', count=count, capacity=capacity))
    candidates, free_groups, availability = [], [], []
    for (socket, node), cores in sorted(groups.items()):
        free = sorted(cpu for core, cpu in cores.items() if (socket, core) not in occupied_cores)
        if free:
            free_groups.append((socket, node, free))
        availability.append(ui.text('scenarios.runtime.cpu.availability', socket=socket,
                                    node=node, count=len(free)))
        if len(free) >= count:
            candidates.append((len(free), socket, node, free))
    if sum(len(free) for _, _, free in free_groups) < count:
        raise ValueError(ui.text('scenarios.runtime.cpu.waiting', count=count,
                                 availability=', '.join(availability)))
    if candidates:
        _, _, _, free = min(candidates)
        selected = free[:count]
    else:
        # Use as few NUMA nodes as possible, then balance ranks across them.
        selected_groups, room = [], 0
        for socket, node, free in sorted(free_groups, key=lambda g: (-len(g[2]), g[0], g[1])):
            selected_groups.append(free)
            room += len(free)
            if room >= count:
                break
        selected = []
        for index in range(max(map(len, selected_groups))):
            for free in selected_groups:
                if index < len(free):
                    selected.append(free[index])
                    if len(selected) == count:
                        break
            if len(selected) == count:
                break
    return dict(cpu_set=format_cpus(selected),
                sockets=sorted({layout[c][0] for c in selected}),
                numa_nodes=sorted({layout[c][1] for c in selected}))


def managed_cpus(config, affinity=None):
    """Return the bot-managed CPU pool, leaving higher-numbered CPUs reserved."""
    available = sorted(os.sched_getaffinity(0) if affinity is None else affinity)
    configured = config.get('scheduler', {}).get('cpu_capacity')
    limit = len(available) if configured is None else min(configured, len(available))
    return set(available[:limit])


def capacity_status(case, observed, active, config):
    """Describe whether one resolved case can start inside the managed pool."""
    pool = managed_cpus(config)
    try:
        busy = occupied_cpus(observed, active)
    except ValueError as exc:
        required = case['cores'] + int(bool(case.get('monitoring', {}).get('allocate_cpu')))
        return dict(capacity=len(pool), used_cores=None, free_cores=None,
                    required_cores=required, can_run=False, capacity_reason=str(exc))
    used = len(pool & busy)
    monitor_requested = bool(case.get('monitoring', {}).get('allocate_cpu'))
    automatic = case.get('cpu_policy') == 'auto'
    requested = set() if automatic else cpu_set(case['cpu_set'])
    required = case['cores'] + int(monitor_requested) if automatic else len(requested) + int(monitor_requested)
    can_run = True
    reason = ''
    try:
        if automatic:
            select_cpus(required, topology(), pool, busy)
        else:
            unavailable = requested - pool
            overlap = requested & busy
            if unavailable:
                can_run = False
                reason = f"CPU {format_cpus(unavailable)} is outside the managed pool"
            elif overlap:
                can_run = False
                reason = f"CPU {format_cpus(overlap)} is already occupied"
            elif monitor_requested:
                select_cpus(1, topology(), pool, busy | requested)
    except ValueError as exc:
        can_run, reason = False, str(exc)
    return dict(capacity=len(pool), used_cores=used, free_cores=max(0, len(pool) - used),
                required_cores=required, can_run=can_run, capacity_reason=reason)


def allocate_cpus(count, observed, active, *, allowed=None):
    pool = os.sched_getaffinity(0) if allowed is None else allowed
    return select_cpus(count, topology(), pool, occupied_cpus(observed, active))
