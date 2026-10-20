# Where the RSTs come from when a replica is removed

[Português](rst-origin.md) · [Earlier capture](connection-timeout.en.md) · [Home](../README.en.md)

The previous capture showed resets using the NodePort address. That identified the packet reaching the client but not its sender before network translation. This stage correlates the same RST on both sides of NAT with the kernel event that sent it. The application, two replicas, draining wait and GET retry limits remain the same.

## Following each reset

The test still compares direct access, access allowing three seconds for TCP connection, and access allowing 500 ms. `--attribute-resets` adds these observations before each real removal:

1. Records both API Pods' names, UIDs, IPs, nodes and network namespaces. Each Pod route identifies its node-side veth and interface index. Gunicorn PIDs are associated with that same network namespace without retaining container environment variables.
2. Captures TCP on NodePort `30434` and **only RSTs** on port `8000` involving the initial API IPs. This avoids collecting all probe traffic, which caused capture drops in the previous stage. `LINUX_SLL2` and a 60-byte snapshot retain headers without HTTP content.
3. Creates its own tracefs instance with `tcp:tcp_send_reset`, a monotonic clock and kernel stacks. Its buffer receives reset events from the shared kernel; the report retains only those involving an initial API Pod IP and port `8000`. Other services' events and the raw host buffer are not published.
4. Stops collectors, removes the tracefs instance and experiment, then validates evidence. It matches flags, sequence and ACK before/after NAT within 20 ms, requires the backend tuple in the kernel event, and checks whether the packet entered the node through the Pod's previously recorded veth (`packet_type=0`).

Reading metadata does not keep the Pod's network namespace open during removal. PCAPs run in node namespaces. Observation and scheduling still have a cost; results do not measure an availability improvement or an SLO.

The report retains unmatched events and explicitly classifies missing evidence as unresolved. A task name in a network-interrupt context alone does not identify the responsible process: an event may show kubelet context while the Pod's TCP stack answers a probe.

## A real positive control for the tracer

Before removing Pods, the test opens a TCP server on a random local port, sends bytes, and closes the accepted socket without reading them. The client receives `ConnectionResetError`; the kernel trace shows `tcp_send_active_reset`, `tcp_close` and a reset reason when the kernel exposes it. This validates collection and the mechanism, but does not replace correlation with a real API connection.

