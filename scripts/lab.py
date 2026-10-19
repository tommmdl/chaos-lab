#!/usr/bin/env python3
"""Build a scoped Chaos Mesh exercise without changing existing lab workloads."""

import argparse, fcntl, hashlib, io, json, os, secrets, subprocess, tarfile, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local"
LOCAL.mkdir(exist_ok=True)
OWNER = "chaos-lab"
APP = "chaos-demo"
OBS = "chaos-observe"
SYSTEM = "chaos-system"
K = os.environ.get("KUBECTL", str(ROOT.parent / "kubernetes-lab/.local/bin/kubectl"))
CONFIG = Path(
    os.environ.get(
        "KUBECONFIG_CHAOS", str(ROOT.parent / "kubernetes-lab/.local/kubeconfig")
    )
)
CONTEXT = "kind-devops-notes"
IMAGES = json.loads((ROOT / "config/images.json").read_text())
TOOLS = json.loads((ROOT / "config/tools.json").read_text())


def run(args, data=None, ok=True, timeout=180):
    p = subprocess.run(
        [str(x) for x in args],
        input=data,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if ok and p.returncode:
        raise RuntimeError(f"{args[0]} failed: {p.stderr[-2200:]}")
    return p


def k(ns, *args, data=None, ok=True, timeout=180):
    return run(
        [K, "--kubeconfig", CONFIG, "--context", CONTEXT, "-n", ns, *args],
        data,
        ok,
        timeout,
    )


def obj(ns, kind, name):
    return json.loads(k(ns, "get", kind, name, "-o", "json").stdout)


def apply(ns, item):
    k(ns, "apply", "-f", "-", data=json.dumps(item))


def meta(ns, name):
    return {
        "namespace": ns,
        "name": name,
        "labels": {"app.kubernetes.io/part-of": OWNER},
    }


def guard(ns):
    if (
        obj(ns, "namespace", ns)["metadata"]
        .get("labels", {})
        .get("app.kubernetes.io/part-of")
        != OWNER
    ):
        raise RuntimeError("Namespace is not owned by this lab: " + ns)


def wait(fn, timeout=120):
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        try:
            value = fn()
            if value:
                return value
        except (ValueError, OSError, RuntimeError) as e:
            last = str(e)
        time.sleep(2)
    raise RuntimeError("Timed out: " + str(last or fn))


def sql(query):
    return k(
        APP,
        "exec",
        "-i",
        "deployment/postgres",
        "--",
        "psql",
        "-X",
        "-U",
        "notes_admin",
        "-d",
        "notes",
        "-At",
        "-v",
        "ON_ERROR_STOP=1",
        data=query,
        timeout=30,
    ).stdout


def tools():
    for filename, url, sha in [
        (
            "helm.tar.gz",
            "https://get.helm.sh/helm-v3.22.1-linux-amd64.tar.gz",
            TOOLS["helm"]["sha256"],
        ),
        (
            "chaos-mesh-2.8.4.tgz",
            "https://charts.chaos-mesh.org/chaos-mesh-2.8.4.tgz",
            TOOLS["chart"]["sha256"],
        ),
    ]:
        path = LOCAL / filename
        if not path.exists():
            with urllib.request.urlopen(url, timeout=90) as r:
                path.write_bytes(r.read())
        if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
            raise RuntimeError("Checksum mismatch: " + filename)
    (LOCAL / "bin").mkdir(exist_ok=True)
    with tarfile.open(LOCAL / "helm.tar.gz") as t:
        (LOCAL / "bin/helm").write_bytes(t.extractfile("linux-amd64/helm").read())
    (LOCAL / "bin/helm").chmod(0o755)


def service(ns, name, app, port, node=None, probes=False, headless=False):
    spec = {"selector": {"app": app}, "ports": [{"port": port, "targetPort": port}]}
    if node:
        spec.update(type="NodePort")
        spec["ports"][0]["nodePort"] = node
    if probes:
        spec["publishNotReadyAddresses"] = True
    if headless:
        spec["clusterIP"] = "None"
    apply(
        ns,
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": meta(ns, name),
            "spec": spec,
        },
    )


