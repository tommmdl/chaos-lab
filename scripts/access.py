#!/usr/bin/env python3
"""Local access with bounded, visible transport retries for API GET only."""

import http.client
import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

POLICY = json.loads(
    (Path(__file__).resolve().parents[1] / "config/api-resilience.json").read_text()
)
HOST = subprocess.check_output(
    [
        "docker",
        "inspect",
        "-f",
        '{{(index .NetworkSettings.Networks "kind").IPAddress}}',
        "devops-notes-control-plane",
    ],
    text=True,
).strip()


def upstream(
    method,
    path,
    data,
    headers,
    host,
    port,
    get_attempts=None,
    api=None,
    connect_timeout=None,
):
    retryable = method == "GET" and (port == 30434 if api is None else api)
    maximum = (
        (POLICY["get_max_attempts"] if get_attempts is None else get_attempts)
        if retryable
        else 1
    )
    started = time.monotonic()
    failures = []
    for attempt in range(1, maximum + 1):
        connection = None
        remaining = POLICY["get_total_budget_seconds"] - (time.monotonic() - started)
        if attempt > 1 and remaining <= 0:
            break
        timeout = (
            min(POLICY["get_attempt_timeout_seconds"], max(0.01, remaining))
            if retryable
            else 12
        )
        phase = "connect"
        attempt_started = time.monotonic()
        try:
            dial_timeout = (
                min(
                    timeout,
                    (
                        POLICY["get_connect_timeout_seconds"]
                        if connect_timeout is None
                        else connect_timeout
                    ),
                )
                if retryable
                else timeout
            )
            connection = http.client.HTTPConnection(host, port, timeout=dial_timeout)
            connection.connect()
            # Establishment gets a short budget; an established GET keeps its read budget.
            if retryable:
                remaining = POLICY["get_total_budget_seconds"] - (
                    time.monotonic() - started
                )
                if remaining <= 0:
                    raise TimeoutError("GET budget exhausted after connect")
                connection.sock.settimeout(
                    min(POLICY["get_attempt_timeout_seconds"], remaining)
                )
            phase = "send"
            connection.request(method, path, body=data, headers=headers)
            phase = "headers"
            response = connection.getresponse()
            phase = "body"
            body = response.read()
            # An HTTP response, including 4xx/5xx, is returned without another attempt.
            return response.status, response.getheaders(), body, attempt, failures
        except (OSError, http.client.HTTPException) as error:
            failures.append(
                {
                    "type": type(error).__name__,
                    "message": str(error)[:300],
                    "phase": phase,
                    "elapsed_ms": round((time.monotonic() - attempt_started) * 1000, 3),
                }
            )
            if attempt == maximum:
                break
            delay = POLICY["get_retry_delays_seconds"][attempt - 1]
            if time.monotonic() - started + delay >= POLICY["get_total_budget_seconds"]:
                break
            time.sleep(delay)
        finally:
            if connection:
                connection.close()
    return (
        502,
        [("Content-Type", "application/json")],
        json.dumps(
            {
                "error": "upstream_unavailable",
                "write_outcome": (
                    "unknown; reconcile before retrying"
                    if method != "GET"
                    else "not applicable"
                ),
            }
        ).encode(),
        len(failures),
        failures,
    )


def handler(
    port,
    host=None,
    get_attempts=None,
    observer=None,
    quiet=False,
    api=None,
    connect_timeout=None,
):
    class Proxy(BaseHTTPRequestHandler):
        def request(self):
            data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            headers = {
                k: v
                for k, v in self.headers.items()
                if k.lower() not in {"host", "connection", "transfer-encoding"}
            }
            status, returned, body, attempts, failures = upstream(
                self.command,
                self.path,
                data,
                headers,
                host or HOST,
                port,
                get_attempts,
                api,
                connect_timeout,
            )
            if failures and not quiet:
                print(
                    json.dumps(
                        {
                            "event": "upstream_transport_failure",
                            "method": self.command,
                            "path": self.path.split("?")[0],
                            "attempts": attempts,
                            "failures": failures,
                        }
                    ),
                    flush=True,
                )
            if observer:
                observer(
                    {
                        "method": self.command,
                        "status": status,
                        "attempts": attempts,
                        "transport_failures": failures,
                    }
                )
            self.send_response(status)
            for key, value in returned:
                if key.lower() not in {
                    "content-length",
                    "transfer-encoding",
                    "connection",
                    "server",
                    "date",
                    "x-lab-upstream-attempts",
                    "x-lab-transport-failures",
                }:
                    self.send_header(key, value)
            self.send_header("X-Lab-Upstream-Attempts", str(attempts))
            self.send_header("X-Lab-Transport-Failures", str(len(failures)))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        do_GET = do_POST = do_DELETE = request

        def log_message(self, format, *args):
            if not quiet:
                super().log_message(format, *args)

    return Proxy


if __name__ == "__main__":
    servers = []
    try:
        for local, remote in [
            (8024, 30434),
            (8025, 30435),
            (8026, 30436),
            (8027, 30437),
        ]:
            server = ThreadingHTTPServer(("127.0.0.1", local), handler(remote))
            servers.append(server)
            threading.Thread(target=server.serve_forever, daemon=True).start()
        print(
            "App 8024 / Prometheus 8025 / Alertmanager 8026 / receiver 8027", flush=True
        )
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
