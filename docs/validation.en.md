# What the experiments showed

[Português](validation.md) · [Procedure](runbook.en.md)

In the first local run on 19 October 2026, **43 checks passed in 183.87 seconds**. The [initial report](../evidence/initial-local-verification.json) is retained. Five real Chaos Mesh experiments were executed, with three alerts followed from Prometheus through the local webhook. Each alert's firing and resolved notifications shared the same fingerprint. The six verified notifications survived receiver Pod replacement.

| Experiment | Initial local result |
| --- | --- |
| Abruptly remove one API replica | 14 of 16 samples returned HTTP 200. Two had connection failures. The Pod was replaced and no sustained outage alert fired. |
| Delay API → database by 100 ms per packet | Readiness took about 837 ms and still succeeded. The latency alert fired and resolved after normal experiment removal. |
| Partition API → database for 60 s | Liveness stayed healthy while readiness failed. The dependency alert reached the receiver. The resource remained present when `AllRecovered` confirmed duration-based recovery. Checking recovery and its notification took 64.73 s. |
| Fail both API replicas | Both probes failed and the critical alert reached the receiver. Normal removal restored the original API images and delivered resolved. |
| Abruptly remove PostgreSQL's Pod | PostgreSQL returned with a new Pod UID and the same PVC UID. Rows, IDs and timestamps matched; a new note was written through the API. |

That first run ended with two ready API replicas, one PostgreSQL, two notes, both probes at 1 and no active alerts. No experiment remained pending and `cleanup_errors` was empty. Other labs' workload configurations, images and PVC UIDs were compared before and after and remained unchanged.

## Local repetition with the corrected wait

All **43 checks passed again**, in **183.53 seconds**. This run's first experiment returned 200 for all 16 samples. Observed readiness latency under injected delay was about 824 ms, and checking partition recovery took 64.39 s.

Both API replicas and the replacement PostgreSQL Pod finished ready. Four notes accumulated across the runs remained on the same PVC, both probes returned to 1 and no alert remained active. The [current report](../evidence/chaos-first-release-verification.json) records this state. The difference between 14/16 and 16/16 successful responses reinforces the sampling limitation; the initial failure remains documented.

## Connection failures are part of the result

Passing the checks did not mean zero errors while removing a replica. A sample value of `0` in the report means a client failure without an HTTP response; it is not a server response status. Other samples returned 200. This collection alone does not establish the exact cause of each failure.

Replica recovery was demonstrated. A complete absence of transient errors was not. The alert requires sustained failure for 9 seconds, and periodic probes do not necessarily observe every failed user request. Connection draining, graceful termination, request metrics and an error SLO are useful next experiments. The test keeps these errors visible.

## Decisions made during setup

The WSL kernel provided `sch_netem` as a module, but it was not loaded. The WSL root user loaded it before latency testing. The complete test checks this requirement before injecting faults.

Chart 2.8.4 namespace mode omitted namespace reads and reads of the cluster-scoped `RemoteCluster` resource that the controller attempted to watch. An additional read-only discovery role was added. The test confirms that the controller account can remove Pods in `chaos-demo` and cannot do so in `notes-lab`. This does not remove the daemon's privileges over the node runtime.

The first Prometheus configuration had a scrape timeout longer than its interval, so Prometheus refused to start. The interval was changed to 5 seconds, with a 4-second timeout. Mounted configuration was checked by Prometheus and Alertmanager validators before experiments.

The database uses a headless Service so connection destinations match the Pod IPs selected by NetworkChaos. Tests use a dedicated copy of the application; pausing Argo CD or changing previous labs' shared alert rules was unnecessary.

## CI exposed an incorrect recovery wait

The [first GitHub Actions run](https://github.com/tommmdl/chaos-lab/actions/runs/39313601808) verified alerts and recovery for the first four experiments. During PostgreSQL restart, the test saw a new Pod but attempted SQL before its container started. An older healthy Prometheus sample remained available. The [attempt report](../evidence/ci-first-verification.json) records the failure and successful fault cleanup.

The corrected test requires the new Pod to be Running/Ready with its container ready, a successful real SQL query and both API replicas ready. It then waits for both probes to have timestamps newer than that recovery. A changed UID and the last healthy probe value alone no longer complete verification.

## Repetition in GitHub Actions

The [successful CI run](https://github.com/tommmdl/chaos-lab/actions/runs/39314335897) passed the same **43 checks in 185.60 seconds** on a fresh cluster. Readiness under injected delay took about 837 ms; checking partition recovery took 65.36 s. API and PostgreSQL Pods finished ready, with two notes preserved and no active alerts.

Removing one replica produced **15 HTTP 200 responses and one connection failure in 16 samples** in CI. The corrected wait strengthened database recovery verification; brief connection failures remain an observed behavior of abrupt Pod removal.

The [CI report](../evidence/ci-verification.json), [experiment objects](../evidence/ci) and [run metadata](../evidence/ci-run.json) were downloaded and retained in the repository. The earlier failed attempt also remains visible.

## Read the evidence

[verification.json](../evidence/chaos-first-release-verification.json) includes checks, HTTP samples, observed latency, verified notifications, images and final state. Numbered JSON files retain experiment objects, targets and injection/recovery records. `03-database-partition-injected.json` and `03-database-partition-recovered.json` show the same resource UID before and after duration expiry.

`seconds_until_recovery_checked` ends after checking recovery, the resolved notification and data. It is not a continuous downtime measurement. Full test duration includes waiting for probes, rules, delivery and recovery, but excludes initial installation.

Actual reference notes and the password stay under `.local`. Public evidence includes counts, checks and a hash of rows, without exporting the database or credentials. GitHub Actions creates a fresh cluster and repeats the same exercise, retaining its results as an artifact.

These results cover this environment and these parameters. Machine loss, physical storage failure, simultaneous chaos controller/application failure and external on-call delivery were not tested. HTTP sampling also does not establish a production availability percentage.

## Follow-up: replica removal

The original run above is retained. A separate investigation compares 16 removals under concurrent HTTP traffic, draining and bounded GET retries, including remaining failures and write safety. [Results and new local/CI evidence](connections.en.md).
