#!/usr/bin/env python3
"""Exercise actual TCP connect timeouts using a scoped DROP on an unused node port."""

import datetime, json, socket, sys, uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import lab, access

report = {
    "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "checks": [],
}


def check(name, value, detail=None):
    report["checks"].append({"name": name, "passed": bool(value), "detail": detail})
    print(("PASS " if value else "FAIL ") + name, flush=True)
    if not value:
        raise AssertionError(name)


def main():
    node = "devops-notes-control-plane"
    label = "chaos-lab-connect-" + uuid.uuid4().hex[:10]
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.connect((access.HOST, 9))
    source = udp.getsockname()[0]
    udp.close()
    # Check refusal before adding the rule: never intercept a listening service.
    port = None
    for candidate in range(18471, 18491):
        sock = socket.socket()
        sock.settimeout(1)
        try:
            sock.connect((access.HOST, candidate))
        except ConnectionRefusedError:
            port = candidate
            break
        finally:
            sock.close()
    if port is None:
        raise RuntimeError("No confirmed unused fixture port")
    rule = [
        "INPUT",
        "-s",
        source,
        "-d",
        access.HOST,
        "-p",
        "tcp",
        "--dport",
        str(port),
        "--syn",
        "-m",
        "comment",
        "--comment",
        label,
        "-j",
        "DROP",
    ]
    command = ["docker", "exec", node, "iptables"]
    active = False
    try:
        lab.run([*command, "-I", rule[0], "1", *rule[1:]])
        active = True
        results = {}
        for name, budget in [
            ("previous", 3),
            ("short_connect", access.POLICY["get_connect_timeout_seconds"]),
        ]:
            status, _, _, attempts, failures = access.upstream(
                "GET",
                "/healthz",
                b"",
                {},
                access.HOST,
                port,
                api=True,
                connect_timeout=budget,
            )
            elapsed = sum(e["elapsed_ms"] for e in failures)
            results[name] = {
                "status": status,
                "attempts": attempts,
                "failures": failures,
                "attempt_elapsed_ms": round(elapsed, 3),
            }
        check(
            "Dropped SYNs are classified as connect failures for both policies",
            all(
                r["status"] == 502
                and all(
                    e["phase"] == "connect" and e["type"] == "TimeoutError"
                    for e in r["failures"]
                )
                for r in results.values()
            ),
            results,
        )
        check(
            "Short connect leaves time for all three bounded attempts",
            results["short_connect"]["attempts"] == 3
            and results["short_connect"]["attempt_elapsed_ms"] < 2000
            and results["previous"]["attempt_elapsed_ms"] > 3500,
            results,
        )
        report["passed"] = True
    except BaseException as error:
        report["passed"] = False
        report["error"] = str(error)
        raise
    finally:
        if active:
            lab.run([*command, "-D", *rule])
        absent = lab.run([*command, "-C", *rule], ok=False).returncode != 0
        report["rule_removed"] = absent
        check("Scoped SYN-drop rule removed", absent, {"port": port, "comment": label})
        (lab.ROOT / "evidence/syn-budget.json").write_text(
            json.dumps(report, indent=2) + "\n"
        )


if __name__ == "__main__":
    main()
