#!/usr/bin/env python3
"""Own tracefs instance; retain only API-owned TCP reset events and kernel stacks."""
import json, os, re, subprocess, sys, time
from pathlib import Path

BASE = Path('/sys/kernel/tracing')


def location(name):
    if not re.fullmatch(r'chaos-lab-reset-[0-9a-f]{32}', name):
        raise ValueError('Invalid owned trace instance name')
    return BASE / 'instances' / name


def start(name):
    if not (BASE / 'events/tcp/tcp_send_reset/format').exists():
        if os.environ.get('CI') == 'true':
            subprocess.run(['mount', '-t', 'tracefs', 'tracefs', str(BASE)], check=True)
    root = location(name)
    root.mkdir()
    try:
        (root / 'tracing_on').write_text('0')
        (root / 'trace_clock').write_text('mono')
        (root / 'buffer_size_kb').write_text('2048')
        (root / 'options/stacktrace').write_text('1')
        fmt = (root / 'events/tcp/tcp_send_reset/format').read_text()
        (root / 'events/tcp/tcp_send_reset/enable').write_text('1')
        (root / 'tracing_on').write_text('1')
        return {'instance': name, 'epoch_minus_mono': time.time() - time.monotonic(),
                'kernel': os.uname().release, 'event_format': fmt,
                'clock': 'mono', 'buffer_kb_per_cpu': 2048}
    except BaseException:
        (root / 'tracing_on').write_text('0')
        root.rmdir()
        raise


def parse(text, ips, offset, port=8000):
    events = []
    pending = {}
    stack_target = None
    # An event and its stack may be interleaved with events on another CPU.
    pattern = re.compile(r'^\s*(.*?)\s+\[(\d+)\].*?\s(\d+\.\d+): (tcp_send_reset:|kernel_stack:|<stack trace>)\s*(.*)$')
    for line in text.splitlines():
        m = pattern.match(line)
        if m:
            label, cpu, stamp, kind, body = m.groups()
            stamp = float(stamp)
            stack_target = None
            if kind == 'tcp_send_reset:':
                src = re.search(r'\bsrc=([\d.]+):(\d+)', body)
                dst = re.search(r'\bdest=([\d.]+):(\d+)', body)
                # Older kernels expose separate sport/dport/saddr/daddr fields.
                if not src:
                    src = re.search(r'\bsaddr=([\d.]+)', body)
                    dst = re.search(r'\bdaddr=([\d.]+)', body)
                    sp, dp = re.search(r'\bsport=(\d+)', body), re.search(r'\bdport=(\d+)', body)
                    values = (src.group(1), int(sp.group(1)), dst.group(1), int(dp.group(1))) if all([src,dst,sp,dp]) else None
                else:
                    values = (src.group(1), int(src.group(2)), dst.group(1), int(dst.group(2))) if dst else None
                pending.pop(cpu, None)
                if not values or not ((values[0] in ips and values[1] == port) or (values[2] in ips and values[3] == port)):
                    continue
                reason = re.search(r'\breason=(\S+)', body)
                event = {'time': stamp + offset, 'monotonic_seconds': stamp, 'cpu': int(cpu),
                         'task': label, 'src': values[0], 'sport': values[1],
                         'dst': values[2], 'dport': values[3],
                         'reason': reason.group(1) if reason else None,
                         'event': line, 'stack': []}
                events.append(event)
                pending[cpu] = event
            else:
                previous = pending.get(cpu)
                if previous and previous['task'] == label and 0 <= stamp - previous['monotonic_seconds'] < 0.01:
                    stack_target = previous
        elif stack_target is not None and line.lstrip().startswith('=>'):
            stack_target['stack'].append(line.strip())
        elif line.strip() and not line.startswith('#'):
            stack_target = None
    return events


def stop(name, scope):
    root = location(name)
    try:
        (root / 'tracing_on').write_text('0')
        (root / 'events/tcp/tcp_send_reset/enable').write_text('0')
        stats = {}
        for p in sorted((root / 'per_cpu').glob('cpu*/stats')):
            stats[p.parent.name] = p.read_text()
        text = (root / 'trace').read_text()
        events = parse(text, set(scope['ips']), scope['epoch_minus_mono'], scope.get('port', 8000))
        return {'events': events, 'per_cpu_stats': stats,
                'trace_header': [s for s in text.splitlines() if s.startswith('#')],
                'scope': f"Only records with a scoped IP and TCP port {scope.get('port',8000)} are retained; no packet payload.",
                'instance': name}
    finally:
        (root / 'tracing_on').write_text('0')
        (root / 'events/tcp/tcp_send_reset/enable').write_text('0')
        root.rmdir()


if __name__ == '__main__':
    action, name = sys.argv[1:]
    if os.geteuid() != 0:
        raise SystemExit('Root is required to manage an isolated trace instance')
    if action == 'processes':
        request = json.load(sys.stdin)
        result = []
        for pid in request['pids']:
            try:
                proc = Path('/proc') / str(int(pid))
                netns = os.readlink(proc / 'ns/net')
                if netns in request['netns']:
                    status = proc.joinpath('status').read_text()
                    result.append({'host_pid': pid, 'comm': proc.joinpath('comm').read_text().strip(),
                                   'netns': netns, 'nspid': next((s for s in status.splitlines() if s.startswith('NSpid:')), None)})
            except (FileNotFoundError, ProcessLookupError):
                pass
    else:
        result = start(name) if action == 'start' else stop(name, json.load(sys.stdin))
    print(json.dumps(result))
