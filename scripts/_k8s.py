"""Helpers shared by k8s_up.py and k8s_down.py: run commands, and start/stop the
background port-forward that makes the cluster reachable on localhost."""

import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAMESPACE = "emergency"
# Images built from this repo (docker compose build) and the one pulled from Docker Hub.
BUILT_IMAGES = ["storage-node", "coordinator", "request-service", "matching-service", "ui-service"]
RABBITMQ_IMAGE = "rabbitmq:3.13-management"
# KEDA scales matching-service on the queue length (k8s/keda/). It is installed from its release
# manifest, into its own namespace, before the application is deployed.
KEDA_VERSION = "2.21.1"
KEDA_URL = f"https://github.com/kedacore/keda/releases/download/v{KEDA_VERSION}/keda-{KEDA_VERSION}.yaml"
KEDA_DEPLOYMENTS = ["keda-operator", "keda-metrics-apiserver", "keda-admission"]
# Everything Kubernetes keeps running for us, in the order we wait for them.
DEPLOYMENTS = ["coordinator", "request-service", "matching-service", "ui-service"]
# Not built from this repo, so never restarted after a rebuild (that would clear the metric history).
OBSERVABILITY = ["prometheus", "grafana"]
STATEFULSETS = ["rabbitmq", "shard0", "shard1", "shard2"]
# Local ports the forwarder uses (see k8s_forward.py): they must be free.
FORWARDED_PORTS = [8000, 8080, 8081, 15672, 3000, 9090] + list(range(9001, 9007))

PID_FILE = Path(tempfile.gettempdir()) / "spd-emergency-forward.pid"
LOG_FILE = Path(tempfile.gettempdir()) / "spd-emergency-forward.log"


class Fail(Exception):
    """Something the user has to fix; the message says what."""


def step(text: str) -> None:
    print(f"\n>> {text}", flush=True)


def need(tool: str) -> None:
    if shutil.which(tool) is None:
        raise Fail(f"'{tool}' is not installed or not on PATH")


def run(*cmd: str, check: bool = True, quiet: bool = False) -> subprocess.CompletedProcess:
    """Run a command from the repo root. quiet=True hides its output unless it fails."""
    result = subprocess.run(cmd, cwd=ROOT, capture_output=quiet, text=True)
    if check and result.returncode != 0:
        if quiet:
            sys.stderr.write((result.stdout or "") + (result.stderr or ""))
        raise Fail(f"command failed ({result.returncode}): {' '.join(cmd)}")
    return result


def kubectl(*args: str, check: bool = True, quiet: bool = False) -> subprocess.CompletedProcess:
    return run("kubectl", "-n", NAMESPACE, *args, check=check, quiet=quiet)


def docker_running() -> bool:
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def minikube_running() -> bool:
    out = subprocess.run(["minikube", "status", "--format", "{{.Host}}/{{.APIServer}}"], capture_output=True, text=True)
    return out.stdout.strip() == "Running/Running"


def port_in_use(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def http_json(url: str, timeout: float = 3.0):
    import json

    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())


def wipe_data() -> None:
    """Delete the namespace AND the stored data, so the next start is empty.

    Deleting the namespace alone is not enough on minikube: its storage provisioner
    keeps each volume in a folder named after the namespace and claim
    (/tmp/hostpath-provisioner/emergency/data-shard0-0) and does not always remove it,
    so a recreated namespace would be handed its old data back.
    """
    run("kubectl", "delete", "namespace", NAMESPACE, "--ignore-not-found", "--wait=true")
    leftovers = run(
        "kubectl", "get", "pv", "-o",
        f'jsonpath={{range .items[?(@.spec.claimRef.namespace=="{NAMESPACE}")]}}{{.metadata.name}} {{end}}',
        check=False, quiet=True,
    ).stdout.split()
    if leftovers:
        run("kubectl", "delete", "pv", "--wait=false", *leftovers, quiet=True)
    context = run("kubectl", "config", "current-context", check=False, quiet=True).stdout.strip()
    if context == "minikube":
        run("minikube", "ssh", f"sudo rm -rf /tmp/hostpath-provisioner/{NAMESPACE}", quiet=True)


# -- the background port-forward -----------------------------------------------------------------

def forward_running() -> bool:
    if not PID_FILE.exists():
        return False
    try:
        pid = int(PID_FILE.read_text())
    except ValueError:
        return False
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def start_forward() -> None:
    """Start scripts/k8s_forward.py detached from this terminal, so it keeps running after we exit."""
    log = open(LOG_FILE, "w")
    kwargs: dict = {"cwd": ROOT, "stdout": log, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "k8s_forward.py")], **kwargs)
    PID_FILE.write_text(str(proc.pid))


def stop_forward() -> bool:
    """Stop the background forwarder and its kubectl children. True if one was running."""
    was_running = forward_running()
    if was_running:
        pid = int(PID_FILE.read_text())
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
        else:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except OSError:
                pass
    PID_FILE.unlink(missing_ok=True)
    time.sleep(1)
    return was_running
