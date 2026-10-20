# Observe, inject and recover

[Português](runbook.md) · [Home](../README.en.md)

Start with `python3 scripts/lab.py status`. Expect two ready API replicas, one PostgreSQL and four observation components. The Chaos Mesh controller and daemon must be healthy. In Prometheus, inspect `probe_success{job="chaos-http"}`: both series should be 1. The inbox at localhost:8027/api/events shows received notifications.

## Apply an experiment

Use `python3 scripts/experiment.py apply NAME`, using the filename without `.json`. Start with `01-api-pod-kill`, and retain Pod UIDs before and after. Network experiments are `02-database-delay` and `03-database-partition`. Both-replica downtime is `04-api-failure`; the database restart is `05-database-pod-kill`.

The runner validates scope and shares a lock with the complete test. Every resource targets `chaos-demo`, with selectors identifying the application and lab. `up` refuses reconfiguration while experiment objects exist. An existing Chaos Mesh installation that does not belong to this lab is also refused.

To follow progress:

```bash
bash scripts/k.sh get podchaos,networkchaos
bash scripts/k.sh describe networkchaos 03-database-partition
bash scripts/k.sh get pods,pvc
bash scripts/k.sh -n chaos-system logs deployment/chaos-controller-manager --tail=60
bash scripts/k.sh -n chaos-system logs daemonset/chaos-daemon --tail=60
```

`AllInjected` confirms that the controller injected a fault. Also inspect probes, application behavior and received notifications. `AllRecovered` and healthy probes are part of recovery verification. The partition test waits for those conditions while the resource remains present with the same UID, without deleting it to trigger recovery.

## Read the signals

- `ChaosApiSlow`: readiness still succeeds but takes over 400 ms for at least 9 seconds. Configured delay is per packet; total database connection time is higher.
- `ChaosDatabaseUnavailable`: liveness succeeds while readiness fails. A blocked connection may hit the probe timeout before returning HTTP 503. The known experiment directs the failure at the database connection.
- `ChaosApiUnavailable`: both probes fail for at least 9 seconds. Inspect Pods and the active experiment.
- `ChaosProbeMissing`: one probe series is missing for at least 15 seconds. Inspect blackbox and scrape configuration. This does not replace external monitoring of Prometheus itself.

Application traffic uses the normal API Service, which respects readiness. Observation uses `api-probes` with `publishNotReadyAddresses=true` to distinguish a live process from an unavailable dependency. PostgreSQL's Service is headless, allowing Pod selectors to reach the connection without depending on ClusterIP address translation.

The local receiver retains up to 500 SQLite events on a PVC. It sends no email or third-party messages. Each alert should have firing and resolved events with the same fingerprint. If resolved is missing, check whether the fault ended, Prometheus resolved the rule and Alertmanager can reach the receiver.

## Cancel and verify recovery

```bash
python3 scripts/experiment.py remove 02-database-delay
python3 scripts/lab.py recover
```

The first command removes one selected experiment. The second removes PodChaos and NetworkChaos resources labeled as belonging to this lab. Both use normal deletion and wait for finalizers. Wait for the complete test to finish before running another mutation command.

Then confirm ready Pods, both `probe_success` values at 1, resolved alerts and preserved data. The test attempts to remove its own faults even if an assertion fails and records problems under `cleanup_errors`. Experiment duration depends on the controller; it is not an independent kernel timer.

If cleanup stalls, inspect the resource and logs. Restore the controller and daemon with their original configuration and repeat normal removal. Never force-remove finalizers or delete CRDs or the daemon while faults remain: network changes may stay applied. Namespace, UIDs and experiment records help identify the correct target.

## Stop while preserving data

`recover` retains workloads and PVCs. To pause the application after confirming that no experiments remain, scale the API to zero, wait for its Pods to terminate, then stop PostgreSQL. Observation components can also be scaled to zero. Retain PVCs to continue later.

The Helm release is `chaos-lab` in `chaos-system`. Uninstall only after recovering every experiment; uninstalling does not automatically remove application data. CRDs are cluster resources: check other uses before deleting them. There is no local command that deletes the cluster or every volume. Automatic workflow cleanup targets only the disposable cluster created on that runner.

NodePorts 30434–30437 provide access within the local Docker network; the browser access process exposes 8024–8027 only on 127.0.0.1. This path has no production authentication. Database credentials, kubeconfig, generated Helm files and reference data remain in `.local`, outside Git.

## Inspect connections during removal

Original `01-api-pod-kill` uses zero grace and skips the draining hook. Use `06-api-graceful-removal` for a removal with a grace period. `python3 tests/resilience.py --rounds 4` compares both removals, with and without waiting, under direct and local gateway traffic. Restart the access process after updating its code. At localhost:8024, inspect `X-Lab-Upstream-Attempts` and `X-Lab-Transport-Failures`; a recovered read can conceal a failure if you only inspect the final status. Reports retain both. POST and DELETE are never retried automatically: reconcile an uncertain write against the database before resending it. [Diagnosis, measurements and limits](connections.en.md).

Access now distinguishes TCP establishment from response reading. For GET, `get_connect_timeout_seconds` controls establishment; the adopted value is 0.5 s. Inspect `phase` and `elapsed_ms` in transport-failure logs before changing it. [CI timeout analysis and reverting the connection limit](connection-timeout.en.md).
