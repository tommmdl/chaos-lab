# The six-second waits, captured inside the matrix

[Português](resilience-timeouts.md) · [Previous investigation](pod-replacement.en.md)

The previous two CI runs passed their recovery checks but still recorded six-second timeouts in the resilience matrix. Their captures covered a different test that ran before the matrix. They could not explain these requests.

This investigation captures the matrix itself across four profiles: draining off/on, combined with abrupt removal/a 30-second grace period. It retains four rounds per profile, two workers per access path and the same GET retry policy. The direct probe still uses a six-second timeout for socket operations; the test was not shortened to hide the wait.

## What is now observed

The probe in [resilience.py](../tests/resilience.py) records start/end timestamps, source port, connection duration and the failed phase: connection, send, headers or body. Request content, response reads and time limits remain the same. Socket creation is instrumented to retain the source port even when connection establishment fails; lab destinations are numeric IPv4 addresses.

CI captures NodePort `30434` traffic in the kind nodes and the runner host. It also captures TCP on port `8000` involving the initial API Pod IPs. Files use Linux SLL2 with a 60-byte snapshot and no HTTP content. Pods created after a trial starts are outside the backend filter; their absence in this capture does not prove packet loss.

Analysis in [resilience_packets.py](../scripts/resilience_packets.py) matches the tuple and time interval of each failed direct request or request taking at least 500 ms. It also retains one healthy request per trial. It checks SYN, SYN-ACK, sent data, ACKs, response data and RST. SYN sequence and time identify NAT candidates involving initial Pods. An ACK does not prove the application process accepted or read a request.

Every HTTP sample remains in the report. The retrying client connects to a local proxy: its loopback port does not identify every internal NodePort attempt. Those errors and counts remain visible but are not assigned to a particular connection without evidence.

## Real controls, separate from the application

[resilience_capture.py](../tests/resilience_capture.py) tests two known cases with the same six-second client:

- A local server receives the request and sends no response. The probe should time out in headers, after connection establishment, without response data.
- A temporary rule drops SYNs to a port verified to have no listener in the kind node. The probe should time out in connect, with SYNs and no SYN-ACK. The rule has its own identifier and is removed afterward.

These controls validate capture interpretation. They do not count as reproduced API timeouts. The matrix still uses actual Chaos Mesh faults and compares original database rows, PVC identity and other labs' resources.

## Results

The [first local matrix](../evidence/matrix-timeouts-local-resilience.json) passed all 39 checks across 16 removals in 290.38 seconds. This first capture observed the kind nodes; the runner vantage was added for CI. No six-second timeout occurred in this matrix. The [analysis](../evidence/matrix-timeouts-local-resilience-timeouts.json) classified 15 failed/slow direct-request flows as `reset_seen` and one successful 4.086-second request whose tuple had no match inside its recorded time window. A same-port flow exists outside that window; it was not shifted or assigned by guesswork. The gap remains unresolved. Rows, timestamps, PVC identity and other labs’ resources passed their preservation checks.

The [seven local control checks](../evidence/matrix-timeouts-local-resilience-controls.json) passed. Both six-second waits were observed and [correctly classified from packets](../evidence/matrix-timeouts-local-resilience-controls-analysis.json): one during connection establishment and one waiting for response headers. Those fixture timeouts are not counted as API reproductions.

## What the CI reproduced

