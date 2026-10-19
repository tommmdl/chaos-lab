#!/usr/bin/env python3
"""Observe real Chaos Mesh faults, Prometheus alerts and durable notifications."""

import copy, datetime, fcntl, hashlib, json, subprocess, sys, time, urllib.error, urllib.parse, urllib.request, uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import lab, experiment

IP = lab.run(
    [
        "docker",
        "inspect",
        "-f",
        '{{(index .NetworkSettings.Networks "kind").IPAddress}}',
        "devops-notes-control-plane",
    ]
).stdout.strip()
API = f"http://{IP}:30434"
PROM = f"http://{IP}:30435"
AM = f"http://{IP}:30436"
INBOX = f"http://{IP}:30437"
REPORT = {
    "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "checks": [],
    "experiments": [],
    "notifications": [],
    "images": lab.IMAGES,
}
ACTIVE = []


def check(name, value, detail=None):
    REPORT["checks"].append(
        {
            "name": name,
            "passed": bool(value),
            **({"detail": detail} if detail is not None else {}),
        }
    )
    print(("PASS " if value else "FAIL ") + name, flush=True)
    if not value:
        raise AssertionError(name)


def request(base, path, body=None):
    req = urllib.request.Request(
        base + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            raw = r.read()
            return r.status, (
                json.loads(raw) if raw[:1] in [b"{", b"["] else raw.decode()
            )
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except OSError as e:
        return 0, str(e)


def query(expr):
    status, data = request(
        PROM, "/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    )
    if status != 200:
        raise RuntimeError("Prometheus query failed")
    return data["data"]["result"]


def metric(name, endpoint):
    values = query(name + '{job="chaos-http",endpoint="' + endpoint + '"}')
    return float(values[0]["value"][1]) if len(values) == 1 else None


def healthy():
    return metric("probe_success", "healthz") == metric("probe_success", "readyz") == 1


def events():
    status, data = request(INBOX, "/api/events")
    if status != 200:
        raise RuntimeError("Webhook inbox unavailable")
    return data["events"]


def cursor():
    return max((e["id"] for e in events()), default=0)


def notification(name, status, after):
    for event in events():
        if event["id"] <= after:
            continue
        for alert in event["payload"]["alerts"]:
            if alert["labels"].get("alertname") == name and alert["status"] == status:
                return {
                    "id": event["id"],
                    "received_at": event["received_at"],
                    "status": status,
                    "name": name,
                    "fingerprint": alert["fingerprint"],
                    "receiver": event["payload"]["receiver"],
                    "labels": alert["labels"],
                }
    return None


def firing(name):
    status, data = request(PROM, "/api/v1/alerts")
    return status == 200 and any(
        a["labels"].get("alertname") == name and a["state"] == "firing"
        for a in data["data"]["alerts"]
    )


def fired(name, after):
    lab.wait(lambda: firing(name), 80)
    check(name + ": Prometheus fires from actual probe metrics", True)
    event = lab.wait(lambda: notification(name, "firing", after), 60)
    REPORT["notifications"].append(event)
    check(
        name + ": Alertmanager delivers a firing webhook",
        event["receiver"] == "chaos-local"
        and event["labels"].get("lab") == "chaos-lab",
    )
    return event


def resolved(name, after, previous):
    event = lab.wait(lambda: notification(name, "resolved", after), 80)
    REPORT["notifications"].append(event)
    check(
        name + ": resolved webhook matches the firing fingerprint",
        event["fingerprint"] == previous["fingerprint"],
    )


def pods(name):
    return json.loads(
        lab.k(lab.APP, "get", "pods", "-l", "app=" + name, "-o", "json").stdout
    )["items"]


def uids(name):
    return {p["metadata"]["uid"] for p in pods(name)}


def available(name, count):
    return (
        lab.obj(lab.APP, "deployment", name)
        .get("status", {})
        .get("availableReplicas", 0)
        == count
    )


def rows():
    return json.loads(
        lab.sql(
            "SELECT coalesce(json_agg(t ORDER BY id),'[]'::json) FROM (SELECT id,title,created_at FROM notes) t;"
        )
    )


def outside():
    resources = json.loads(
        lab.k(lab.APP, "get", "deploy,sts,pvc", "-A", "-o", "json").stdout
    )["items"]
    result = []
    for item in resources:
        if item["metadata"]["namespace"] in [lab.APP, lab.OBS, lab.SYSTEM]:
            continue
        value = {
            "namespace": item["metadata"]["namespace"],
            "kind": item["kind"],
            "name": item["metadata"]["name"],
            "uid": item["metadata"]["uid"],
        }
        if item["kind"] in ["Deployment", "StatefulSet"]:
            value.update(
                replicas=item["spec"]["replicas"],
                images=[
                    c["image"] for c in item["spec"]["template"]["spec"]["containers"]
                ],
            )
        result.append(value)
    return sorted(result, key=lambda x: (x["namespace"], x["kind"], x["name"]))


def condition(kind, name, type):
    return any(
        c["type"] == type and c["status"] == "True"
        for c in lab.obj(lab.APP, kind, name).get("status", {}).get("conditions", [])
    )


def start(name):
    kind = experiment.apply(name)
    ACTIVE.append((kind, name))
    return kind


def capture(kind, name, stage):
    resource = lab.obj(lab.APP, kind, name)
    safe = {
        k: resource[k]
        for k in ["apiVersion", "kind", "spec", "status"]
        if k in resource
    }
    safe["metadata"] = {
        k: resource["metadata"][k]
        for k in ["name", "namespace", "uid", "creationTimestamp"]
    }
    (lab.ROOT / "evidence" / f"{name}-{stage}.json").write_text(
        json.dumps(safe, indent=2) + "\n"
    )
    return safe


def finish(name):
    experiment.remove(name)
    ACTIVE[:] = [(k, n) for k, n in ACTIVE if n != name]


def verify():
    for ns in [lab.APP, lab.OBS, lab.SYSTEM]:
        lab.guard(ns)
    check(
        "Kernel has the netem module required for latency injection",
        Path("/sys/module/sch_netem").exists(),
    )
    for kind in ["podchaos", "networkchaos"]:
        check(
            "No pre-existing " + kind + " experiment is replaced",
            not json.loads(lab.k(lab.APP, "get", kind, "-o", "json").stdout)["items"],
        )
    protected = outside()
    lab.wait(healthy, 120)
    lab.wait(lambda: available("api", 2) and available("postgres", 1), 90)
    check("Two API replicas, PostgreSQL and both probes are healthy", True)
    for deploy in ["prometheus", "blackbox", "alertmanager", "receiver"]:
        check(
            deploy + " is available in the observation namespace",
            lab.obj(lab.OBS, "deployment", deploy)["status"].get("availableReplicas")
            == 1,
        )
    lab.k(
        lab.OBS,
        "exec",
        "deployment/prometheus",
        "--",
        "/bin/promtool",
        "check",
        "config",
        "/etc/prometheus/prometheus.yml",
    )
    lab.k(
        lab.OBS,
        "exec",
        "deployment/alertmanager",
        "--",
        "/bin/amtool",
        "check-config",
        "/etc/config/alertmanager.yml",
    )
    check("Mounted Prometheus rules and Alertmanager configuration validate", True)
    sa = "system:serviceaccount:" + lab.SYSTEM + ":chaos-controller-manager"
    allowed = lab.k(
        lab.APP, "auth", "can-i", "delete", "pods", "--as=" + sa, ok=False
    ).stdout.strip()
    denied = lab.k(
        "notes-lab", "auth", "can-i", "delete", "pods", "--as=" + sa, ok=False
    ).stdout.strip()
    check(
        "Controller may delete target Pods but not Pods in notes-lab",
        allowed == "yes" and denied == "no",
    )
    manager = lab.obj(lab.SYSTEM, "deployment", "chaos-controller-manager")
    env = {
        v["name"]: v.get("value")
        for v in manager["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    check(
        "Namespace filtering is enabled in the actual controller",
        env.get("ENABLE_FILTER_NAMESPACE") == "true"
        and lab.obj(lab.APP, "namespace", lab.APP)["metadata"]["annotations"].get(
            "chaos-mesh.org/inject"
        )
        == "enabled",
    )
    invalid = copy.deepcopy(experiment.load("01-api-pod-kill"))
    invalid["spec"]["selector"]["namespaces"] = ["notes-lab"]
    try:
        experiment.validate(invalid)
    except ValueError:
        check("Runner rejects a selector outside the lab before applying it", True)
    else:
        check("Runner rejects a selector outside the lab before applying it", False)
    check(
        "Database DNS resolves through a headless service for Pod-targeted faults",
        lab.obj(lab.APP, "service", "postgres")["spec"]["clusterIP"] == "None",
    )
    marker = "chaos-" + uuid.uuid4().hex + " — recuperação / recovery"
    status, note = request(API, "/notes", {"title": marker})
    check("A real API write is acknowledged before faults", status == 201)
    baseline = rows()
    pvc_uid = lab.obj(lab.APP, "pvc", "postgres-data")["metadata"]["uid"]
    (lab.LOCAL / "expected-private.json").write_text(
        json.dumps({"rows": baseline, "pvc_uid": pvc_uid}, indent=2) + "\n"
    )
    check(
        "Readiness latency starts below the alert threshold",
        metric("probe_duration_seconds", "readyz") < 0.4,
    )

    name = "01-api-pod-kill"
    before = uids("api")
    after = cursor()
    kind = start(name)
    samples = []
    for _ in range(16):
        status, _ = request(API, "/healthz")
        samples.append(status)
        time.sleep(0.3)
    lab.wait(lambda: available("api", 2) and uids("api") != before, 100)
    lab.wait(healthy)
    check(
        "Chaos Mesh removes an API Pod and Kubernetes replaces it",
        uids("api") != before and len(uids("api")) == 2,
    )
    check(
        "API service serves successful requests during the replica experiment",
        200 in samples,
        {"sample_statuses": samples, "failed_samples": sum(x != 200 for x in samples)},
    )
    check(
        "Replica loss does not produce a sustained API outage alert",
        not firing("ChaosApiUnavailable")
        and not notification("ChaosApiUnavailable", "firing", after),
    )
    capture(kind, name, "completed")
    finish(name)
    REPORT["experiments"].append(
        {"name": name, "sample_statuses": samples, "data_equal": rows() == baseline}
    )
    check("API Pod replacement preserves all database rows", rows() == baseline)

    name = "02-database-delay"
    after = cursor()
    kind = start(name)
    lab.wait(lambda: condition(kind, name, "AllInjected"), 90)
    lab.wait(
        lambda: metric("probe_success", "readyz") == 1
        and metric("probe_duration_seconds", "readyz") > 0.4,
        60,
    )
    latency = metric("probe_duration_seconds", "readyz")
    check(
        "Injected delay makes readiness slow while both probes still succeed",
        healthy(),
        {"readyz_seconds": latency},
    )
    event = fired("ChaosApiSlow", after)
    capture(kind, name, "injected")
    finish(name)
    lab.wait(lambda: healthy() and metric("probe_duration_seconds", "readyz") < 0.4, 90)
    resolved("ChaosApiSlow", after, event)
    check(
        "Deleting the delay experiment restores latency and preserves rows",
        rows() == baseline,
    )
    REPORT["experiments"].append(
        {
            "name": name,
            "readyz_seconds": latency,
            "recovery": "delete through normal finalizers",
            "data_equal": rows() == baseline,
        }
    )

    name = "03-database-partition"
    after = cursor()
    kind = start(name)
    uid = lab.obj(lab.APP, kind, name)["metadata"]["uid"]
    started = time.monotonic()
    lab.wait(lambda: condition(kind, name, "AllInjected"), 90)
    lab.wait(
        lambda: metric("probe_success", "healthz") == 1
        and metric("probe_success", "readyz") == 0,
        60,
    )
    check("Partition leaves liveness healthy and breaks database readiness", True)
    event = fired("ChaosDatabaseUnavailable", after)
    capture(kind, name, "injected")
    lab.wait(lambda: condition(kind, name, "AllRecovered"), 120)
    lab.wait(healthy, 90)
    check(
        "Duration expiry recovers the link without deleting the experiment",
        lab.obj(lab.APP, kind, name)["metadata"]["uid"] == uid
        and condition(kind, name, "AllRecovered"),
    )
    resolved("ChaosDatabaseUnavailable", after, event)
    capture(kind, name, "recovered")
    check("Automatic network recovery preserves every row", rows() == baseline)
    REPORT["experiments"].append(
        {
            "name": name,
            "duration": "60s",
            "seconds_until_recovery_checked": round(time.monotonic() - started, 2),
            "recovery": "automatic duration expiry; object still present",
            "data_equal": rows() == baseline,
        }
    )
    finish(name)

    name = "04-api-failure"
    after = cursor()
    kind = start(name)
    lab.wait(lambda: condition(kind, name, "AllInjected"), 90)
    lab.wait(
        lambda: metric("probe_success", "healthz")
        == metric("probe_success", "readyz")
        == 0,
        60,
    )
    check("Pod failure makes both real HTTP probes fail", True)
    event = fired("ChaosApiUnavailable", after)
    capture(kind, name, "injected")
    finish(name)
    lab.wait(lambda: available("api", 2), 90)
    lab.wait(healthy, 90)
    resolved("ChaosApiUnavailable", after, event)
    expected = lab.IMAGES["api"]["image"]
    check(
        "Recovery restores the original pinned API image in every Pod",
        all(p["spec"]["containers"][0]["image"] == expected for p in pods("api")),
    )
    check("Complete API outage preserves all rows and timestamps", rows() == baseline)
    REPORT["experiments"].append(
        {
            "name": name,
            "recovery": "delete through normal finalizers",
            "data_equal": rows() == baseline,
        }
    )

    name = "05-database-pod-kill"
    before = uids("postgres")
    kind = start(name)
    lab.wait(lambda: available("postgres", 1) and uids("postgres") != before, 100)
    lab.wait(healthy, 120)
    check(
        "Chaos Mesh kills PostgreSQL and Kubernetes creates a new Pod",
        uids("postgres") != before,
    )
    check(
        "Database returns on the same PVC without losing committed data",
        lab.obj(lab.APP, "pvc", "postgres-data")["metadata"]["uid"] == pvc_uid
        and rows() == baseline,
    )
    capture(kind, name, "completed")
    finish(name)
    REPORT["experiments"].append(
        {"name": name, "pvc_uid": pvc_uid, "data_equal": rows() == baseline}
    )
    status, new_note = request(API, "/notes", {"title": marker + " — depois / after"})
    check(
        "Recovered database accepts a new API write with a fresh ID",
        status == 201 and new_note["id"] > max(row["id"] for row in baseline),
    )
    check(
        "Original marker remains visible through the recovered API",
        any(
            r["id"] == note["id"] and r["title"] == marker
            for r in request(API, "/notes")[1]["notes"]
        ),
    )
    stored = {e["id"] for e in events()}
    lab.k(lab.OBS, "delete", "pod", "-l", "app=receiver", "--timeout=60s", timeout=80)
    lab.k(
        lab.OBS,
        "rollout",
        "status",
        "deployment/receiver",
        "--timeout=100s",
        timeout=120,
    )
    check(
        "Firing and resolved webhooks survive a receiver Pod replacement",
        stored.issubset({e["id"] for e in events()}),
    )
    lab.wait(lambda: request(AM, "/api/v2/alerts")[1] == [], 100)
    check(
        "No active alerts remain after recovery", request(AM, "/api/v2/alerts")[1] == []
    )
    check(
        "Existing labs keep their workload specs, images and PVC identities",
        outside() == protected,
    )
    final_rows = rows()
    REPORT["final"] = {
        "api_replicas": 2,
        "postgres_replicas": 1,
        "rows": len(final_rows),
        "pvc_uid": pvc_uid,
        "row_sha256": hashlib.sha256(
            json.dumps(final_rows, sort_keys=True).encode()
        ).hexdigest(),
        "healthz_probe": metric("probe_success", "healthz"),
        "readyz_probe": metric("probe_success", "readyz"),
        "active_alerts": 0,
    }


def main():
    started = time.monotonic()
    with (lab.LOCAL / "operation.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for previous in (lab.ROOT / "evidence").glob("0*-*.json"):
            previous.unlink()
        try:
            verify()
            REPORT["passed"] = True
        except Exception as e:
            REPORT["passed"] = False
            REPORT["error"] = str(e)
            raise
        finally:
            errors = []
            for kind, name in list(ACTIVE):
                try:
                    finish(name)
                except Exception as e:
                    errors.append(str(e))
            REPORT["cleanup_errors"] = errors
            if errors:
                REPORT["passed"] = False
            REPORT["duration_seconds"] = round(time.monotonic() - started, 2)
            (lab.ROOT / "evidence/verification.json").write_text(
                json.dumps(REPORT, indent=2) + "\n"
            )
            if errors:
                raise RuntimeError(
                    "Fault cleanup failed; inspect the recorded resources before continuing"
                )


if __name__ == "__main__":
    main()
