#!/usr/bin/env python3
"""Correlate NodePort RSTs with backend interfaces, kernel stacks and owned Pods."""
import json, re, socket, sys, time, uuid
from pathlib import Path
import lab, packets

HELPER = Path(__file__).with_name('reset_trace.py')


def trace(action, name, data=None):
    return json.loads(lab.run([*packets.privileged(), 'python3', HELPER, action, name],
                             data=json.dumps(data) if data is not None else None).stdout)


def snapshot(pods):
    nodes = sorted({p['spec']['nodeName'] for p in pods})
    links = {n: json.loads(lab.run(['docker', 'exec', n, 'ip', '-j', 'link']).stdout) for n in nodes}
    result = []
    for p in pods:
        name, node, ip = p['metadata']['name'], p['spec']['nodeName'], p['status']['podIP']
        route = json.loads(lab.run(['docker', 'exec', node, 'ip', '-j', 'route', 'get', ip]).stdout)[0]
        interface = next((x for x in links[node] if x['ifname'] == route.get('dev')), {})
        # CRI inspect contains environment variables; retain only PID, never the raw output.
        cid = p['status']['containerStatuses'][0]['containerID'].split('://',1)[1]
        inspected = json.loads(lab.run(['docker', 'exec', node, 'crictl', 'inspect', cid]).stdout)
        pid = inspected['info']['pid']
        netns = lab.run(['docker', 'exec', node, 'readlink', f'/proc/{pid}/ns/net']).stdout.strip()
        host_pids = [int(x.split()[0]) for x in lab.run(['docker','top',node,'-eo','pid,comm']).stdout.splitlines()[1:]]
        processes = trace('processes', 'unused', {'pids':host_pids, 'netns':[netns]})
        result.append({'name': name, 'uid': p['metadata']['uid'], 'ip': ip, 'node': node,
                       'route': route, 'interface_index': interface.get('ifindex'),
                       'interface_name': interface.get('ifname'), 'netns': netns,
                       'processes': processes})
    return result


class Observer:
    def __init__(self, pods):
        self.name = 'chaos-lab-reset-' + uuid.uuid4().hex
        self.pods = pods
        self.metadata = None
        self.result = None

    def start(self):
        self.topology = snapshot(self.pods)
        self.metadata = trace('start', self.name)

    def stop(self):
        if self.metadata is not None:
            self.result = trace('stop', self.name, {
                'ips':[p['ip'] for p in self.topology],
                'epoch_minus_mono': self.metadata['epoch_minus_mono']})
            self.metadata['cleaned_up'] = True

    def report(self):
        return {'tool':self.metadata, 'topology':self.topology, **(self.result or {})}


def intact(record):
    if not record.get('tool',{}).get('cleaned_up'):
        return False
    stats = record.get('per_cpu_stats',{})
    required = {'overrun', 'commit overrun', 'dropped events'}
    for raw in stats.values():
        values = {m.group(1):int(m.group(2)) for m in re.finditer(
            r'^(overrun|commit overrun|dropped events):\s*(\d+)',raw,re.M)}
        if set(values) != required or any(values.values()):
            return False
    return bool(stats)


def correlate(flow, index, record):
    visible = [p for p in flow['tcp']['packets'] if p['flags'] & 4]
    if not visible:
        return {'classification':'no_client_rst_observed'}
    podmap = {p['ip']:p for p in record['topology']}
    backend = [p for key, group in index.items() if key[0] in podmap and key[1]==8000 for p in group
               if p['flags'] & 4 and any(
                   p['seq']==r['seq'] and p['ack']==r['ack'] and p['flags']==r['flags']
                   and abs(p['time']-r['time'])<0.02 for r in visible)]
    events = [e for e in record.get('events',[]) if any(
        (e['src'],e['sport'],e['dst'],e['dport'])==(p['src'],p['sport'],p['dst'],p['dport'])
        and abs(e['time']-p['time'])<0.02 for p in backend)]
    ingress = [p for p in backend if p['packet_type']==0
               and p['interface_index']==podmap[p['src']]['interface_index']
               and p['node'].endswith('-'+podmap[p['src']]['node'])]
    active_close = [e for e in events if any('tcp_send_active_reset' in s for s in e['stack'])
                    and any('tcp_close' in s for s in e['stack'])]
    no_socket = [e for e in events if e.get('reason')=='NO_SOCKET'
                 and any('tcp_v4_send_reset' in s for s in e['stack'])]
    if ingress and active_close:
        label='api_pod_tcp_close_active_reset'
    elif ingress and no_socket:
        label='api_pod_tcp_no_socket_reset'
    elif ingress and events:
        label='api_pod_tcp_stack_other_reset'
    elif ingress:
        label='api_pod_ingress_packet_without_kernel_attribution'
    else:
        label='unresolved_no_matching_pod_ingress_reset'
    owners=[]
    for e in events:
        pod=podmap.get(e['src'])
        if pod:
            pid=re.search(r'-(\d+)$', e['task'])
            owner=next((p for p in pod['processes'] if pid and p['host_pid']==int(pid.group(1))),None)
            owners.append({'pod':pod['name'],'pod_uid':pod['uid'],'netns':pod['netns'],
                           'event_task':e['task'],'matched_process':owner})
    return {'classification':label,
            'matching_rule':'NodePort/backend RST flags, seq and ack equal; timestamps within 20 ms. Kernel event must match the backend 4-tuple within 20 ms; Pod ingress requires its pre-recorded veth ifindex and packet_type=0.',
            'backend_packets':backend,'pod_ingress_packets':ingress,
            'kernel_events':events,'process_owners':owners,
            'limits':'Unmatched events remain unresolved. NAT does not retain sender identity in the client-visible address alone. A task name in softirq context is not proof that that task generated the reset.'}


def control():
    """Real positive control: close an ephemeral TCP server with unread bytes."""
    name='chaos-lab-reset-'+uuid.uuid4().hex
    server=socket.socket(); server.bind(('127.0.0.1',0)); server.listen()
    port=server.getsockname()[1]
    info=trace('start',name)
    observed=None
    try:
        with socket.create_connection(('127.0.0.1',port)) as client:
            peer,_=server.accept()
            client.sendall(b'control-unread')
            time.sleep(.05)
            peer.close()
            try:
                observed=repr(client.recv(1))
            except OSError as e:
                observed=type(e).__name__
    finally:
        server.close()
        result=trace('stop',name,{'ips':['127.0.0.1'],'port':port,
                                    'epoch_minus_mono':info['epoch_minus_mono']})
        info['cleaned_up']=True
    result['tool']=info
    result['client_result']=observed
    result['server_port']=port
    result['passed']=intact(result) and observed=='ConnectionResetError' and any(
        e['sport']==port and any('tcp_send_active_reset' in s for s in e['stack'])
        and any('tcp_close' in s for s in e['stack']) for e in result['events'])
    return result