[Linux TCP source](https://kernel.googlesource.com/pub/scm/linux/kernel/git/torvalds/linux/+/ca13ab654064fee86d6e7c9e87d0af7789561509/net/ipv4/tcp.c) associates closing with unread data with an active reset and `TCP_ABORT_ON_CLOSE`. A `NO_SOCKET` reset takes a different path without an accepted connection to close. The [tracepoint definition](https://github.com/torvalds/linux/blob/v6.11/include/trace/events/tcp.h) records the tuple and, on kernels exposing it, the reason. [ftrace documentation](https://docs.kernel.org/trace/ftrace.html#instances) describes instance buffers and per-event stacks. Reports retain the actual kernel release and event format rather than assuming identical fields on every kernel.

## Local result

The local run passed **24 checks in 145.06 seconds**, covering four graceful removals and two abrupt controls. All three paths completed without request failures. Direct access had 0/2,770 failures during graceful removal and 0/1,328 during abrupt controls.

The positive control recorded `TCP_ABORT_ON_CLOSE` and the TCP-close path. Other Pod connections produced kernel reset events during graceful shutdown, but no measured direct request received a reset. Those events remain in the report without attribution to a direct failure. Therefore, **this local sample does not attribute the earlier CI resets**. No round reported PCAP drops or tracefs overruns. Original database rows, timestamps and the PostgreSQL PVC were preserved, and other labs' workloads were compared too.

[Full sample/event report](../evidence/rst-origin-local.json) · [Local capture index](../evidence/rst-origin-local-archive.json)

## CI result

[Run 39411512758](https://github.com/tommmdl/chaos-lab/actions/runs/39411512758), on kernel `6.17.0-1022-azure`, passed **115 suite checks**. The attribution comparison passed 42 assertions in 141.57 seconds. All PCAP hashes were verified: 12 files, 166,143 frames, zero capture drops, no HTTP payload and no reported tracefs overruns.

| Comparison removal | Failures / direct | Failures / TCP 3 s | Failures / TCP 500 ms | Recovered internal failures, 3 s / 500 ms |
| --- | --- | --- | --- | --- |
| 30 s, eight trials | 0 / 3,488 | 0 / 3,486 | 0 / 3,485 | 0 / 0 |
| Zero, four controls | 0 / 1,462 | 0 / 1,460 | 0 / 1,461 | 2 / 1 |

No direct request failed in this run. Resets still occurred: retrying access recorded three internal errors, and capture retained three server-side RSTs. I therefore added analysis of **every** observed NodePort RST alongside failed direct samples. It can also run after testing against compressed PCAPs.

### Origin of this run's three packets

All three came from Pod `api-5459756fdf-6fhmq`, IP `10.244.0.25`, network namespace `4026532975`. Capture received them on `veth178953dc`, interface index 26, with `packet_type=0`: entering the node from the Pod. Flags, sequence and ACK match the client-visible NodePort RSTs. The kernel event has the backend tuple and a timestamp within the correlation window.

| Client port at the NodePort | Kernel reason | Observed path |
| --- | --- | --- |
| 35592 | `TCP_STATE` | `tcp_close → __tcp_close → inet_csk_listen_stop → inet_child_forget → tcp_disconnect → tcp_send_active_reset` |
| 35654 | `NO_SOCKET` | `tcp_v4_rcv → tcp_v4_send_reset` |
| 35664 | `NO_SOCKET` | `tcp_v4_rcv → tcp_v4_send_reset` |

The first event ran in host PID **26201**, `gunicorn`, PID **16 inside the Pod**, associated with the same network namespace in the pre-fault snapshot. The flow completed its handshake and had request data acknowledged before the RST. Its stack shows listener shutdown discarding a connection still in the accept queue; [`inet_csk_listen_stop`](https://github.com/torvalds/linux/blob/v6.17/net/ipv4/inet_connection_sock.c) describes that path. Its actual reason was **`TCP_STATE`**, rather than the positive control's unread-data `TCP_ABORT_ON_CLOSE`. I did not attribute it to a Flask route response.

The next two SYNs received RST without SYN-ACK. The same Pod's TCP stack recorded `NO_SOCKET` and `tcp_v4_send_reset`, consistent with attempts arriving after listener closure. The task appears as `python3` because network processing ran during the client's connect operation; it is not a Gunicorn PID and was not treated as the API sender. [IPv4 TCP source](https://github.com/torvalds/linux/blob/v6.17/net/ipv4/tcp_ipv4.c) shows the no-socket reset path.

**These three RSTs originated in the API Pod's Linux TCP stack.** In the first case, listener shutdown in Gunicorn context reset a pending connection. In the others, the kernel answered SYNs without a valid socket. NodePort translated these packets' addresses; it was not their identified origin. This is not retroactive attribution of the seven earlier resets/refusals or every possible reset in a cluster.

Proxy logs do not retain each attempt's source port, so I did not assign individual packets to either GET mode. The aggregate three recovered errors agree with the packet count, but the individual evidence chain is **PCAP, veth, tuple and kernel event**, not a proxy request ID. None of this capture's RST observations remained unattributed.

[All-RST analysis and stacks](../evidence/rst-origin-ci-flows.json) · [Full report](../evidence/rst-origin-ci.json) · [Summary and tested commit](../evidence/rst-origin-ci-summary.json) · [SHA-256 index](../evidence/rst-origin-ci-archive.json) · [PCAPs](../evidence/rst-origin/ci-39411512758-packets)

### The separate regression retained failures too

The later resilience matrix passed 23 recovery/policy checks while retaining request failures. Those scenarios run without this stage's tracing; I did not attribute their packets using another interval's capture.

| Profile | Grace | Failures / direct | Failures / retrying GET | Recovered internal failures |
| --- | --- | --- | --- | --- |
| No wait | 0 s | 16 / 825 | 0 / 1,430 | 21 |
| No wait | 30 s | 11 / 1,020 | 0 / 1,469 | 15 |
| Wait enabled | 0 s | 5 / 1,106 | 0 / 1,180 | 5 |
| Wait enabled | 30 s | 0 / 1,537 | 0 / 1,538 | 0 |

Reports for [resilience](../evidence/rst-origin-ci-resilience.json), [alerts/recovery](../evidence/rst-origin-ci-verification.json), [HTTP policy](../evidence/rst-origin-ci-access-policy.json) and [real SYN dropping](../evidence/rst-origin-ci-syn-budget.json) are preserved too. Green CI means assertions passed, not that every connection survived injected faults.


### Final validation exposed a setup failure

[Run 39413310742](https://github.com/tommmdl/chaos-lab/actions/runs/39413310742) stopped before cluster creation: its tracepoint-file check lacked `sudo` and interpreted denied access as missing tracefs. Mount returned “already mounted” with exit code 32. I changed the check to use collector privileges and avoid mounting an existing mountpoint. Runtime-report cleanup now happens before prerequisites: that failed run's artifact contained historical checkout reports, which were not counted as tests from that execution. [Failure/correction record](../evidence/rst-origin-ci-setup-failure.json). The earlier collection and its three resets remain unchanged.

## Reproduce and inspect

The runner needs Linux, Docker, `tcpdump`, tracefs, and a kernel exposing `tcp:tcp_send_reset` and stack traces. CI checks support before creating the cluster. Locally, the cluster must be ready with no active experiments:

```bash
python3 tests/connection_window.py --rounds 8 --attribute-resets
python3 scripts/archive_packets.py
python3 scripts/analyze_resets.py evidence/connection-window.json --output evidence/reset-origins.json
```

In this WSL environment, prefix the test command with `PACKET_PRIVILEGE=wsl`. Regular Linux uses `sudo -n`. Do not run simultaneous mutation tests. Existing experiments are never replaced, and the test holds the lab operation lock.

To repeat only the analysis of published captures, run `python3 scripts/analyze_resets.py evidence/rst-origin-ci.json --output .local/rst-origin-recheck.json`; cluster access is unnecessary. The tool verifies original SHA-256 values and never treats absence of RSTs as attribution proof: it reports `all_observed_resets_attributed=null` in that case.

PCAP integrity and tracefs per-CPU loss counters are checked after retaining the trial's results. A missing tracepoint, failed positive control, loss or cleanup failure makes the test fail. The workflow attempts to upload diagnostics after a failed step; original captures are retained as artifacts for 30 days. Compressed copies and hashes are preserved in the repository.

I also repaired file references in earlier capture reports: some omitted `.gz` or used a local prefix absent from Git. Original and compressed SHA-256 values were verified; historical packets, samples and counts were unchanged. The archiver now persists compressed paths in the report and can verify an already archived capture on a repeat invocation.

## What this investigation can establish

A reset seen entering the Pod's node-side veth, with the same sequence/ACK as the client-visible RST and a matching kernel TCP-send event, attributes **that packet** to the Pod's TCP stack. A PID associated with the same Pod network namespace plus a close stack can identify process context too. Missing evidence remains unresolved; it is not evidence that kube-proxy, iptables or the client generated the reset.

The historical six-second timeout happened during graceful removal without a packet capture. Identifying the origin of current resets cannot retroactively determine that timeout's cause.
