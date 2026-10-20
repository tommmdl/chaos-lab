"""Observe readiness and identity in one PodList; keep the evidence on failure."""

import json
import time

import lab


def snapshot():
    value = json.loads(lab.k(lab.APP, "get", "pods", "-l", "app=api", "-o", "json").stdout)
    items = []
    for pod in value["items"]:
        metadata, status = pod["metadata"], pod.get("status", {})
        containers = status.get("containerStatuses", [])
        items.append({
            "name": metadata["name"], "uid": metadata["uid"],
            "resource_version": metadata["resourceVersion"],
            "deletion_timestamp": metadata.get("deletionTimestamp"),
            "phase": status.get("phase"),
            "ready": any(c["type"] == "Ready" and c["status"] == "True"
                         for c in status.get("conditions", [])),
            "containers_running_ready": bool(containers)
                and len(containers) == len(pod["spec"]["containers"])
                and all(c.get("ready") and "running" in c.get("state", {}) for c in containers),
        })
    return {"time": time.time(), "resource_version": value["metadata"]["resourceVersion"],
            "pods": sorted(items, key=lambda p: p["name"])}


def ready(view):
    return len(view["pods"]) == 2 and all(
        p["phase"] == "Running" and p["ready"] and p["containers_running_ready"]
        and not p["deletion_timestamp"] for p in view["pods"])


def uids(view):
    return {p["uid"] for p in view["pods"]}


class Replacement:
    def __init__(self, baseline=None):
        self.before = snapshot() if baseline is None else baseline
        self.report = {"before": self.before, "observations": [], "completed": False}
        self.targets = set()
        if not ready(self.before):
            raise RuntimeError("Pod replacement requires exactly two ready API Pods before injection")

    def complete(self, view):
        current = uids(view)
        return (ready(view) and current != uids(self.before) and bool(self.targets)
                and self.targets.isdisjoint(current))

    def observe(self, kind, name):
        resource = lab.obj(lab.APP, kind, name)
        records = resource.get("status", {}).get("experiment", {}).get("containerRecords", [])
        before_by_name = {lab.APP + "/" + p["name"]: p["uid"] for p in self.before["pods"]}
        self.targets = {before_by_name[r["id"]] for r in records
                        if r.get("id") in before_by_name and any(
                            e.get("operation") == "Apply" and e.get("type") == "Succeeded"
                            for e in r.get("events", []))}
        view = snapshot()
        view.update(experiment_uid=resource["metadata"]["uid"], records=records,
                    target_uids=sorted(self.targets))
        view["replacement_complete"] = self.complete(view)
        self.report["observations"].append(view)
        return view

    def wait(self, kind, name, timeout=120):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # API/CLI errors are retained and fail the test instead of looking like progress.
            try:
                view = self.observe(kind, name)
            except Exception as error:
                self.report["error"] = str(error)
                raise
            if view["replacement_complete"]:
                self.report["completed"] = True
                return view
            time.sleep(0.5)
        self.report["error"] = "Timed out waiting for the selected Pod to disappear and two ready API Pods"
        raise TimeoutError(self.report["error"])

    def confirm(self):
        view = snapshot()
        view["replacement_complete"] = self.complete(view)
        self.report["after_cleanup"] = view
        return view["replacement_complete"]
