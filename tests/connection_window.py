#!/usr/bin/env python3
"""Inspect real replica removals with phase timing and node conntrack metadata."""

import argparse, datetime, fcntl, http.client, json, socket, subprocess, sys, threading, time, uuid
from collections import Counter
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import lab, access, experiment, packets
import verify as original
import resilience

REPORT = {
    "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "checks": [],
    "trials": [],
    "policy": access.POLICY,
}


def check(name, passed, detail=None):
    REPORT["checks"].append(
        {
            "name": name,
            "passed": bool(passed),
            **({"detail": detail} if detail is not None else {}),
        }
    )
    print(("PASS " if passed else "FAIL ") + name, flush=True)
    if not passed:
        raise AssertionError(name)


def raw_request(host, port):
    start = time.monotonic()
    phase = "connect"
    connected = None
    source = None
    c = http.client.HTTPConnection(host, port, timeout=6)
    # Retain the socket before connect so even a SYN timeout has a source port.
    c.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    c.sock.settimeout(6)
    c.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    try:
        c.sock.connect((host, port))
        connected = time.monotonic()
        source = c.sock.getsockname()
        phase = "send"
        c.request("GET", "/healthz", headers={"Connection": "close"})
        phase = "headers"
        r = c.getresponse()
        status = r.status
        phase = "body"
        r.read()
        row = {"status": status}
    except (OSError, http.client.HTTPException) as e:
        row = {
            "status": 0,
            "error_type": type(e).__name__,
            "error": str(e)[:300],
            "phase": phase,
        }
    finally:
        if c.sock:
            source = c.sock.getsockname()
        c.close()
    return {
        **row,
        "started_at": time.time() - (time.monotonic() - start),
        "time": time.time(),
        "latency_ms": round((time.monotonic() - start) * 1000, 3),
        "connect_ms": round((connected - start) * 1000, 3) if connected else None,
        "source": source,
        "attempts": 1,
        "transport_failures": int(row["status"] == 0),
    }


