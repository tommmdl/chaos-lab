# The Pod replacement check that returned too early

[Português](pod-replacement.md)

[CI run 39413620240](https://github.com/tommmdl/chaos-lab/actions/runs/39413620240) collected and attributed the resets, then failed at `Pod replacement completes: drain=True, grace=30, trial=3`. The cluster ended with two ready replicas. That does not make the run successful: its original report still has `passed: false`.

## What we found

The wait issued two independent queries: one checked for two ready Pods; the next looked for changed UIDs. Between those reads, Chaos Mesh could start deletion and the ReplicaSet could create a replacement. The first response still described the previous healthy pair; the second could already include a third, unready Pod. The expression returned true without observing a completed replacement.

In the failed attempt, the watch recorded deletion of `api-5459756fdf-grzwn`, creation of `api-5459756fdf-cntqs`, and the replacement still unready before collection stopped. The [event summary](../evidence/pod-replacement-historical-analysis.json) supports this early-exit diagnosis. The old test did not retain the exact responses to those two queries, so we cannot reconstruct them with certainty.

To verify the defect, we injected a real Chaos Mesh removal between the old predicate's two reads. This pause is deliberate: it reproduces a valid ordering without relying on scheduler luck. There are no mocked Kubernetes responses. The test confirmed that the old predicate accepted an incomplete state and the new one rejected it, waiting for actual recovery.

## What changed

The shared [pod_replacement.py](../scripts/pod_replacement.py) module evaluates readiness and identity from the same list response. For the API, it requires exactly two running Pods, all containers ready, no terminating Pod, and a UID set different from the initial pair. It also matches the target recorded as successfully injected by Chaos Mesh to an initial Pod and requires that UID to be absent.

The resilience matrix, the TCP investigation and the initial experiment suite use this module. The latter also checks PostgreSQL replacement with an expected count of one Pod. After deleting the experiment resource, a fresh complete read confirms the outcome. Traffic suites retain their 120-second wait; the initial suite retains its 100-second wait. We did not raise the timeout or rerun the suite until it passed.

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

The matrix passed its recovery assertions; this does not mean every request succeeded. Three GETs still failed during abrupt removals without draining. Their attempts and errors remain in the samples.

The [local TCP suite](../evidence/pod-replacement-local-connection-window.json) passed 42 checks over 12 removals in 306.57 seconds. Graceful removals had 0/5,631 direct failures; abrupt controls had 2/2,833. The three-second and 500 ms connection lanes had no final failures during abrupt controls, but each recovered two transport failures. All [24 captures](../evidence/pod-replacement-local-packet-archive.json) were archived with verified hashes: 168,887 frames and no reported capture drops. Re-analysis from the archived files exactly matched the original result: [six RSTs attributed to listener shutdown in the API Pod](../evidence/pod-replacement-local-reset-origins.json), with no unresolved observations. These are observations from this run, not an availability guarantee.

The [local initial suite](../evidence/pod-replacement-local-verification.json) then passed all 43 checks in 202.37 seconds, including API and PostgreSQL replacement with the shared observer. This brings the [local total](../evidence/pod-replacement-local-summary.json) to 116 checks. The first three suites exercised commit `4bd2eb7`; the initial suite exercised `a4bad03`, which extends the same predicate to the API and database checks without changing the API defaults.

## First CI after the fix

[Run 39416708311](https://github.com/tommmdl/chaos-lab/actions/runs/39416708311), commit `4bd2eb7`, passed all **123 checks**. Its [summary](../evidence/pod-replacement-ci-39416708311-summary.json) and full reports retain 16 completed replacements in the resilience matrix, 12 in the TCP suite, and the reproduced old false positive. The matrix still had these HTTP failures:

| Draining | Chaos grace | Direct failures/requests | GET failures/requests |
|---|---:|---:|---:|
| no | 0 s | 5/1222 | 0/1213 |
| no | 30 s | 29/839 | 4/1157 |
| yes | 0 s | 2/1280 | 0/1279 |
| yes | 30 s | 1/1505 | 0/1519 |

Five direct requests timed out around six seconds, including one with draining enabled and a 30-second grace period. Four GETs also exhausted their bounded retry policy without draining. This recovery-check fix does not explain or remove those failures. Packet capture belongs to the earlier TCP step, not this matrix, so these later timeouts cannot be attributed from that capture.

The TCP step itself had no final HTTP failures and no observed NodePort RST. Its 12 captures retained 164,210 frames with no drops; hash checks and archived re-analysis passed. With no observed reset, the attribution result is `null`, not proof that resets were eliminated.

## Final CI with all replacement checks corrected

[Run 39417441792](https://github.com/tommmdl/chaos-lab/actions/runs/39417441792), commit `a4bad03`, also passed all **123 checks**. The [summary](../evidence/pod-replacement-ci-39417441792-summary.json) retains the workflow steps, final runner state and results from the accompanying reports. All 16 matrix replacements and all 12 TCP-suite replacements were confirmed from coherent observations. The initial suite also recorded the API and PostgreSQL targets disappearing, followed by the expected ready replicas. Original data and PVC identity checks passed.

The final matrix recorded these HTTP failures:

| Draining | Chaos grace | Direct failures/requests | GET failures/requests |
|---|---:|---:|---:|
| no | 0 s | 9/1039 | 2/1213 |
| no | 30 s | 19/819 | 4/1185 |
| yes | 0 s | 3/873 | 0/1192 |
| yes | 30 s | 0/1555 | 0/1545 |

The [matrix samples](../evidence/pod-replacement-ci-39417441792-resilience.json) include **nine direct timeouts around six seconds**, three during abrupt removals with draining. Six GETs ended in failure without draining. The profile with draining and a 30-second grace period had no final HTTP failure in this run, but the earlier passing CI still had a timeout in that profile. Those differing outcomes remain visible; this is not a claim of zero connection failures.

In the [TCP step](../evidence/pod-replacement-ci-39417441792-connection-window.json), graceful removals had 0/3,235 direct failures; abrupt controls had 3/1,367. The three-second and 500 ms lanes had no final failures but recovered three and two transport failures respectively. The [12 captures](../evidence/pod-replacement-ci-39417441792-packet-archive.json) retained 157,961 frames with no reported drops. [Analysis](../evidence/pod-replacement-ci-39417441792-reset-origins.json) attributed all eight observed NodePort RSTs: seven to listener shutdown in the API Pod and one to `NO_SOCKET`. Re-analysis from the archived files exactly matched the runner. This capture does not cover the later matrix timeouts.

Both successful runs are retained alongside the earlier failed run. The workflow was not rerun unchanged to wait for a green result: the second run validates the additional fix in the initial API and PostgreSQL checks. Capture hashes are verified, and provenance reports retain hashes of the original downloaded JSON files before their capture paths were relocated. Local and CI experiment manifests/status reports remain under [the evidence directory](../evidence/pod-replacement). Each failure sample stays in its full report; no transport error or historical failed assertion was suppressed.

## Reproduce in your environment

With the lab installed, run one suite at a time:

```bash
python3 tests/pod_replacement.py
python3 tests/resilience.py --rounds 4
python3 tests/connection_window.py --rounds 8 --attribute-resets
```

Capture needs the tracing and packet-capture privileges described in the [reset investigation](rst-origin.en.md). CI prepares these dependencies in a disposable cluster. Canonical reports are written under `evidence`; save each attempt under a separate name before repeating it to retain history.

Fixing this check does not eliminate actual connection failures during abrupt removals. They remain in HTTP samples, attempt counts and reset analysis. The historical six-second timeout during graceful removal remains unexplained.