def pvc(ns, name, size):
    apply(
        ns,
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": meta(ns, name),
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "storageClassName": "standard",
                "resources": {"requests": {"storage": size}},
            },
        },
    )


def config(ns, name, files):
    apply(
        ns,
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": meta(ns, name),
            "data": {file: (ROOT / "config" / file).read_text() for file in files},
        },
    )


def deployment(
    ns,
    name,
    image,
    uid,
    port,
    args=None,
    env=None,
    mounts=None,
    volumes=None,
    replicas=1,
    readiness=None,
    readonly=True,
    memory="192Mi",
    command=None,
):
    c = {
        "name": name,
        "image": image,
        "ports": [{"containerPort": port}],
        "env": env or [],
        "resources": {
            "requests": {"cpu": "50m", "memory": "64Mi"},
            "limits": {"cpu": "1", "memory": memory},
        },
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": readonly,
            "capabilities": {"drop": ["ALL"]},
        },
        "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}] + (mounts or []),
    }
    if args:
        c["args"] = args
    if command:
        c["command"] = command
    if readiness:
        c["readinessProbe"] = {
            "httpGet": {"path": readiness, "port": port},
            "periodSeconds": 3,
            "failureThreshold": 2,
        }
    elif name == "postgres":
        c["readinessProbe"] = {
            "exec": {"command": ["pg_isready", "-U", "lab", "-d", "notes"]},
            "periodSeconds": 3,
            "initialDelaySeconds": 5,
        }
    spec = {
        "automountServiceAccountToken": False,
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": uid,
            "runAsGroup": uid,
            "fsGroup": uid,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [c],
        "volumes": [{"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}}]
        + (volumes or []),
    }
    # Spread API replicas across nodes when possible, without making CI depend on a second node.
    if name == "api":
        spec["affinity"] = {
            "podAntiAffinity": {
                "preferredDuringSchedulingIgnoredDuringExecution": [
                    {
                        "weight": 100,
                        "podAffinityTerm": {
                            "labelSelector": {"matchLabels": {"app": "api"}},
                            "topologyKey": "kubernetes.io/hostname",
                        },
                    }
                ]
            }
        }
    apply(
        ns,
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": meta(ns, name),
            "spec": {
                "replicas": replicas,
                "strategy": {"type": "Recreate" if name != "api" else "RollingUpdate"},
                "selector": {"matchLabels": {"app": name}},
                "template": {
                    "metadata": {
                        "labels": {"app": name, "app.kubernetes.io/part-of": OWNER}
                    },
                    "spec": spec,
                },
            },
        },
    )


def install():
    for ns in [APP, OBS, SYSTEM]:
        p = k(ns, "get", "namespace", ns, ok=False)
        if p.returncode == 0:
            guard(ns)
        elif "NotFound" not in p.stderr:
            raise RuntimeError(p.stderr)
    existing = k(SYSTEM, "get", "crd", "podchaos.chaos-mesh.org", ok=False)
    if (
        existing.returncode == 0
        and k(
            SYSTEM, "get", "secret", "-l", "owner=helm,name=chaos-lab", "-o", "name"
        ).stdout.strip()
        == ""
    ):
        raise RuntimeError("An existing Chaos Mesh installation must not be adopted")
    if existing.returncode == 0:
        for kind in ["podchaos", "networkchaos"]:
            if json.loads(k(APP, "get", kind, "-o", "json").stdout)["items"]:
                raise RuntimeError(
                    "Inspect/recover existing experiments before reconfiguring controllers or workloads"
                )
    for ns in [APP, OBS, SYSTEM]:
        item = {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": ns, "labels": {"app.kubernetes.io/part-of": OWNER}},
        }
        if ns == APP:
            item["metadata"]["annotations"] = {"chaos-mesh.org/inject": "enabled"}
        apply(ns, item)
    password = LOCAL / "db-password"
    if not password.exists():
        if json.loads(k(APP, "get", "pvc", "-o", "json").stdout)["items"]:
            raise RuntimeError(
                "Database volume exists but the private password file is missing"
            )
        password.write_text(secrets.token_urlsafe(32))
        password.chmod(0o600)
    apply(
        APP,
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": meta(APP, "database"),
            "stringData": {"password": password.read_text().strip()},
        },
    )
    # Namespace-scoped chart RBAC omits namespace reads needed by FilterNamespace.
    apply(
        SYSTEM,
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {
                "name": "chaos-lab-namespace-reader",
                "labels": {"app.kubernetes.io/part-of": OWNER},
            },
            "rules": [
                {
                    "apiGroups": [""],
                    "resources": ["namespaces"],
                    "verbs": ["get", "list", "watch"],
                },
                {
                    "apiGroups": ["chaos-mesh.org"],
                    "resources": ["remoteclusters"],
                    "verbs": ["get", "list", "watch"],
                },
            ],
        },
    )
    apply(
        SYSTEM,
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRoleBinding",
            "metadata": {
                "name": "chaos-lab-namespace-reader",
                "labels": {"app.kubernetes.io/part-of": OWNER},
            },
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "ClusterRole",
                "name": "chaos-lab-namespace-reader",
            },
            "subjects": [
                {
                    "kind": "ServiceAccount",
                    "name": "chaos-controller-manager",
                    "namespace": SYSTEM,
                }
            ],
        },
    )
    tools()
    run(
        [
            LOCAL / "bin/helm",
            "upgrade",
            "--install",
            "chaos-lab",
            LOCAL / "chaos-mesh-2.8.4.tgz",
            "--namespace",
            SYSTEM,
            "--kubeconfig",
            CONFIG,
            "--kube-context",
            CONTEXT,
            "-f",
            ROOT / "config/chaos-values.yaml",
            "--wait",
            "--timeout",
            "5m",
        ],
        timeout=330,
    )
    k(
        SYSTEM,
        "rollout",
        "status",
        "daemonset/chaos-daemon",
        "--timeout=180s",
        timeout=200,
    )
    pvc(APP, "postgres-data", "1Gi")
    secrets_vol = {
        "name": "password",
        "secret": {"secretName": "database", "defaultMode": 256},
    }
    deployment(
        APP,
        "postgres",
        IMAGES["postgres"]["image"],
        70,
        5432,
        env=[
            {"name": "PGDATA", "value": "/var/lib/postgresql/data/pgdata"},
            {"name": "POSTGRES_DB", "value": "notes"},
            {"name": "POSTGRES_USER", "value": "lab"},
            {"name": "POSTGRES_PASSWORD_FILE", "value": "/run/secrets/password"},
            {
                "name": "POSTGRES_INITDB_ARGS",
                "value": "--encoding=UTF8 --locale=C.UTF-8 --locale-provider=icu --icu-locale=en-US",
            },
        ],
        mounts=[
            {"name": "data", "mountPath": "/var/lib/postgresql/data"},
            {"name": "socket", "mountPath": "/var/run/postgresql"},
            {"name": "password", "mountPath": "/run/secrets", "readOnly": True},
        ],
        volumes=[
            {"name": "data", "persistentVolumeClaim": {"claimName": "postgres-data"}},
            {"name": "socket", "emptyDir": {}},
            secrets_vol,
        ],
        readonly=False,
        memory="384Mi",
    )
    service(APP, "postgres", "postgres", 5432, headless=True)
    k(APP, "rollout", "status", "deployment/postgres", "--timeout=160s", timeout=180)
    deployment(
        APP,
        "api",
        IMAGES["api"]["image"],
        10001,
        8000,
        env=[
            {"name": "DB_HOST", "value": "postgres"},
            {"name": "DB_NAME", "value": "notes"},
            {"name": "DB_USER", "value": "lab"},
            {"name": "DB_PASSWORD_FILE", "value": "/run/secrets/password"},
        ],
        mounts=[{"name": "password", "mountPath": "/run/secrets", "readOnly": True}],
        volumes=[secrets_vol],
        replicas=2,
        readiness="/readyz",
    )
    service(APP, "api", "api", 8000, 30434)
    service(APP, "api-probes", "api", 8000, probes=True)
    config(OBS, "prometheus-config", ["prometheus.yml", "rules.yml"])
    config(OBS, "blackbox-config", ["blackbox.yml"])
    config(OBS, "alertmanager-config", ["alertmanager.yml"])
    config(OBS, "receiver-code", ["receiver.py"])
    for name, size in [
        ("prometheus", "1Gi"),
        ("alertmanager", "256Mi"),
        ("receiver", "128Mi"),
    ]:
        pvc(OBS, name + "-data", size)
    entries = [
        (
            "blackbox",
            65534,
            9115,
            ["--config.file=/etc/config/blackbox.yml"],
            "blackbox-config",
            "/",
            None,
            None,
        ),
        (
            "prometheus",
            65534,
            9090,
            [
                "--config.file=/etc/prometheus/prometheus.yml",
                "--storage.tsdb.path=/data",
                "--storage.tsdb.retention.time=2d",
            ],
            "prometheus-config",
            "/-/ready",
            30435,
            None,
        ),
        (
            "alertmanager",
            65534,
            9093,
            [
                "--config.file=/etc/config/alertmanager.yml",
                "--storage.path=/data",
                "--cluster.listen-address=",
            ],
            "alertmanager-config",
            "/-/ready",
            30436,
            None,
        ),
        (
            "receiver",
            10001,
            8080,
            None,
            "receiver-code",
            "/healthz",
            30437,
            ["python", "/srv/receiver.py"],
        ),
    ]
    for name, uid, port, args, cm, ready, node, command in entries:
        path = (
            "/etc/prometheus"
            if name == "prometheus"
            else "/srv" if name == "receiver" else "/etc/config"
        )
        volumes = [{"name": "config", "configMap": {"name": cm}}]
        mounts = [{"name": "config", "mountPath": path, "readOnly": True}]
        if name != "blackbox":
            volumes.append(
                {"name": "data", "persistentVolumeClaim": {"claimName": name + "-data"}}
            )
            mounts.append({"name": "data", "mountPath": "/data"})
        deployment(
            OBS,
            name,
            IMAGES[name]["image"],
            uid,
            port,
            args=args,
            command=command,
            mounts=mounts,
            volumes=volumes,
            readiness=ready,
            memory="384Mi" if name == "prometheus" else "192Mi",
        )
        service(OBS, name, name, port, node)
    for ns, names in [
        (APP, ["api"]),
        (OBS, ["blackbox", "prometheus", "alertmanager", "receiver"]),
    ]:
        for name in names:
            k(
                ns,
                "rollout",
                "status",
                "deployment/" + name,
                "--timeout=180s",
                timeout=200,
            )
    (LOCAL / "installed").write_text("Namespaces and release owned by chaos-lab\n")
    print("Chaos Mesh and the scoped application/alert stack are ready")


def cleanup():
    guard(APP)
    for kind in ["podchaos", "networkchaos"]:
        k(
            APP,
            "delete",
            kind,
            "-l",
            "app.kubernetes.io/part-of=chaos-lab",
            "--timeout=120s",
            timeout=140,
        )


def status():
    for ns in [APP, OBS, SYSTEM]:
        print(ns, k(ns, "get", "pods,pvc").stdout)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["up", "status", "recover"])
    args = p.parse_args()
    if args.action == "status":
        status()
        return
    with (LOCAL / "operation.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another lab operation is running")
        if args.action == "up":
            install()
        else:
            cleanup()
            print("Owned experiments removed; controllers and data retained")


if __name__ == "__main__":
    main()
