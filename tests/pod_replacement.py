#!/usr/bin/env python3
"""Expose the old two-list race using a real Chaos Mesh removal, without mocks."""

import datetime
import fcntl
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import lab
import experiment
import pod_replacement as replacement_check
import resilience
import verify as original


def main():
    report = {"started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "checks": [], "passed": False,
              "method": "Deliberately inject between the old readiness and UID reads; no mocked Kubernetes responses"}

    def check(name, value):
        report["checks"].append({"name": name, "passed": bool(value)})
        print(("PASS " if value else "FAIL ") + name, flush=True)
        if not value:
            raise AssertionError(name)

    name = "06-api-graceful-removal"
    active = False
    started = time.monotonic()
    with (lab.LOCAL / "operation.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lab.guard(lab.APP)
        try:
            check("No existing fault is replaced", all(not json.loads(
                lab.k(lab.APP, "get", kind, "-o", "json").stdout)["items"]
                for kind in ["podchaos", "networkchaos"]))
            rows, outside = original.rows(), original.outside()
            pvc = lab.obj(lab.APP, "pvc", "postgres-data")["metadata"]["uid"]
            resilience.profile(True)
            replacement = replacement_check.Replacement()
            report["replacement"] = replacement.report
            # This is the first call from the former predicate, evaluated before deletion.
            old_ready = original.ready_pods("api", 2)
            check("The first readiness read sees two ready Pods", old_ready)
            kind = experiment.apply(name)
            active = True
            deadline = time.monotonic() + 15
            transition = None
            while time.monotonic() < deadline:
                view = replacement.observe(kind, name)
                if (replacement_check.uids(view) != replacement_check.uids(replacement.before)
                        and not replacement_check.ready(view)):
                    transition = view
                    break
                time.sleep(0.1)
            check("Real deletion exposes changed UIDs while the replica pair is not ready", transition is not None)
            # Second call from the former predicate; the deliberate pause makes the race reproducible.
            second_uids = original.uids("api")
            old_result = old_ready and second_uids != replacement_check.uids(replacement.before)
            report["controlled_interleaving"] = {
                "first_read_ready": old_ready, "second_read_uids": sorted(second_uids),
                "old_predicate_result": old_result, "coherent_observation": transition,
                "new_predicate_result": replacement.complete(transition),
            }
            check("The old predicate accepts an incomplete replacement; the new one rejects it",
                  old_result and not replacement.complete(transition))
            replacement.wait(kind, name)
            experiment.remove(name)
            active = False
            check("The selected Pod is gone and exactly two Pods are ready after cleanup", replacement.confirm())
            check("Rows and timestamps are unchanged", original.rows() == rows)
            check("PostgreSQL keeps its PVC", lab.obj(lab.APP, "pvc", "postgres-data")["metadata"]["uid"] == pvc)
            check("Other labs keep their workloads, images and PVC identities", original.outside() == outside)
            report["passed"] = True
        except Exception as error:
            report["error"] = str(error)
            raise
        finally:
            try:
                if active:
                    experiment.remove(name)
                lab.wait(lambda: replacement_check.ready(replacement_check.snapshot()), 120)
                report["final_ready"] = True
            except Exception as error:
                report.update(passed=False, final_ready=False, recovery_error=str(error))
            report["duration_seconds"] = round(time.monotonic() - started, 2)
            (lab.ROOT / "evidence/pod-replacement.json").write_text(json.dumps(report, indent=2) + "\n")
            if not report.get("final_ready"):
                raise RuntimeError("API recovery needs attention")


if __name__ == "__main__":
    main()
