#!/usr/bin/env python3
"""Analyze every server-side NodePort RST, including reads recovered by retries."""
import argparse, gzip, hashlib, json, sys
from collections import Counter, defaultdict
from pathlib import Path
import lab, packets, reset_origin


def analyze(report):
    if not report.get('attribute_resets'):
        raise ValueError('Report was not collected with --attribute-resets')
    result={'collection_passed':report.get('passed'), 'trials':[], 'errors':[],
            'lane_limit':'PCAP tuples identify connections, not proxy request IDs. Internal proxy failure logs do not retain attempt source ports, so a recovered RST is not assigned to a particular retrying lane.',
            'matching_rule':'Exact RST flags/sequence/ACK plus a 20 ms window across NAT. Exact backend 4-tuple and a 20 ms window for the kernel event; ingress must match the Pod veth recorded before deletion.'}
    for t in report.get('trials',[]):
        index=defaultdict(list)
        captures=t['packet_capture']['captures']
        integrity=True
        for c in captures:
            path=lab.ROOT/c['path'];blob=path.read_bytes()
            raw=gzip.decompress(blob) if path.suffix=='.gz' else blob
            if hashlib.sha256(raw).hexdigest()!=c['sha256']:
                raise ValueError('PCAP hash mismatch: '+str(path))
            rows, stats=packets.read(path)
            integrity &= (stats['captured_http_payload_bytes']==0
                          and stats['packets']==c['packets']
                          and c['dropped_by_capture_kernel']==0 and c['exit_code']==0)
            for p in rows:index[(p['src'],p['sport'],p['dst'],p['dport'])].append(p)
        rst=[p for key, group in index.items() if key[1]==30434 for p in group if p['flags']&4]
        flows=[]
        # Retain each vantage observation; they are not independent application failures.
        for p in sorted(rst,key=lambda x:x['time']):
            origin=reset_origin.correlate({'tcp':{'packets':[p]}},index,t['rst_trace'])
            outgoing=(p['dst'],p['dport'],p['src'],p['sport'])
            incoming=(p['src'],p['sport'],p['dst'],p['dport'])
            syn=[x for x in index.get(outgoing,[]) if x['flags']&2 and not x['flags']&16
                 and p['time']-6 <= x['time'] <= p['time']]
            begin=max((x['time'] for x in syn),default=p['time']-.1)
            context=sorted([x for k in [outgoing,incoming] for x in index.get(k,[])
                            if begin <= x['time'] <= p['time']+.001],key=lambda x:x['time'])
            flows.append({'nodeport_rst':p,'nodeport_flow_context':context,'origin':origin})
        trace=t['rst_trace']
        matched={e['event'] for f in flows for e in f['origin']['kernel_events']}
        result['trials'].append({'grace_seconds':t['grace_seconds'],'trial':t['trial'],
            'capture_hashes_verified':True,'capture_integrity':bool(integrity),
            'kernel_trace_integrity':reset_origin.intact(trace),
            'kernel_event_count':len(trace['events']),
            'unmatched_kernel_events':[e for e in trace['events'] if e['event'] not in matched],
            'nodeport_rst_observations':flows})
    all_flows=[f for t in result['trials'] for f in t['nodeport_rst_observations']]
    result['nodeport_rst_observations']=len(all_flows)
    result['classifications']=dict(Counter(f['origin']['classification'] for f in all_flows))
    complete={'api_pod_tcp_close_active_reset','api_pod_tcp_no_socket_reset','api_pod_tcp_stack_other_reset'}
    result['unresolved_observations']=sum(f['origin']['classification'] not in complete for f in all_flows)
    result['all_observed_resets_attributed']=(result['unresolved_observations']==0) if all_flows else None
    result['analysis_integrity_passed']=bool(result['trials']) and all(
        t['capture_integrity'] and t['kernel_trace_integrity'] for t in result['trials'])
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('report',type=Path)
    p.add_argument('--output',type=Path,default=lab.ROOT/'evidence/reset-origins.json')
    args=p.parse_args()
    try:
        result=analyze(json.loads(args.report.read_text()))
    except Exception as error:
        result={'analysis_integrity_passed':False,'error':str(error)}
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k!='trials'},indent=2))
    if not result['analysis_integrity_passed']:sys.exit(1)
