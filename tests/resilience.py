#!/usr/bin/env python3
"""Paired real HTTP traffic during repeated Chaos Mesh removals; never discard failures."""

import argparse, copy, datetime, fcntl, hashlib, http.client, json, os, socket, subprocess, sys, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import lab, experiment, access
import verify as original

REPORT = {
    "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "checks": [],
    "trials": [],
    "image": lab.IMAGES["api"]["image"],
    "policy": access.POLICY,
    "drain_hook": {"type": "kubelet sleep", "seconds": 5},
}


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


def profile(drain):
    deploy = lab.obj(lab.APP, "deployment", "api")
    template = deploy["spec"]["template"]["spec"]
    container = template["containers"][0]
    env = [v for v in container.get("env", []) if v["name"] != "GUNICORN_CMD_ARGS"]
    if not drain:
        env.append({"name": "GUNICORN_CMD_ARGS", "$patch": "delete"})
    if drain:
        env.append({"name": "GUNICORN_CMD_ARGS", "value": "--graceful-timeout 20"})
    patch = {
        "spec": {
            "minReadySeconds": 2,
            "strategy": {
                "type": "RollingUpdate",
                "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0},
            },
            "template": {
                "spec": {
                    "terminationGracePeriodSeconds": 30,
                    "containers": [
                        {
                            "name": "api",
                            "lifecycle": (
                                {"preStop": {"sleep": {"seconds": 5}}}
                                if drain
                                else None
                            ),
                            "env": env,
                        }
                    ],
                }
            },
        }
    }
    lab.k(
        lab.APP, "patch", "deployment/api", "--type=strategic", "-p", json.dumps(patch)
    )
    lab.k(lab.APP, "rollout", "status", "deployment/api", "--timeout=140s", timeout=160)
    lab.wait(lambda: original.ready_pods("api", 2), 120)


def connection(host, port, method="GET", path="/healthz", body=None):
    started = time.monotonic()
    c = http.client.HTTPConnection(host, port, timeout=6)
    try:
        raw = None if body is None else json.dumps(body).encode()
        c.request(
            method,
            path,
            body=raw,
            headers={"Content-Type": "application/json", "Connection": "close"},
        )
        r = c.getresponse()
        data = r.read()
        return {
            "time": time.time(),
            "status": r.status,
            "latency_ms": round((time.monotonic() - started) * 1000, 3),
            "attempts": int(r.getheader("X-Lab-Upstream-Attempts", "1")),
            "transport_failures": int(r.getheader("X-Lab-Transport-Failures", "0")),
        }, data
    except (OSError, http.client.HTTPException) as e:
        return {
            "time": time.time(),
            "status": 0,
            "latency_ms": round((time.monotonic() - started) * 1000, 3),
            "attempts": 1,
            "transport_failures": 1,
            "error_type": type(e).__name__,
            "error": str(e)[:300],
        }, b""
    finally:
        c.close()


class Watches:
    def __init__(self):
        self.processes = []
        self.events = []
        self.threads = []

    def start(self, args, kind):
        command = [
            lab.K,
            "--kubeconfig",
            str(lab.CONFIG),
            "--context",
            lab.CONTEXT,
            "-n",
            lab.APP,
            *args,
        ]
        p = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.processes.append(p)

        def consume():
            buffer = ""
            for line in p.stdout:
                if kind == "logs":
                    if any(
                        key in line.lower()
                        for key in [
                            "handling signal",
                            "worker exiting",
                            "shutting down",
                        ]
                    ):
                        self.events.append(
                            {
                                "time": time.time(),
                                "kind": kind,
                                "line": line.strip()[:500],
                            }
                        )
                    continue
                buffer += line
                try:
                    value = json.loads(buffer)
                except json.JSONDecodeError:
                    continue
                buffer = ""
                o = value["object"]
                m = o.get("metadata", {})
                row = {
                    "time": time.time(),
                    "kind": kind,
                    "event": value.get("type"),
                    "name": m.get("name"),
                    "uid": m.get("uid"),
                    "resource_version": m.get("resourceVersion"),
                }
                if kind == "endpoints":
                    row["endpoints"] = [
                        {
                            "addresses": e.get("addresses"),
                            "conditions": e.get("conditions"),
                            "pod_uid": e.get("targetRef", {}).get("uid"),
                        }
                        for e in o.get("endpoints", [])
                    ]
                else:
                    row.update(
                        ip=o.get("status", {}).get("podIP"),
                        phase=o.get("status", {}).get("phase"),
                        deletion_timestamp=m.get("deletionTimestamp"),
                        deletion_grace_seconds=m.get("deletionGracePeriodSeconds"),
                        conditions=o.get("status", {}).get("conditions"),
                        containers=[
                            {"ready": c.get("ready"), "state": c.get("state")}
                            for c in o.get("status", {}).get("containerStatuses", [])
                        ],
                    )
                self.events.append(row)

        t = threading.Thread(target=consume, daemon=True)
        t.start()
        self.threads.append(t)

    def close(self):
        for p in self.processes:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        for t in self.threads:
            t.join(timeout=2)


