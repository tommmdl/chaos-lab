#!/usr/bin/env python3
"""Local browser access that survives replacement of application Pods."""

import http.client, json, subprocess, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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


def handler(port):
    class Proxy(BaseHTTPRequestHandler):
        def request(self):
            connection = None
            try:
                data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                connection = http.client.HTTPConnection(HOST, port, timeout=12)
                headers = {
                    k: v
                    for k, v in self.headers.items()
                    if k.lower() not in {"host", "connection", "transfer-encoding"}
                }
                connection.request(self.command, self.path, body=data, headers=headers)
                upstream = connection.getresponse()
                body = upstream.read()
                self.send_response(upstream.status)
                for key, value in upstream.getheaders():
                    if key.lower() not in {
                        "content-length",
                        "transfer-encoding",
                        "connection",
                        "server",
                        "date",
                    }:
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                self.send_error(
                    502, "Lab upstream unavailable; failed writes are never retried"
                )
            finally:
                if connection:
                    connection.close()

        do_GET = do_POST = do_DELETE = request

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
