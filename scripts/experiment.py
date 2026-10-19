#!/usr/bin/env python3
"""Validate the intended blast radius before creating an experiment."""

import argparse, fcntl, json
from pathlib import Path
import lab


def validate(item):
    m = item.get("metadata", {})
    s = item.get("spec", {})
    if item.get("apiVersion") != "chaos-mesh.org/v1alpha1" or item.get("kind") not in [
        "PodChaos",
        "NetworkChaos",
    ]:
        raise ValueError("Unsupported experiment kind")
    if (
        m.get("namespace") != lab.APP
        or m.get("labels", {}).get("app.kubernetes.io/part-of") != lab.OWNER
    ):
        raise ValueError("Experiment must belong to the dedicated namespace")

    def selector(value):
        if set(value) != {"namespaces", "labelSelectors"} or value["namespaces"] != [
            lab.APP
        ]:
            raise ValueError("Cross-namespace or broad selector refused")
        labels = value["labelSelectors"]
        if (
            set(labels) != {"app", "app.kubernetes.io/part-of"}
            or labels["app.kubernetes.io/part-of"] != lab.OWNER
            or labels["app"] not in ["api", "postgres"]
        ):
            raise ValueError("Target must be an owned application or database Pod")

    selector(s.get("selector", {}))
    if s.get("mode") not in ["one", "all"]:
        raise ValueError("Unsupported mode")
    if item["kind"] == "NetworkChaos":
        if s.get("action") not in ["delay", "partition"] or s.get("direction") != "to":
            raise ValueError("Unsupported network action")
        if s.get("externalTargets"):
            raise ValueError("External network targets refused")
        selector(s.get("target", {}).get("selector", {}))
        if (
            s["selector"]["labelSelectors"]["app"] != "api"
            or s["target"]["selector"]["labelSelectors"]["app"] != "postgres"
        ):
            raise ValueError("Only the API-to-database link may be changed")
        if s["target"].get("mode") != "all":
            raise ValueError("Unexpected target mode")
        if s["action"] == "delay" and s.get("delay") != {
            "latency": "100ms",
            "jitter": "0ms",
            "correlation": "100",
        }:
            raise ValueError("Unexpected delay parameters")
    elif s.get("action") not in ["pod-kill", "pod-failure"]:
        raise ValueError("Unsupported Pod action")
    if s.get("action") != "pod-kill" and s.get("duration") not in ["60s", "120s"]:
        raise ValueError("A bounded duration is required")
    return item


def load(name):
    allowed = {p.stem: p for p in (lab.ROOT / "experiments").glob("*.json")}
    if name not in allowed:
        raise ValueError("Unknown experiment")
    item = validate(json.loads(allowed[name].read_text()))
    if item["metadata"].get("name") != name:
        raise ValueError("Experiment name must match its file")
    return item


def apply(name):
    lab.guard(lab.APP)
    item = load(name)
    existing = lab.k(lab.APP, "get", item["kind"], name, ok=False)
    if existing.returncode == 0:
        raise RuntimeError(
            "An experiment with this name already exists; inspect/recover first"
        )
    if "NotFound" not in existing.stderr:
        raise RuntimeError(existing.stderr)
    lab.apply(lab.APP, item)
    return item["kind"]


def remove(name):
    lab.guard(lab.APP)
    item = load(name)
    lab.k(
        lab.APP,
        "delete",
        item["kind"],
        name,
        "--ignore-not-found",
        "--timeout=120s",
        timeout=140,
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["apply", "remove"])
    p.add_argument("name")
    args = p.parse_args()
    with (lab.LOCAL / "operation.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        (apply if args.action == "apply" else remove)(args.name)
