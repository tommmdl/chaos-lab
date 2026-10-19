# What the experiments showed

[Português](validation.md) · [Procedure](runbook.en.md)

On 19 October 2026, **43 local checks passed in 183.87 seconds**. Five real Chaos Mesh experiments were executed, with three alerts followed from Prometheus through the local webhook. Each alert's firing and resolved notifications shared the same fingerprint. The six verified notifications survived receiver Pod replacement.

| Experiment | Local result |
| --- | --- |
| Abruptly remove one API replica | 14 of 16 samples returned HTTP 200. Two had connection failures. The Pod was replaced and no sustained outage alert fired. |
| Delay API → database by 100 ms per packet | Readiness took about 837 ms and still succeeded. The latency alert fired and resolved after normal experiment removal. |
| Partition API → database for 60 s | Liveness stayed healthy while readiness failed. The dependency alert reached the receiver. The resource remained present when `AllRecovered` confirmed duration-based recovery. Checking recovery and its notification took 64.73 s. |
| Fail both API replicas | Both probes failed and the critical alert reached the receiver. Normal removal restored the original API images and delivered resolved. |
| Abruptly remove PostgreSQL's Pod | PostgreSQL returned with a new Pod UID and the same PVC UID. Rows, IDs and timestamps matched; a new note was written through the API. |

The final state has two ready API replicas, one PostgreSQL, two notes, both probes at 1 and no active alerts. No experiment remained pending and `cleanup_errors` was empty. Other labs' workload configurations, images and PVC UIDs were compared before and after and remained unchanged.

## Connection failures are part of the result

Passing the checks did not mean zero errors while removing a replica. A sample value of `0` in the report means a client failure without an HTTP response; it is not a server response status. Other samples returned 200. This collection alone does not establish the exact cause of each failure.

Replica recovery was demonstrated. A complete absence of transient errors was not. The alert requires sustained failure for 9 seconds, and periodic probes do not necessarily observe every failed user request. Connection draining, graceful termination, request metrics and an error SLO are useful next experiments. The test keeps these errors visible.

## Decisions made during setup

The WSL kernel provided `sch_netem` as a module, but it was not loaded. The WSL root user loaded it before latency testing. The complete test checks this requirement before injecting faults.

Chart 2.8.4 namespace mode omitted namespace reads and reads of the cluster-scoped `RemoteCluster` resource that the controller attempted to watch. An additional read-only discovery role was added. The test confirms that the controller account can remove Pods in `chaos-demo` and cannot do so in `notes-lab`. This does not remove the daemon's privileges over the node runtime.

The first Prometheus configuration had a scrape timeout longer than its interval, so Prometheus refused to start. The interval was changed to 5 seconds, with a 4-second timeout. Mounted configuration was checked by Prometheus and Alertmanager validators before experiments.

The database uses a headless Service so connection destinations match the Pod IPs selected by NetworkChaos. Tests use a dedicated copy of the application; pausing Argo CD or changing previous labs' shared alert rules was unnecessary.

## Read the evidence

[verification.json](../evidence/verification.json) includes checks, HTTP samples, observed latency, verified notifications, images and final state. Other JSON files retain experiment objects, targets and injection/recovery records. `03-database-partition-injected.json` and `03-database-partition-recovered.json` show the same resource UID before and after duration expiry.

`seconds_until_recovery_checked` ends after checking recovery, the resolved notification and data. It is not a continuous downtime measurement. Full test duration includes waiting for probes, rules, delivery and recovery, but excludes initial installation.

Actual reference notes and the password stay under `.local`. Public evidence includes counts, checks and a hash of rows, without exporting the database or credentials. GitHub Actions creates a fresh cluster and repeats the same exercise, retaining its results as an artifact.

These results cover this environment and these parameters. Machine loss, physical storage failure, simultaneous chaos controller/application failure and external on-call delivery were not tested. HTTP sampling also does not establish a production availability percentage.