[Run 39421860854](https://github.com/tommmdl/chaos-lab/actions/runs/39421860854), commit `2171884`, passed **146 checks**. The [summary](../evidence/matrix-timeouts-ci-39421860854-summary.json) retains every step and the final runner state. The matrix completed 16 removals in 181.96 seconds and reproduced **five direct timeouts**, all during **TCP connection establishment**, lasting 6,001–6,006 ms. Each had SYN retransmissions without an observed SYN-ACK or server RST. No HTTP request was sent on those failed connection attempts.

The [32 matrix captures](../evidence/matrix-timeouts-ci-39421860854-matrix-packet-archive.json) retained **432,610 frames with no reported drops**. [Offline analysis](../evidence/matrix-timeouts-ci-39421860854-resilience-timeouts.json) reproduced the online analysis exactly, with no unresolved direct source tuple in this CI. The matrix, control and earlier RST analyses were also reproduced from the relocated archives. Recovery, original rows/timestamps, PVC identity and other labs’ resource checks all passed.

The [individual cases](../evidence/matrix-timeouts-ci-39421860854-cases.json) and [compact timeline](../evidence/matrix-timeouts-ci-39421860854-timeline-summary.json) keep packet and Pod evidence together:

| Draining / grace / trial | Client port | Wait | Backend observed by SYN match |
|---|---:|---:|---|
| off / 30 s / 2 | 35140 | 6.004 s | `10.244.0.40`, selected Pod `crgb9` |
| off / 30 s / 2 | 35148 | 6.006 s | `10.244.0.40`, selected Pod `crgb9` |
| off / 30 s / 3 | 42264 | 6.004 s | `10.244.0.41`, selected Pod `pqs4l` |
| off / 30 s / 3 | 42300 | 6.001 s | `10.244.0.41`, selected Pod `pqs4l` |
| on / 0 s / 3 | 35158 | 6.003 s | No initial-backend match |

### Four connections kept targeting a departing Pod

For client port `35140`, the Pod watch had already reported termination roughly 417 ms before the probe started. The EndpointSlice watch removed `10.244.0.40` roughly 53 ms before that start. Nevertheless, SYN sequence/time matches identified translated traffic to that same initial Pod IP, with node-side retransmissions at approximately 0, 1.06, 2.08, 3.11, 4.13 and 5.16 seconds. There was no SYN-ACK. The probe timed out around six seconds. The other three cases followed the same pattern for the selected Pod in their trial. Watch times are client receipt timestamps, not kernel transition timestamps.

The packet pattern is consistent with a Service dataplane update window followed by persistence of a NAT mapping to the departing backend. This is an **inference**, not a measured kube-proxy/conntrack root cause. Kubernetes documents that [Service rules can lag EndpointSlice changes](https://kubernetes.io/docs/reference/networking/virtual-ips/#optimizing-iptables-mode-performance), and Netfilter describes [NAT bindings based on connection tracking](https://www.netfilter.org/projects/nftables/manpage.html). This matrix did not retain conntrack entries, rule snapshots or routing decisions for those tuples, so it cannot establish which exact rule or route caused the missing TCP reply. Backend packets appeared with both node and runner source translations; a candidate IP match does not prove delivery to the application process.

### One connection was already waiting before the fault

Client port `35158` started about **917 ms before** the watch reported Pod deletion in its current trial. The recorded experiment-apply timestamp also places the probe start **653 ms before the apply call**. NodePort SYNs were captured in both the node and runner, with no SYN-ACK, but no backend candidate matched the initial Pod-IP filter. We therefore cannot assign this timeout to the Pod removed by that trial. Its destination after NAT and the reason for missing replies remain unresolved. Capturing conntrack state, current rules and routing alongside these exact tuples is the next diagnostic gap.

The capture counts observations, not independent attempts: a SYN can appear on more than one interface and in both scopes. The first four cases each show six node-side SYN transmissions, not eighteen separate connection attempts.

### What this establishes, and what remains open

The six seconds in these five cases are the client’s **connect wait**, not six seconds spent serving `/healthz`. They differ from the earlier reproduced resets after a completed handshake. The five current timeouts are not retroactive proof that every historical uninstrumented timeout had the same cause. Short connect limits and bounded GET retries already exist on the retrying path; this investigation keeps the raw probe unchanged so the failure stays visible. It does not change kube-proxy tuning or claim that draining eliminates all failures.

The complete HTTP comparison is:

| Draining | Grace | Direct failures/requests | GET failures/requests |
|---|---:|---:|---:|
| no | 0 s | 2/1330 | 0/1327 |
| no | 30 s | 21/759 | 5/1083 |
| yes | 0 s | 2/1070 | 0/1178 |
| yes | 30 s | 0/1505 | 0/1505 |

Five retrying GETs still ended in failure without draining. Their attempts and transport errors remain in the samples. In the draining/30-second profile there was no final HTTP failure in this run, but the previous CI had a timeout in that profile. Both histories remain published. The seven CI fixture checks also passed; their two controlled timeouts are counted separately from the five API timeouts above.

## Reproduce

With the lab installed, run one suite at a time:

```bash
python3 tests/resilience_capture.py
python3 tests/resilience.py --rounds 4 --capture-packets
python3 scripts/archive_packets.py --report evidence/resilience.json --output evidence/matrix-packet-archive.json
python3 scripts/analyze_resilience.py evidence/resilience.json --output evidence/resilience-timeouts.json
```

Capture requires `tcpdump` and permission to enter the nodes' network namespaces. In this lab's WSL environment, `PACKET_PRIVILEGE=wsl` selects root access; CI uses `sudo`. Save each run under a separate name before repeating: canonical files under `evidence` are outputs from the current execution.

Offline analysis checks PCAP hashes, the header-only format, frame counts, drops and equality with the online analysis. Missing evidence or capture loss remains explicit; it does not become a conclusion about the responsible component.

References: [HTTPConnection timeout](https://docs.python.org/3/library/http.client.html) and [Pod and endpoint termination](https://kubernetes.io/docs/tutorials/services/pods-and-endpoint-termination-flow/).
