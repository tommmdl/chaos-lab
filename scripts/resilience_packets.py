"""Packet evidence for the original six-second resilience probe; no inferred payload."""
import gzip, hashlib, json
from collections import Counter, defaultdict
from pathlib import Path
import lab, packets


def flow(sample, index, host, port, pods):
    source = sample.get('source')
    begin, end = sample['started_at'] - .02, sample['time'] + .25
    candidates = []
    if source:
        candidates = [key for key in index if key[1] == source[1] and key[2:] == (host, port)
                      and (source[0] == '0.0.0.0' or key[0] == source[0])
                      and any(begin <= p['time'] <= end for p in index[key])]
    if len(candidates) != 1:
        return {'sample': sample, 'classification': 'unresolved_source_tuple',
                'candidate_tuples': candidates}
    key = candidates[0]
    resolved = {**sample, 'source': key[:2]}
    tcp = packets.correlate(resolved, host, port, index)
    rows = tcp['packets']
    sent = [p for p in rows if p['dport'] == port]
    incoming = [p for p in rows if p['sport'] == port]
    syn = [p for p in sent if p['flags'] & 2 and not p['flags'] & 16]
    synack = [p for p in incoming if p['flags'] & 18 == 18]
    data = [p for p in sent if p['tcp_payload_length']]
    response = [p for p in incoming if p['tcp_payload_length']]
    rst = [p for p in incoming if p['flags'] & 4 and p['time'] <= sample['time']]
    label = tcp['classification']
    if sample.get('error_type') == 'TimeoutError':
        if sample.get('phase') == 'connect' and syn and not synack:
            label = 'connect_timeout_syn_without_synack_in_capture'
        elif sample.get('phase') == 'headers' and synack and data and not response:
            label = 'headers_timeout_after_handshake_without_response_data'
        elif sample.get('phase') == 'headers' and response:
            label = 'headers_timeout_despite_response_data_in_capture'
        else:
            label = 'timeout_incomplete_capture_or_other_phase'
    ips = {p['ip']: p for p in pods}
    # Match NAT candidates by SYN sequence and time, not by source port alone.
    backend_keys = {k for k, group in index.items() if k[2] in ips and k[3] == 8000
                    and any(p['flags'] & 2 and not p['flags'] & 16
                            and any(p['seq'] == s['seq'] and abs(p['time']-s['time']) < .02 for s in syn)
                            for p in group)}
    backends = []
    for backend in sorted(backend_keys):
        reverse = (backend[2], backend[3], backend[0], backend[1])
        context = sorted([p for k in [backend, reverse] for p in index.get(k, [])
                          if begin <= p['time'] <= end], key=lambda p:p['time'])
        backends.append({'pod': ips[backend[2]], 'tuple': backend, 'packets': context})
    return {'sample': sample, 'classification': label, 'tcp': tcp,
            'server_rst_before_probe_return': rst,
            'backend_candidates': backends,
            'limits': 'SYN sequence/time matches are NAT candidates. Only initial Pod IPs are captured on port 8000. Missing packets do not prove a component dropped them. ACKs do not prove application acceptance. Header-only capture cannot inspect HTTP content.'}


def flows(trial, index, host, port=30434):
    chosen = [s for s in trial['samples'] if s['lane']=='raw'
              and (s['status'] != 200 or s['latency_ms'] >= 500)]
    healthy = next((s for s in trial['samples'] if s['lane']=='raw' and s['status']==200), None)
    if healthy:
        chosen.append(healthy)
    return [flow(s, index, host, port, trial.get('initial_pods', [])) for s in chosen]


def analyze(report):
    result = {'collection_passed': report.get('passed'), 'trials': [],
              'limits': 'Only raw/direct requests have NodePort source tuples. Proxy-internal retry attempts cannot be assigned from the loopback client tuple. Every HTTP sample remains in the source report.'}
    for trial in report.get('trials', []):
        index = defaultdict(list)
        integrity = True
        captures = trial.get('packet_capture', {}).get('captures', [])
        for c in captures:
            path = lab.ROOT/c['path']
            blob = path.read_bytes()
            raw = gzip.decompress(blob) if path.suffix=='.gz' else blob
            if hashlib.sha256(raw).hexdigest() != c['sha256']:
                raise ValueError('PCAP hash mismatch: '+str(path))
            rows, stats = packets.read(path)
            integrity &= (c['exit_code']==0 and c['dropped_by_capture_kernel']==0
                          and stats['packets']==c['packets']
                          and stats['unparsed_fragments_or_short_headers']==0
                          and stats['captured_http_payload_bytes']==0)
            for p in rows:index[(p['src'],p['sport'],p['dst'],p['dport'])].append(p)
        observed = flows(trial, index, trial.get('capture_host', report.get('capture_host')), trial.get('capture_port', report.get('capture_port',30434)))
        recorded = trial.get('packet_capture', {}).get('flows')
        # JSON changes tuples to lists; compare normalized values when present.
        same = recorded is None or json.loads(json.dumps(observed)) == recorded
        result['trials'].append({'drain': trial.get('drain'),
            'grace_seconds':trial.get('chaos_grace_seconds'), 'trial':trial.get('trial'),
            'capture_integrity':bool(captures) and bool(integrity),
            'matches_online_analysis':same, 'flows':observed})
    all_flows = [f for t in result['trials'] for f in t['flows']]
    timeouts = [f for f in all_flows if f['sample'].get('error_type')=='TimeoutError']
    result.update(direct_timeouts=len(timeouts),
                  timeout_classifications=dict(Counter(f['classification'] for f in timeouts)),
                  flow_classifications=dict(Counter(f['classification'] for f in all_flows)),
                  unresolved_source_tuples=sum(f['classification']=='unresolved_source_tuple' for f in all_flows),
                  analysis_integrity_passed=bool(result['trials']) and all(
                      t['capture_integrity'] and t['matches_online_analysis'] for t in result['trials']))
    return result
