#!/usr/bin/env python3
"""Real HTTP boundaries: exhausted GET retries, HTTP errors and non-GET methods."""

import json, sys, threading, time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import access

counts = Counter()
checks = []


class Origin(BaseHTTPRequestHandler):
    def do_GET(self):
        counts[self.path] += 1
        if self.path == "/unavailable":
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/slow":
            time.sleep(5)
            self.close_connection = True
        else:
            self.close_connection = True

    def do_DELETE(self):
        counts["DELETE"] += 1
        self.close_connection = True

    def log_message(self, *args):
        pass


def check(name, value, detail):
    checks.append({"name": name, "passed": bool(value), "detail": detail})
    print(("PASS " if value else "FAIL ") + name, flush=True)
    if not value:
        raise AssertionError(name)


def main():
    origin = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    threading.Thread(target=origin.serve_forever, daemon=True).start()
    report = {"checks": checks}
    try:

        def call(method, path):
            return access.upstream(
                method, path, b"", {}, "127.0.0.1", origin.server_port, api=True
            )

        status, headers, body, attempts, failures = call("GET", "/drop")
        check(
            "GET transport retries stop after three attempts",
            status == 502 and attempts == counts["/drop"] == 3 and len(failures) == 3,
            {"status": status, "attempts": attempts, "errors": failures},
        )
        status, _, _, attempts, failures = call("GET", "/unavailable")
        check(
            "An actual HTTP 503 is returned without retry",
            status == 503 and attempts == counts["/unavailable"] == 1 and not failures,
            {"status": status, "attempts": attempts},
        )
        status, _, _, attempts, failures = call("DELETE", "/drop")
        check(
            "DELETE transport failure is never retried",
            status == 502 and attempts == counts["DELETE"] == 1,
            {"status": status, "attempts": attempts},
        )
        start = time.monotonic()
        status, _, _, attempts, failures = call("GET", "/slow")
        elapsed = time.monotonic() - start
        check(
            "Slow GET response exhausts the retry budget without unbounded waiting",
            status == 502 and elapsed < 4.6 and attempts <= 3,
            {
                "status": status,
                "attempts": attempts,
                "elapsed_seconds": round(elapsed, 3),
            },
        )
        report["passed"] = True
    except Exception as error:
        report["passed"] = False
        report["error"] = str(error)
        raise
    finally:
        origin.shutdown()
        origin.server_close()
        (
            Path(__file__).resolve().parents[1] / "evidence/access-policy.json"
        ).write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
