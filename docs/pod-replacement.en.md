# The Pod replacement check that returned too early

[Português](pod-replacement.md)

[CI run 39413620240](https://github.com/tommmdl/chaos-lab/actions/runs/39413620240) collected and attributed the resets, then failed at `Pod replacement completes: drain=True, grace=30, trial=3`. The cluster ended with two ready replicas. That does not make the run successful: its original report still has `passed: false`.

## What we found

The wait issued two independent queries: one checked for two ready Pods; the next looked for changed UIDs. Between those reads, Chaos Mesh could start deletion and the ReplicaSet could create a replacement. The first response still described the previous healthy pair; the second could already include a third, unready Pod. The expression returned true without observing a completed replacement.

In the failed attempt, the watch recorded deletion of `api-5459756fdf-grzwn`, creation of `api-5459756fdf-cntqs`, and the replacement still unready before collection stopped. The [event summary](../evidence/pod-replacement-historical-analysis.json) supports this early-exit diagnosis. The old test did not retain the exact responses to those two queries, so we cannot reconstruct them with certainty.

To verify the defect, we injected a real Chaos Mesh removal between the old predicate's two reads. This pause is deliberate: it reproduces a valid ordering without relying on scheduler luck. There are no mocked Kubernetes responses. The test confirmed that the old predicate accepted an incomplete state and the new one rejected it, waiting for actual recovery.

## What changed

The shared [pod_replacement.py](../scripts/pod_replacement.py) module evaluates readiness and identity from the same list response. It requires exactly two running Pods, all containers ready, no terminating Pod, and a UID set different from the initial pair. It also matches the target recorded as successfully injected by Chaos Mesh to an initial Pod and requires that UID to be absent.

The resilience matrix, the TCP investigation and the initial experiment suite use this module. The latter also checks PostgreSQL replacement with an expected count of one Pod. After deleting the experiment resource, a fresh complete read confirms the outcome. The deadline remains 120 seconds. We did not raise the timeout or rerun the suite until it passed.

Each attempt retains its initial state, resource versions, observations while waiting, experiment target, and state after cleanup. On timeout or error, collected samples and states remain in the report. Final recovery must also be healthy before a report can declare success.

This follows Kubernetes' distinction between [Pod identity, phase, readiness and termination](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/): `Running` alone does not establish recovery.

## Tests and evidence

The [local control](../evidence/pod-replacement-local-regression.json) passed all eight checks: it reproduced the old false positive, rejected that state with the new predicate, and verified recovery. Database rows, timestamps, the PostgreSQL PVC, and other labs' resources were unchanged.

The [local resilience matrix](../evidence/pod-replacement-local-resilience.json) completed all 16 removals and passed 23 checks in 270.96 seconds. Every retained observation confirms that the selected UID disappeared and two Pods were ready after cleanup. Rows, timestamps and the database PVC survived. GET retry limits and the committed POST with a lost response also passed their real HTTP controls.

| Draining | Chaos grace | Direct failures/requests | GET failures/requests |
|---|---:|---:|---:|
| no | 0 s | 41/1415 | 3/1882 |
| no | 30 s | 5/2016 | 0/2075 |
| yes | 0 s | 1/1974 | 0/1969 |
| yes | 30 s | 0/1787 | 0/1783 |

The matrix passed its recovery assertions; this does not mean every request succeeded. Three GETs still failed during abrupt removals without draining. Their attempts and errors remain in the samples. Results from the TCP suite and GitHub Actions will be added after execution.

## Reproduce in your environment

With the lab installed, run one suite at a time:

```bash
python3 tests/pod_replacement.py
python3 tests/resilience.py --rounds 4
python3 tests/connection_window.py --rounds 8 --attribute-resets
```

Capture needs the tracing and packet-capture privileges described in the [reset investigation](rst-origin.en.md). CI prepares these dependencies in a disposable cluster. Canonical reports are written under `evidence`; save each attempt under a separate name before repeating it to retain history.

Fixing this check does not eliminate actual connection failures during abrupt removals. They remain in HTTP samples, attempt counts and reset analysis. The historical six-second timeout during graceful removal remains unexplained.
