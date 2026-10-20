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

The [first local matrix](../evidence/matrix-timeouts-local-resilience.json) passed all 39 checks across 16 removals in 290.38 seconds. This first capture observed the kind nodes; the runner vantage was added for CI. No six-second timeout occurred in this matrix. The [analysis](../evidence/matrix-timeouts-local-resilience-timeouts.json) retained 15 reset observations among failed/slow direct requests and one successful 4.086-second request whose tuple had no match inside its recorded time window. A same-port flow exists outside that window; it was not shifted or assigned by guesswork. The gap remains unresolved. Rows, timestamps, PVC identity and other labs’ resources passed their preservation checks.

The [seven local control checks](../evidence/matrix-timeouts-local-resilience-controls.json) passed. Both six-second waits were observed and [correctly classified from packets](../evidence/matrix-timeouts-local-resilience-controls-analysis.json): one during connection establishment and one waiting for response headers. Those fixture timeouts are not counted as API reproductions.

CI results will be added after execution, including failed attempts and cases where no timeout is observed.

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
