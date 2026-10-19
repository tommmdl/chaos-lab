#!/usr/bin/env python3
"""Create one disposable CI cluster; never adopt an existing cluster."""

import hashlib, os, urllib.request
import lab

if os.environ.get("CI") != "true":
    raise SystemExit("For disposable GitHub Actions runners only")
(lab.LOCAL / "bin").mkdir(exist_ok=True)
urls = {
    "kind": "https://github.com/kubernetes-sigs/kind/releases/download/v0.33.0/kind-linux-amd64",
    "kubectl": "https://dl.k8s.io/release/v1.37.1/bin/linux/amd64/kubectl",
}
for tool, url in urls.items():
    path = lab.LOCAL / "bin" / tool
    if not path.exists():
        with urllib.request.urlopen(url, timeout=90) as r:
            path.write_bytes(r.read())
    if hashlib.sha256(path.read_bytes()).hexdigest() != lab.TOOLS[tool]["sha256"]:
        raise SystemExit("Checksum mismatch: " + tool)
    path.chmod(0o755)
lab.run(["sudo", "modprobe", "sch_netem"])
lab.run(["sudo", "sysctl", "-w", "fs.inotify.max_user_instances=512"])
kind = lab.LOCAL / "bin/kind"
if "devops-notes" in lab.run([kind, "get", "clusters"]).stdout.splitlines():
    raise SystemExit("Refusing to adopt an existing cluster")
(lab.LOCAL / "ci-cluster-owned").write_text("devops-notes\n")
lab.run(
    [
        kind,
        "create",
        "cluster",
        "--name",
        "devops-notes",
        "--image",
        lab.TOOLS["node_image"],
        "--kubeconfig",
        lab.CONFIG,
        "--wait",
        "120s",
    ],
    timeout=240,
)
print("Disposable CI cluster ready")