def trial(grace, number, servers):
    name = "06-api-graceful-removal" if grace else "01-api-pod-kill"
    before = original.uids("api")
    watches = resilience.Watches()
    watches.start(
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
    watches.start(
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
        watches.start(
            ["logs", "-f", pod["metadata"]["name"], "--since=1s", "--tail=0"], "logs"
        )
    ctpath = lab.LOCAL / f"conntrack-{grace}-{number}.txt"
    ctfile = ctpath.open("w")
    pidfile = "/tmp/chaos-lab-conntrack-" + uuid.uuid4().hex + ".pid"
    ct = subprocess.Popen(
        [
            "docker",
            "exec",
            "devops-notes-control-plane",
            "sh",
            "-c",
            f"echo $$ > {pidfile}; exec conntrack -E -p tcp --dport 30434 -o timestamp,extended",
        ],
        stdout=ctfile,
        stderr=subprocess.STDOUT,
        text=True,
    )
    capture = (
        packets.Capture(f"removal-{grace}-{number}")
        if REPORT.get("capture_packets")
        else None
    )
    stop = threading.Event()
    samples = []
    threads = []
    active = False
    injected = None
    recovered = None
    resource = {}

    def worker(lane, port):
        while not stop.is_set():
            started = time.monotonic()
            if lane == "direct":
                sample = raw_request(access.HOST, 30434)
            else:
                sample, _ = resilience.connection("127.0.0.1", port)
            sample["lane"] = lane
            samples.append(sample)
            stop.wait(max(0, 0.05 - (time.monotonic() - started)))

    try:
        if capture:
            capture.start()
        for lane, port in [
            ("direct", 30434),
            *[(name, s.server_port) for name, s in servers.items()],
        ]:
            for _ in range(2):
                t = threading.Thread(target=worker, args=(lane, port))
                t.start()
                threads.append(t)
        time.sleep(1.5)
        injected = time.time()
        kind = experiment.apply(name)
        active = True
        lab.wait(
            lambda: original.ready_pods("api", 2) and original.uids("api") != before,
            120,
        )
        recovered = time.time()
        time.sleep(2)
        resource = lab.obj(lab.APP, kind, name)
    finally:
        cleanup_errors = []
        stop.set()
        for t in threads:
            t.join(timeout=8)
        try:
            if capture:
                capture.stop()
        except Exception as error:
            cleanup_errors.append("packet capture: " + str(error))
        try:
            watches.close()
        except Exception as error:
            cleanup_errors.append("watch processes: " + str(error))
        try:
            lab.run(
                [
                    "docker",
                    "exec",
                    "devops-notes-control-plane",
                    "sh",
                    "-c",
                    f'if [ -f {pidfile} ]; then kill -INT "$(cat {pidfile})"; rm -f {pidfile}; fi',
                ],
                ok=False,
            )
        except Exception as error:
            cleanup_errors.append("conntrack observer: " + str(error))
        try:
            ct.terminate()
            ct.wait(timeout=4)
        except subprocess.TimeoutExpired:
            ct.kill()
            ct.wait()
        finally:
            ctfile.close()
        if active:
            try:
                experiment.remove(name)
            except Exception as error:
                cleanup_errors.append("Chaos Mesh experiment: " + str(error))
        if cleanup_errors:
            REPORT.setdefault("cleanup_errors", []).extend(cleanup_errors)
            raise RuntimeError("Trial cleanup failed: " + "; ".join(cleanup_errors))
    failed = [s for s in samples if s["status"] != 200]
    ports = {str(s["source"][1]) for s in failed if s.get("source")}
    lines = ctpath.read_text().splitlines()
    related = [line for line in lines if any("sport=" + p + " " in line for p in ports)]
    packet_report = None
    if capture:
        index, stats = capture.index()
        selected = [
            s
            for s in samples
            if s["lane"] == "direct" and (s["status"] != 200 or s["latency_ms"] >= 500)
        ]
        healthy = next(
            (
                s
                for s in samples
                if s["lane"] == "direct"
                and s["status"] == 200
                and injected <= s["started_at"] <= recovered
            ),
            None,
        )
        if healthy:
            selected.append(healthy)
        packet_report = {
            "captures": stats,
            "event_window": {"injected_at": injected, "recovered_at": recovered},
            "flows": [
                {"sample": s, "tcp": packets.correlate(s, access.HOST, 30434, index)}
                for s in selected
            ],
        }
        check(
            f"Packet capture is readable, header-only and has no reported capture drops: grace={grace}, trial={number}",
            bool(stats)
            and any(r["packets"] > 0 for r in stats)
            and all(
                r["dropped_by_capture_kernel"] == 0 and r["exit_code"] == 0
                for r in stats
            ),
        )
    totals = {}
    for lane in ["direct", *servers]:
        rows = [s for s in samples if s["lane"] == lane]
        lat = sorted(s["latency_ms"] for s in rows)
        totals[lane] = {
            "requests": len(rows),
            "failures": sum(s["status"] != 200 for s in rows),
            "attempts": sum(s["attempts"] for s in rows),
            "transport_failures": sum(s["transport_failures"] for s in rows),
            "p95_ms": lat[int(0.95 * (len(lat) - 1))],
            "max_ms": max(lat),
        }
    REPORT["trials"].append(
        {
            "grace_seconds": grace,
            "trial": number,
            "injected_at": injected,
            "samples": samples,
            "totals": totals,
            "timeline": sorted(watches.events, key=lambda e: e["time"]),
            "conntrack_for_failed_direct_requests": related,
            "conntrack_capture": {
                "lines": len(lines),
                "first_line": lines[0] if lines else "",
                "note": "NodePort metadata only; retained tuples match failed direct source ports; no payload",
            },
            "packet_capture": packet_report,
            "experiment_uid": resource.get("metadata", {}).get("uid"),
        }
    )
    print(json.dumps({"grace": grace, "trial": number, "totals": totals}), flush=True)
    check(
        f"Replica recovers after grace={grace}, trial={number}",
        original.ready_pods("api", 2) and original.uids("api") != before,
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--rounds", type=int, default=8)
    p.add_argument("--capture-packets", action="store_true")
    args = p.parse_args()
    REPORT["capture_packets"] = args.capture_packets
    if not 4 <= args.rounds <= 12:
        raise SystemExit("Use 4–12 graceful rounds; half as many abrupt controls")
    start = time.monotonic()
    servers = {}
    observed = {"previous": [], "short_connect": []}
    with (lab.LOCAL / "operation.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            lab.guard(lab.APP)
            for kind in ["podchaos", "networkchaos"]:
                check(
                    "No existing " + kind,
                    not json.loads(lab.k(lab.APP, "get", kind, "-o", "json").stdout)[
                        "items"
                    ],
                )
            baseline = original.rows()
            outside = original.outside()
            pvc = lab.obj(lab.APP, "pvc", "postgres-data")["metadata"]["uid"]
            resilience.profile(True)
            for name, budget in [
                ("previous", 3),
                ("short_connect", access.POLICY["get_connect_timeout_seconds"]),
            ]:
                s = ThreadingHTTPServer(
                    ("127.0.0.1", 0),
                    access.handler(
                        30434,
                        quiet=True,
                        connect_timeout=budget,
                        observer=observed[name].append,
                    ),
                )
                servers[name] = s
                threading.Thread(target=s.serve_forever, daemon=True).start()
            # Alternate graceful removals with abrupt controls rather than grouping profiles.
            for i in range(1, args.rounds + 1):
                trial(30, i, servers)
                if i % 2 == 0:
                    trial(0, i // 2, servers)
            check(
                "All original rows and timestamps survive", baseline == original.rows()
            )
            check(
                "PostgreSQL keeps its PVC",
                pvc == lab.obj(lab.APP, "pvc", "postgres-data")["metadata"]["uid"],
            )
            check(
                "Other labs retain their workload specs, images and PVCs",
                outside == original.outside(),
            )
            REPORT["gateway_events"] = observed
            REPORT["summary"] = []
            for grace in [30, 0]:
                for lane in ["direct", *servers]:
                    totals = [
                        t["totals"][lane]
                        for t in REPORT["trials"]
                        if t["grace_seconds"] == grace
                    ]
                    REPORT["summary"].append(
                        {
                            "grace_seconds": grace,
                            "lane": lane,
                            **{
                                key: sum(t[key] for t in totals)
                                for key in [
                                    "requests",
                                    "failures",
                                    "attempts",
                                    "transport_failures",
                                ]
                            },
                        }
                    )
            REPORT["passed"] = True
        except BaseException as e:
            REPORT["passed"] = False
            REPORT["error"] = str(e)
            raise
        finally:
            for s in servers.values():
                s.shutdown()
                s.server_close()
            try:
                lab.wait(lambda: original.ready_pods("api", 2), 120)
                REPORT["final_ready"] = True
            except Exception as recovery:
                REPORT["final_ready"] = False
                REPORT["recovery_error"] = str(recovery)
            REPORT["duration_seconds"] = round(time.monotonic() - start, 2)
            (lab.ROOT / "evidence/connection-window.json").write_text(
                json.dumps(REPORT, indent=2) + "\n"
            )
            if not REPORT["final_ready"]:
                REPORT["passed"] = False
                raise RuntimeError("API recovery needs attention")


if __name__ == "__main__":
    main()
