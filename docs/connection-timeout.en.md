# Investigating the timeout that remained in CI

[Português](connection-timeout.md) · [Previous investigation](connections.en.md) · [Home](../README.en.md)

The draining wait helped in the first comparison, but one direct request still failed on GitHub. I inspected that connection's record before changing Pod timings. This step keeps the API, Service, replicas and shutdown settings the same. The adopted change is in local read access: separating the time allowed to establish a connection from the time allowed to receive its response.

## What the old record tells us

In the fourth trial with draining and a 30-second grace period, the request ended with `TimeoutError` after 6,002.085 ms. Its estimated start was 704 ms after experiment submission, already after the watch received an endpoint with `ready=false`. The SIGTERM log arrived about 4.59 seconds after the request began. The connection started during the five-second wait.

This weakens the explanation that the API simply needed more time before SIGTERM. It does not identify the backend: the old report retained neither the socket phase nor the source port. It cannot distinguish an unanswered SYN from waiting for HTTP headers. Watch times are receipt times, and request start was calculated from duration. The [extracted calculation](../evidence/connection-window-original-timeout.json) points to the unchanged original report.

I did not attribute the error to DNS, PostgreSQL, conntrack exhaustion or a specific kernel bug. The client uses a numeric IP and `/healthz`; evidence does not close the remaining hypotheses. [Kubernetes networking documentation](https://kubernetes.io/docs/reference/networking/virtual-ips/) describes proxy behavior and known issues, but a known issue's existence does not identify this timeout's cause.

## What changed and why

GET now allows **500 ms to establish TCP**, retaining up to three attempts, 50/100 ms delays, up to three seconds for reading and a configured four-second budget. After connecting, the read timeout is also capped by remaining time. POST and DELETE keep a single attempt and do not use the short connection timeout.

Each internal failure records `phase` (`connect`, `send`, `headers` or `body`) and `elapsed_ms`, alongside its error type. Attempt headers and logs remain visible. HTTP 503 and other HTTP responses do not trigger another attempt. Prometheus probes continue to bypass retrying access.

I did not increase `preStop`, remove the failed sample or change the direct comparator client's six-second timeout. If access recovers a read, the lost connection remains in its report. This change leaves more room to try a new connection; it does not itself fix packet loss in the cluster network.

## A TCP test independent of removal luck

`tests/syn_budget.py` first checks an unused node port that refuses connections. It then installs a temporary SYN-drop rule restricted to that port, node IP and source IP. The rule does not target the API or database and is removed in `finally`. These are real sockets and packets, without synthesizing a Python exception.

Locally, access allowing three seconds to connect exhausted about **3,951 ms within two attempts**. With 500 ms, three attempts took about **1,502 ms of connection waiting**. These totals sum attempts and exclude 50/100 ms delays. Both returned 502 because the destination remained unreachable. This demonstrates phase classification, bounded waiting and room for an additional attempt; it does not demonstrate a recovered read or reproduce the historical cause. [Complete report](../evidence/syn-budget.json).

## Compare under real removals

`tests/connection_window.py --rounds 8` performs eight graceful removals interleaved with four abrupt controls. Each trial runs three concurrent paths: direct NodePort, instrumented access allowing three seconds to connect, and instrumented access allowing 500 ms. Each path has two clients with a minimum 50 ms interval. Both access paths share measurement code and read timeouts; the three-second path represents the earlier connection budget, not a byte-for-byte replay of the old implementation.

The direct client records start, phase, latency and source port when available. Watches follow Pods, EndpointSlices and Gunicorn signals. A node conntrack observer collects only connection metadata for NodePort 30434; the report retains records matching failed direct requests' source ports. It does not capture payload or credentials. A `SYN_SENT` record alone does not prove a connection never progressed: collection can lose events. Read it alongside socket phase evidence.

The three paths do not send the same request to the same backend. Timeouts and retries change sample counts, and replica selection is random. Counts describe these runs rather than an SLO measurement.

## What happened in the new local comparison

The 12 removals took 237.05 s and passed 17 recovery/preservation checks. Eight graceful removals and four abrupt controls produced no connection errors in any path in this run.

| Removal | Direct | TCP 3 s access | TCP 500 ms access |
| --- | --- | --- | --- |
| 30 s grace, eight trials | 0 / 5,413 | 0 / 5,369 | 0 / 5,358 |
| Zero grace, four trials | 0 / 2,505 | 0 / 2,497 | 0 / 2,492 |

Each cell shows failures/requests. There were no internally recovered transport failures either. Since both gateways had zero errors, this sample **does not demonstrate a failure-rate reduction from the candidate**. Error-free abrupt controls do not make zero grace safe. Previous runs with failures remain valid observations from those executions.

The [summary](../evidence/connection-window-summary.json) and [complete sample record](../evidence/connection-window.json) remain separate from the earlier comparison. No failed direct connection was available to correlate with conntrack in this run. Collection ended with observers stopped; original rows, timestamps, PVC and other labs' workloads were checked.

## Regression also retained failures

I repeated the earlier 16-removal matrix using the new gateway, including profiles without the draining wait. Its 23 recovery, preservation and write-policy assertions passed in 283.24 s; this does not mean all requests succeeded.

| Local profile | Grace | Failures / direct | Failures / short-TCP GET | Internal transport failures |
| --- | --- | --- | --- | --- |
| No wait | 0 s | 43 / 1,557 | 4 / 1,966 | 42 |
| No wait | 30 s | 24 / 1,658 | 2 / 2,028 | 23 |
| Wait enabled | 0 s | 4 / 2,142 | 0 / 2,113 | 6 |
| Wait enabled | 30 s | 0 / 2,152 | 0 / 2,161 | 1 |

Even with the new gateway, six reads failed in profiles without waiting. With waiting and a grace period, one internal transport failure was recovered. Direct and retrying requests are not paired. [This regression report](../evidence/connection-timeout-resilience.json) retains every sample and the real POST/lost-response test, confirming one attempt and one commit. The [earlier report](../evidence/resilience.json) was not replaced.

## CI exposed different failures from the old timeout

[Workflow 39403569026](https://github.com/tommmdl/chaos-lab/actions/runs/39403569026) passed **90 checks** in 9 min 32 s. [Run metadata](../evidence/connection-timeout-ci-run.json) identifies the tested commit. The new comparison took 121.13 s:

| CI removal | Direct | TCP 3 s access | TCP 500 ms access |
| --- | --- | --- | --- |
| 30 s grace, eight trials | 0 / 3,346 | 0 / 3,346 | 0 / 3,343 |
| Zero grace, four trials | 6 / 1,273 | 0 / 1,270 | 0 / 1,269 |

The six-second timeout did not recur. Abrupt controls produced **three resets while reading headers and three refusals while connecting**, retained in the [six-failure extract](../evidence/connection-timeout-ci-failures.json). Each retrying gateway saw four internal transport failures and no final failures. Both connection limits had equal final outcomes in this sample.

For the three refusals, conntrack tuples identify `10.244.0.46` and `10.244.0.51`, IPs belonging to deleting Pods. Attempts began about 45–66 ms after the watch received SIGTERM. This associates those refusals with routing to Pods shutting down during abrupt removal. It does not associate the historical timeout with that mechanism. For the three resets, the HTTP client had already closed its socket when the source port was queried; it remained absent, so I did not correlate tuples for those resets. Error phases were retained.

The 16-removal regression on the same runner also retained its failures:

| CI profile | Grace | Failures / direct | Failures / short-TCP GET | Internal transport failures |
| --- | --- | --- | --- | --- |
| No wait | 0 s | 8 / 1,261 | 0 / 1,257 | 7 |
| No wait | 30 s | 5 / 1,269 | 0 / 1,267 | 2 |
| Wait enabled | 0 s | 0 / 1,344 | 0 / 1,344 | 0 |
| Wait enabled | 30 s | 0 / 1,504 | 0 / 1,504 | 0 |

SYN dropping again produced about 3,951 ms of connection waiting in two attempts versus 1,502 ms in three, without recovering the blocked destination. Complete reports remain separate: [new comparison](../evidence/connection-timeout-ci-connection-window.json), [summary](../evidence/connection-timeout-ci-summary.json), [regression matrix](../evidence/connection-timeout-ci-resilience.json), [SYN](../evidence/connection-timeout-ci-syn-budget.json), [HTTP protocol](../evidence/connection-timeout-ci-access-policy.json) and [alerts/recovery](../evidence/connection-timeout-ci-verification.json).

## What was adopted and what remains open

**90 checks passed locally and 90 in CI**: 43 chaos/alert checks, 23 earlier-matrix checks, four protocol checks, three SYN-drop checks and 17 new comparison checks. [Local alert regression](../evidence/connection-timeout-verification.json) took 198.03 s and ended with two ready API Pods, ready PostgreSQL, ten notes, the same PVC and no active alerts. Tests added three notes; all seven existing notes remained unchanged.

I adopted the shorter TCP limit for GET and phase-based logs. Demonstrated improvement is bounded connection waiting with room for three attempts when SYNs are dropped. **No further failure-rate reduction during graceful removal was demonstrated**, because both access paths completed error-free in the new samples. The historical timeout's exact cause remains open. Abrupt-removal errors are better identified, and draining remains enabled; retries do not make writes safe during an interruption.

The [validation summary](../evidence/connection-timeout-validation.json) lists the five reports for each environment. [Final local state](../evidence/connection-timeout-final-state.json) confirms the active limit, no remaining experiments and no remaining observers. Other labs retained eight, seven and 14 notes.

## Reproduce

```bash
python3 tests/access_policy.py
python3 tests/syn_budget.py
python3 tests/connection_window.py --rounds 8
python3 tests/resilience.py --rounds 4
python3 tests/verify.py
```

The SYN test uses Docker and the kind node's `iptables`. The observer uses that node's `conntrack` and explicitly stops its process before closing the Docker client. The initial collection was interrupted after detecting observers outliving their clients. Two completed trials remain in the [partial report](../evidence/connection-window-initial-capture.json), marked incomplete and not passed. Initial observers were stopped and the full comparison restarted with corrected cleanup.

Restart `python3 scripts/access.py` to activate the browser change. To return only to the earlier connection limit, set `get_connect_timeout_seconds` to 3 and restart that process; no image or data change is required. Set `get_max_attempts: 1` to disable GET retries. Reports and direct access remain available in either case.

500 ms is a choice for this lab's local network. A slow network may require more time to connect, and abandoned attempts add load. Testing does not guarantee that a higher-latency remote service benefits from this value. Uncertain writes still require reconciliation before any retry.