def trial(drain, grace, number, proxy):
    name = "01-api-pod-kill" if grace == 0 else "06-api-graceful-removal"
    before = original.uids("api")
    w = Watches()
    w.start(
        [
            "get",
            "endpointslices",
            "-l",
            "kubernetes.io/service-name=api",
            "--watch",
            "--output-watch-events",
            "-o",
            "json",
        ],
        "endpoints",
    )
    w.start(
        [
            "get",
            "pods",
            "-l",
            "app=api",
            "--watch",
            "--output-watch-events",
            "-o",
            "json",
        ],
        "pods",
    )
    for pod in original.pods("api"):
        w.start(
            ["logs", "-f", pod["metadata"]["name"], "--since=1s", "--tail=0"], "logs"
        )
    samples = []
    stop = threading.Event()
    workers = []

    def traffic(lane, host, port):
        while not stop.is_set():
            started = time.monotonic()
            sample, _ = connection(host, port)
            sample["lane"] = lane
            samples.append(sample)
            stop.wait(max(0, 0.05 - (time.monotonic() - started)))

    for lane, host, port in [
        ("raw", access.HOST, 30434),
        ("get_retry", "127.0.0.1", proxy.server_port),
    ]:
        for _ in range(2):
            t = threading.Thread(target=traffic, args=(lane, host, port))
            t.start()
            workers.append(t)
    active = False
    record = {}
    try:
        time.sleep(1.5)
        injected = time.time()
        kind = experiment.apply(name)
        active = True
        lab.wait(
            lambda: original.ready_pods("api", 2) and original.uids("api") != before,
            120,
        )
        time.sleep(1.5)
        resource = lab.obj(lab.APP, kind, name)
        record = {
            "drain": drain,
            "chaos_grace_seconds": grace,
            "trial": number,
            "injected_at": injected,
            "experiment_uid": resource["metadata"]["uid"],
            "records": resource.get("status", {})
            .get("experiment", {})
            .get("containerRecords", []),
        }
    finally:
        stop.set()
        for t in workers:
            t.join(timeout=8)
        w.close()
        if active:
            experiment.remove(name)
    record.update(samples=samples, timeline=sorted(w.events, key=lambda e: e["time"]))
    for lane in ["raw", "get_retry"]:
        chosen = [s for s in samples if s["lane"] == lane]
        failed = [s for s in chosen if s["status"] != 200]
        latencies = sorted(s["latency_ms"] for s in chosen)
        record[lane] = {
            "requests": len(chosen),
            "failures": len(failed),
            "failure_rate": len(failed) / len(chosen) if chosen else None,
            "attempts": sum(s["attempts"] for s in chosen),
            "transport_failures": sum(s["transport_failures"] for s in chosen),
            "p95_ms": (
                latencies[int(0.95 * (len(latencies) - 1))] if latencies else None
            ),
        }
    REPORT["trials"].append(record)
    print(
        json.dumps(
            {
                k: record[k]
                for k in ["drain", "chaos_grace_seconds", "trial", "raw", "get_retry"]
            }
        ),
        flush=True,
    )
    check(
        f"Pod replacement completes: drain={drain}, grace={grace}, trial={number}",
        original.ready_pods("api", 2) and before != original.uids("api"),
    )


