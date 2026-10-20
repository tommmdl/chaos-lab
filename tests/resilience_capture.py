#!/usr/bin/env python3
"""Real six-second TCP controls, separate from the application's fault matrix."""
import datetime, fcntl, json, socket, sys, threading, time, uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import lab, access, packets, resilience_packets
import resilience

REPORT={'started_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'checks':[], 'trials':[], 'passed':False,
        'purpose':'Ground-truth TCP fixtures; these are not reproduced application timeouts.'}

def check(name, value):
    REPORT['checks'].append({'name':name,'passed':bool(value)})
    print(('PASS ' if value else 'FAIL ')+name,flush=True)
    if not value:raise AssertionError(name)


def record_capture(capture, record, host, port, sample):
    index, stats=capture.index()
    record.update(samples=[{**sample,'lane':'raw'}], initial_pods=[],
                  capture_host=host,capture_port=port,
                  packet_capture={'captures':stats})
    record['packet_capture']['flows']=resilience_packets.flows(record,index,host,port)
    check('Control capture is header-only, readable and has no drops: '+record['trial'],
          bool(stats) and any(c['packets'] for c in stats) and all(
              c['exit_code']==0 and c['dropped_by_capture_kernel']==0
              and c['unparsed_fragments_or_short_headers']==0 for c in stats))
    return record['packet_capture']['flows'][0]


def headers():
    server=socket.socket(); server.bind(('127.0.0.1',0));server.listen(1);server.settimeout(8)
    port=server.getsockname()[1]
    stop=threading.Event();received=threading.Event();errors=[]
    def stall():
        try:
            with server.accept()[0] as peer:
                peer.settimeout(8)
                if peer.recv(4096):received.set()
                stop.wait(8)  # Deliberately send no HTTP response.
        except OSError as error:errors.append(str(error))
    thread=threading.Thread(target=stall,daemon=True);thread.start()
    capture=packets.Capture('matrix-control-headers',scope='host',port=port)
    record={'trial':'headers','packet_capture':{'captures':capture.records}}
    REPORT['trials'].append(record)
    try:
        capture.start()
        sample,_=resilience.connection('127.0.0.1',port)
        record['samples']=[{**sample,'lane':'raw'}]
    finally:
        stop.set();server.close();thread.join(timeout=2)
        capture.stop();record['packet_capture']['captures']=capture.records
    observed=record_capture(capture,record,'127.0.0.1',port,sample)
    record.update(fixture_received_request=received.is_set(),fixture_errors=errors)
    check('Real server receives the request but the unchanged six-second probe times out in headers',
          received.is_set() and not errors and sample.get('phase')=='headers'
          and sample.get('error_type')=='TimeoutError' and 5500<=sample['latency_ms']<=8500)
    check('Packets distinguish the header stall from a connect timeout',
          observed['classification']=='headers_timeout_after_handshake_without_response_data'
          and observed['tcp']['request_data_acked'])


def connect():
    node='devops-notes-control-plane';host=access.HOST
    probe=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);probe.connect((host,9))
    source=probe.getsockname()[0];probe.close()
    port=None
    for candidate in range(18471,18491):
        sock=socket.socket();sock.settimeout(1)
        try:sock.connect((host,candidate))
        except ConnectionRefusedError:port=candidate;break
        finally:sock.close()
    if port is None:raise RuntimeError('No confirmed unused fixture port')
    rule=['INPUT','-s',source,'-d',host,'-p','tcp','--dport',str(port),'--syn',
          '-m','comment','--comment','chaos-lab-matrix-'+uuid.uuid4().hex,'-j','DROP']
    command=['docker','exec',node,'iptables'];active=False
    capture=packets.Capture('matrix-control-connect',scope='nodes-and-host',port=port)
    record={'trial':'connect','packet_capture':{'captures':capture.records}}
    REPORT['trials'].append(record)
    try:
        capture.start()
        lab.run([*command,'-I',rule[0],'1',*rule[1:]]);active=True
        sample,_=resilience.connection(host,port)
        record['samples']=[{**sample,'lane':'raw'}]
    finally:
        try:
            if active:lab.run([*command,'-D',*rule])
        finally:
            capture.stop();record['packet_capture']['captures']=capture.records
    observed=record_capture(capture,record,host,port,sample)
    check('Dropped SYNs produce the unchanged six-second connect timeout',
          sample.get('phase')=='connect' and sample.get('error_type')=='TimeoutError'
          and 5500<=sample['latency_ms']<=8500)
    check('Capture preserves the failed connect tuple and SYNs without SYN-ACK',
          observed['classification']=='connect_timeout_syn_without_synack_in_capture')
    check('The fixture DROP rule is absent after cleanup',
          lab.run([*command,'-C',*rule],ok=False).returncode==1)


def main():
    started=time.monotonic()
    with (lab.LOCAL/'operation.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            lab.guard(lab.APP)
            headers();connect();REPORT['passed']=True
        except Exception as error:
            REPORT['error']=str(error);raise
        finally:
            REPORT['duration_seconds']=round(time.monotonic()-started,2)
            (lab.ROOT/'evidence/resilience-controls.json').write_text(json.dumps(REPORT,indent=2)+'\n')

if __name__=='__main__':main()
