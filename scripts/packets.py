#!/usr/bin/env python3
"""Header-only Linux SLL2 PCAP capture and conservative TCP flow correlation."""

import gzip, hashlib, json, os, re, shlex, socket, struct, subprocess, time, uuid
from collections import defaultdict
from pathlib import Path
import lab

DIRECTORY = lab.LOCAL / "packets"
ARCHIVE = lab.ROOT / "evidence/connection-packets"


def privileged():
    if os.geteuid() == 0:
        return []
    if os.environ.get("PACKET_PRIVILEGE") == "wsl":
        return ["wsl.exe", "-u", "root", "--"]
    return ["sudo", "-n"]


class Capture:
    def __init__(self, name, scope="nodes", port=None):
        self.name = name
        self.scope = scope
        self.port = port
        self.handles = []
        self.records = []
        self.version = None

    def start(self):
        DIRECTORY.mkdir(exist_ok=True)
        self.version = subprocess.run(
            [*privileged(), "tcpdump", "--version"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()[0]
        if self.scope == "host":
            nodes = [("runner", None)]
        else:
            names = lab.run(
                [
                    "docker",
                    "ps",
                    "--filter",
                    "label=io.x-k8s.kind.cluster=devops-notes",
                    "--format",
                    "{{.Names}}",
                ]
            ).stdout.splitlines()
            if not names:
                raise RuntimeError("No lab nodes for packet capture")
            nodes = [
                (
                    n,
                    int(
                        lab.run(
                            ["docker", "inspect", "-f", "{{.State.Pid}}", n]
                        ).stdout.strip()
                    ),
                )
                for n in names
            ]
        try:
            for node, pid in nodes:
                stem = f"{self.name}-{node}"
                path = DIRECTORY / (stem + ".pcap")
                logpath = DIRECTORY / (stem + ".log")
                pidpath = DIRECTORY / (stem + ".pid")
                # SLL2 is 20 bytes; IPv4/TCP minimum headers are 20+20. No HTTP bytes fit.
                if self.port:
                    expression = f"ip and tcp and port {int(self.port)}"
                elif (
                    os.environ.get("CI") == "true"
                    and (lab.LOCAL / "ci-cluster-owned").exists()
                ):
                    # The test's direct lane always enters through this owned NodePort.
                    # Excluding app-side probes and unrelated pod traffic keeps drops measurable.
                    expression = "ip and tcp and port 30434"
                else:
                    # The measured flow is the direct NodePort lane. Capture only that
                    # boundary; pod-side port 8000 includes unrelated probes and scrapes.
                    expression = "ip and tcp and port 30434"
                command = (["nsenter", "-t", str(pid), "-n"] if pid else []) + [
                    "tcpdump",
                    "-i",
                    "any",
                    "-y",
                    "LINUX_SLL2",
                    "-nn",
                    "-s",
                    "60",
                    "-B",
                    "16384",
                    "-U",
                    "-Z",
                    "root",
                    "-w",
                    str(path.resolve()),
                    expression,
                ]
                shell = (
                    "echo $$ > "
                    + shlex.quote(str(pidpath.resolve()))
                    + "; exec "
                    + shlex.join(command)
                )
                log = logpath.open("w")
                process = subprocess.Popen(
                    [*privileged(), "sh", "-c", shell],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
                handle = {
                    "node": node,
                    "path": path,
                    "logpath": logpath,
                    "pidpath": pidpath,
                    "log": log,
                    "process": process,
                    "filter": expression,
                }
                self.handles.append(handle)
                deadline = time.monotonic() + 15
                while "listening on" not in logpath.read_text():
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError(
                            "tcpdump did not start: " + logpath.read_text()[-1200:]
                        )
                    time.sleep(0.1)
        except BaseException:
            self.stop()
            raise
        return self

    def stop(self):
        errors = []
        for h in self.handles:
            p = h["process"]
            try:
                if p.poll() is None:
                    pid = int(h["pidpath"].read_text().strip())
                    cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
                    if (
                        b"tcpdump" not in cmdline
                        or str(h["path"].resolve()).encode() not in cmdline
                    ):
                        raise RuntimeError("Capture PID no longer belongs to this file")
                    lab.run([*privileged(), "kill", "-INT", str(pid)], timeout=10)
                    p.wait(timeout=15)
            except Exception as error:
                errors.append(str(error))
                try:
                    if p.poll() is None:
                        p.terminate()
                        p.wait(timeout=5)
                except Exception as nested:
                    errors.append(str(nested))
            finally:
                h["log"].close()
            try:
                text = h["logpath"].read_text()
                drop = re.search(r"(\d+) packets dropped by kernel", text)
                self.records.append(
                    {
                        "node": h["node"],
                        "path": str(h["path"].relative_to(lab.ROOT)),
                        "sha256": (
                            hashlib.sha256(h["path"].read_bytes()).hexdigest()
                            if h["path"].exists()
                            else None
                        ),
                        "filter": h["filter"],
                        "snaplen": 60,
                        "datalink": "LINUX_SLL2",
                        "dropped_by_capture_kernel": int(drop.group(1)) if drop else None,
                        "exit_code": p.poll(),
                        "capture_log": text,
                        "tcpdump_version": self.version,
                    }
                )
            except Exception as error:
                errors.append(str(error))
            # A leftover PID file is harmless; do not unlink root-owned files from WSL unnecessarily.
        self.handles = []
        if errors:
            raise RuntimeError("; ".join(errors))
        return self.records

    def index(self):
        index = defaultdict(list)
        stats = []
        for record in self.records:
            packets, meta = read(lab.ROOT / record["path"])
            stats.append({**record, **meta})
            for p in packets:
                index[(p["src"], p["sport"], p["dst"], p["dport"])].append(p)
        return index, stats


def read(path):
    data = Path(path).read_bytes()
    if len(data) < 24:
        raise ValueError("Missing PCAP global header")
    if data[:4] == b"\xd4\xc3\xb2\xa1":
        endian = "<"
        scale = 1e6
    elif data[:4] == b"\xa1\xb2\xc3\xd4":
        endian = ">"
        scale = 1e6
    elif data[:4] == b"\x4d\x3c\xb2\xa1":
        endian = "<"
        scale = 1e9
    else:
        raise ValueError("Unsupported PCAP format")
    _, _, _, _, _, snap, link = struct.unpack(endian + "IHHIIII", data[:24])
    if link != 276 or snap != 60:
        raise ValueError("Expected Linux SLL2 with snaplen 60")
    rows = []
    offset = 24
    truncated_tcp = 0
    count = 0
    while offset < len(data):
        if offset + 16 > len(data):
            raise ValueError("Truncated PCAP record")
        sec, fraction, size, wire = struct.unpack_from(endian + "IIII", data, offset)
        offset += 16
        frame = data[offset : offset + size]
        offset += size
        count += 1
        if len(frame) != size or size > 60:
            raise ValueError("Invalid capture length")
        if size < 40 or frame[:2] != b"\x08\x00":
            raise ValueError("Non-IPv4 packet in scoped capture")
        ip = frame[20:]
        ihl = (ip[0] & 15) * 4
        if ip[0] >> 4 != 4 or ip[9] != 6:
            raise ValueError("Non-TCP packet in capture")
        fragment = struct.unpack_from("!H", ip, 6)[0]
        if fragment & 0x1FFF or len(ip) < ihl + 14:
            truncated_tcp += 1
            continue
        tcp = ip[ihl:]
        header = (tcp[12] >> 4) * 4
        if ihl < 20 or header < 20:
            raise ValueError("Invalid IP/TCP header")
        if len(frame) > 20 + ihl + header:
            raise ValueError("HTTP payload present in capture")
        sport, dport, seq, ack = struct.unpack_from("!HHII", tcp)
        rows.append(
            {
                "time": sec + fraction / scale,
                "node": Path(path).stem,
                "interface_index": struct.unpack_from("!I", frame, 4)[0],
                "packet_type": frame[10],
                "src": socket.inet_ntoa(ip[12:16]),
                "dst": socket.inet_ntoa(ip[16:20]),
                "sport": sport,
                "dport": dport,
                "seq": seq,
                "ack": ack,
                "flags": tcp[13],
                "tcp_payload_length": max(
                    0, struct.unpack_from("!H", ip, 2)[0] - ihl - header
                ),
                "captured_bytes": size,
                "wire_bytes": wire,
            }
        )
    return rows, {
        "packets": count,
        "decoded_tcp_packets": len(rows),
        "unparsed_fragments_or_short_headers": truncated_tcp,
        "captured_http_payload_bytes": 0,
    }


def correlate(sample, host, port, index):
    if not sample.get("source"):
        return {"classification": "missing_source_port", "packets": []}
    source, client_port = sample["source"]
    key = (source, client_port, host, port)
    reverse = (host, port, source, client_port)
    begin = sample["started_at"] - 0.02
    end = sample["time"] + 0.25
    outgoing = [p for p in index.get(key, []) if begin <= p["time"] <= end]
    incoming = [p for p in index.get(reverse, []) if begin <= p["time"] <= end]
    syn = [p for p in outgoing if p["flags"] & 2 and not p["flags"] & 16]
    synack = [p for p in incoming if p["flags"] & 18 == 18]
    resets = [p for p in outgoing + incoming if p["flags"] & 4]
    sent = [p for p in outgoing if p["tcp_payload_length"] > 0]
    received = [p for p in incoming if p["tcp_payload_length"] > 0]
    acked = any(
        p["flags"] & 16
        and 0
        <= ((p["ack"] - (s["seq"] + s["tcp_payload_length"])) & 0xFFFFFFFF)
        < 0x80000000
        for s in sent
        for p in incoming
    )
    if resets:
        label = "reset_seen"
    elif (
        syn
        and not synack
        and sample.get("phase") == "connect"
        and sample.get("error_type") == "TimeoutError"
    ):
        label = "connect_timeout_syn_without_synack_in_capture"
    elif (
        synack
        and sent
        and not received
        and sample.get("phase") == "headers"
        and sample.get("error_type") == "TimeoutError"
    ):
        label = "headers_timeout_after_handshake_without_response_data"
    elif sample.get("status") == 200 and synack and received:
        label = "client_success_with_handshake_and_response_data"
    else:
        label = "incomplete_capture_or_other"
    return {
        "classification": label,
        "syn_packets": len(syn),
        "synack_packets": len(synack),
        "reset_packets": len(resets),
        "request_data_acked": acked,
        "response_data_packets": len(received),
        "packets": sorted(outgoing + incoming, key=lambda p: p["time"]),
    }


def archive_report():
    """Publish only captures referenced by this run; verify bytes and retain partials."""
    report_path = lab.ROOT / "evidence/connection-window.json"
    archive_path = lab.ROOT / "evidence/packet-archive.json"
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    result = {"created_utc": time.time(), "captures": [], "errors": []}
    seen = set()
    if report_path.exists():
        report = json.loads(report_path.read_text())
        for trial in report.get("trials", []):
            packet_report = trial.get("packet_capture") or {}
            for capture in packet_report.get("captures", []):
                source = (lab.ROOT / capture["path"]).resolve()
                if not source.is_relative_to(DIRECTORY.resolve()):
                    result["errors"].append(
                        {
                            "path": str(source),
                            "error": "capture path is outside .local/packets",
                        }
                    )
                    continue
                seen.add(source)
                row = {
                    "node": capture.get("node"),
                    "grace_seconds": trial.get("grace_seconds"),
                    "trial": trial.get("trial"),
                    "source_sha256": capture.get("sha256"),
                    "capture_stats": {
                        k: capture.get(k)
                        for k in [
                            "packets",
                            "dropped_by_capture_kernel",
                            "exit_code",
                            "snaplen",
                            "datalink",
                            "tcpdump_version",
                        ]
                    },
                }
                if not source.is_file():
                    row["error"] = "capture file is missing"
                    result["errors"].append(row)
                    result["captures"].append(row)
                    continue
                digest = hashlib.sha256(source.read_bytes()).hexdigest()
                if digest != capture.get("sha256"):
                    row["error"] = "capture SHA-256 differs from the test report"
                    result["errors"].append(row)
                    result["captures"].append(row)
                    continue
                destination = ARCHIVE / (source.name + ".gz")
                with source.open("rb") as inp, destination.open("wb") as raw:
                    with gzip.GzipFile(
                        filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0
                    ) as out:
                        while block := inp.read(1024 * 1024):
                            out.write(block)
                row.update(
                    path=str(destination.relative_to(lab.ROOT)),
                    sha256_gzip=hashlib.sha256(destination.read_bytes()).hexdigest(),
                    bytes_uncompressed=source.stat().st_size,
                    bytes_compressed=destination.stat().st_size,
                )
                capture["path"] = row["path"]
                result["captures"].append(row)
    # Include files left by an interrupted trial; never silently discard a partial capture.
    for source in sorted(DIRECTORY.glob("removal-*.pcap")) if DIRECTORY.exists() else []:
        source = source.resolve()
        if source in seen:
            continue
        destination = ARCHIVE / (source.name + ".gz")
        try:
            with source.open("rb") as inp, destination.open("wb") as raw:
                with gzip.GzipFile(
                    filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0
                ) as out:
                    while block := inp.read(1024 * 1024):
                        out.write(block)
            row = {
                "path": str(destination.relative_to(lab.ROOT)),
                "sha256_gzip": hashlib.sha256(destination.read_bytes()).hexdigest(),
                "bytes_uncompressed": source.stat().st_size,
                "bytes_compressed": destination.stat().st_size,
                "state": "unreferenced partial or capture-tool diagnostic",
            }
            result["captures"].append(row)
        except Exception as error:
            result["errors"].append({"path": str(source), "error": str(error)})
    result["complete"] = not result["errors"] and bool(result["captures"])
    archive_path.write_text(json.dumps(result, indent=2) + "\n")
    return result