def policy_test():
    counts = {"GET": 0, "POST": 0}
    marker = "resilience-policy-" + uuid.uuid4().hex

    class DroppingOrigin(BaseHTTPRequestHandler):
        def do_GET(self):
            counts["GET"] += 1
            status, body = original.request(original.API, "/healthz")
            if counts["GET"] <= 2:
                self.close_connection = True
                return
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            counts["POST"] += 1
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            original.request(original.API, "/notes", {"title": marker})
            # Actual API commit acknowledged to this intermediary; drop the client response.
            self.close_connection = True

        def log_message(self, *args):
            pass

    origin = ThreadingHTTPServer(("127.0.0.1", 0), DroppingOrigin)
    threading.Thread(target=origin.serve_forever, daemon=True).start()
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        access.handler(origin.server_port, host="127.0.0.1", quiet=True, api=True),
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        get, _ = connection("127.0.0.1", server.server_port)
        check(
            "GET transport failures are retried at most three times and remain visible",
            get["status"] == 200
            and get["attempts"] == 3
            and get["transport_failures"] == 2
            and counts["GET"] == 3,
            get,
        )
        post, _ = connection(
            "127.0.0.1", server.server_port, "POST", "/notes", {"title": marker}
        )
        found = int(
            lab.sql("SELECT count(*) FROM notes WHERE title='" + marker + "';").strip()
        )
        check(
            "A committed POST with a lost response is not retried",
            post["status"] == 502
            and post["attempts"] == 1
            and counts["POST"] == 1
            and found == 1,
            {
                "status": post["status"],
                "attempts": post["attempts"],
                "committed_rows": found,
            },
        )
        REPORT["policy_test"] = {
            "get": get,
            "post": post,
            "origin_counts": counts,
            "committed_rows": found,
        }
    finally:
        server.shutdown()
        server.server_close()
        origin.shutdown()
        origin.server_close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--rounds", type=int, default=4)
    a = p.parse_args()
    if not 2 <= a.rounds <= 8:
        raise SystemExit("Use 2–8 rounds per scenario")
    REPORT["rounds_per_scenario"] = a.rounds
    start = time.monotonic()
    proxy = None
    protected = None
    baseline = None
    with (lab.LOCAL / "operation.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            lab.guard(lab.APP)
            for kind in ["podchaos", "networkchaos"]:
                check(
                    "No existing " + kind + " is replaced",
                    not json.loads(lab.k(lab.APP, "get", kind, "-o", "json").stdout)[
                        "items"
                    ],
                )
            protected = original.outside()
            baseline = original.rows()
            pvc = lab.obj(lab.APP, "pvc", "postgres-data")["metadata"]["uid"]
            (lab.LOCAL / "resilience-baseline-private.json").write_text(
                json.dumps(
                    {
                        "rows": baseline,
                        "deployment": lab.obj(lab.APP, "deployment", "api"),
                    },
                    indent=2,
                )
                + "\n"
            )
            proxy = ThreadingHTTPServer(
                ("127.0.0.1", 0), access.handler(30434, quiet=True)
            )
            threading.Thread(target=proxy.serve_forever, daemon=True).start()
            for drain in [False, True]:
                profile(drain)
                for grace in [0, 30]:
                    for i in range(1, a.rounds + 1):
                        trial(drain, grace, i, proxy)
            check(
                "All original rows and timestamps survive every removal",
                original.rows() == baseline,
            )
            check(
                "Database keeps the same PVC",
                lab.obj(lab.APP, "pvc", "postgres-data")["metadata"]["uid"] == pvc,
            )
            check(
                "Other labs keep their workloads, images and PVC identities",
                original.outside() == protected,
            )
            policy_test()
            REPORT["summary"] = []
            for drain in [False, True]:
                for grace in [0, 30]:
                    for lane in ["raw", "get_retry"]:
                        rows = [
                            t[lane]
                            for t in REPORT["trials"]
                            if t["drain"] == drain and t["chaos_grace_seconds"] == grace
                        ]
                        count = sum(t["requests"] for t in rows)
                        errors = sum(t["failures"] for t in rows)
                        REPORT["summary"].append(
                            {
                                "drain": drain,
                                "grace_seconds": grace,
                                "lane": lane,
                                "requests": count,
                                "failures": errors,
                                "failure_rate": errors / count,
                                "attempts": sum(t["attempts"] for t in rows),
                                "transport_failures": sum(
                                    t["transport_failures"] for t in rows
                                ),
                            }
                        )
            REPORT["passed"] = True
        except Exception as e:
            REPORT["passed"] = False
            REPORT["error"] = str(e)
            raise
        finally:
            try:
                profile(True)
                REPORT["final_ready"] = original.ready_pods("api", 2)
            except Exception as e:
                REPORT["passed"] = False
                REPORT["recovery_error"] = str(e)
            if proxy:
                proxy.shutdown()
                proxy.server_close()
            REPORT["duration_seconds"] = round(time.monotonic() - start, 2)
            (lab.ROOT / "evidence/resilience.json").write_text(
                json.dumps(REPORT, indent=2) + "\n"
            )
            if not REPORT.get("final_ready"):
                raise RuntimeError("API recovery needs attention")


if __name__ == "__main__":
    main()
