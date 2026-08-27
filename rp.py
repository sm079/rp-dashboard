#!/usr/bin/env python3
"""Run ComfyUI on a RunPod pod that is reachable only over SSH.

Commands:
  start     launch (or reuse) a pod, connect, open ComfyUI in the browser
            (--models krea2 h3 etc. picks the model sets; see models.json)
  (dashboard.py serves a web UI for start/stop and runs these commands)
  up        launch a pod, run remote setup, download default models
  models    download model sets onto the pod (see models.json)
  connect   open the SSH tunnel to ComfyUI; sync the local sync folder
  status    show the current pod
  ssh       open an interactive shell on the pod
  down      copy anything unsaved, then terminate the pod
"""
import argparse
import json
import math
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import events
from events import emit

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / ".state"
POD_FILE = STATE_DIR / "pod.json"
REST = "https://rest.runpod.io/v1"


def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def save_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")


CONFIG = load_json(ROOT / "config.json", {})


def api_key():
    key = os.environ.get("RUNPOD_API_KEY")
    if not key and sys.platform == "win32":
        # A user env var set after this shell started is only in the registry.
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                key = winreg.QueryValueEx(k, "RUNPOD_API_KEY")[0]
        except OSError:
            pass
    if not key:
        sys.exit("RUNPOD_API_KEY is not set")
    return key


def api(method, path, body=None, exit_on_error=True):
    req = urllib.request.Request(
        REST + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        # Cloudflare in front of the API rejects urllib's default User-Agent.
        headers={"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json",
                 "User-Agent": "runpod-helper/0.1"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        e.detail = e.read().decode(errors="replace")
        if not exit_on_error:
            raise
        sys.exit(f"RunPod API {method} {path} failed: {e.code} {e.detail}")
    return json.loads(raw) if raw else None


def current_pod():
    pod = load_json(POD_FILE)
    if not pod:
        sys.exit("No pod recorded. Run: python rp.py up")
    return pod


def ask(prompt):
    """input() that answers "" when there is no one to ask (dashboard, closed stdin)."""
    if events.ENABLED:
        return ""
    try:
        return input(prompt)
    except EOFError:
        return ""


# --- SSH helpers ------------------------------------------------------------

def ssh_base(pod, *extra):
    key = os.path.expanduser(CONFIG.get("ssh_key", "~/.ssh/id_ed25519"))
    return [
        "ssh", "-i", key, "-p", str(pod["ssh_port"]),
        "-o", "StrictHostKeyChecking=accept-new",
        # Per pod: RunPod reuses IP:port pairs, and a new pod there has a new
        # host key, which a shared known_hosts file would reject.
        "-o", f"UserKnownHostsFile={STATE_DIR / ('known_hosts_' + pod['id'])}",
        "-o", "ServerAliveInterval=30",
        "-o", "ConnectTimeout=15",
        *extra,
        f"root@{pod['ip']}",
    ]


def shlex_quote(s):
    return "'" + s.replace("'", "'\"'\"'") + "'"


def ssh_script(pod, script_path, *args):
    """Pipe a local shell script to bash on the pod."""
    quoted = " ".join(shlex_quote(a) for a in args)
    cmd = ssh_base(pod, "-o", "BatchMode=yes") + [f"bash -s -- {quoted}"]
    with open(script_path, "rb") as f:
        return subprocess.run(cmd, stdin=f).returncode


# --- commands ---------------------------------------------------------------

def wait_for_ssh(pod_id, timeout=900):
    start = time.time()
    last = None
    while time.time() - start < timeout:
        info = api("GET", f"/pods/{pod_id}")
        ip = info.get("publicIp")
        port = (info.get("portMappings") or {}).get("22")
        status = info.get("desiredStatus")
        msg = f"status={status} ip={ip or '-'} ssh_port={port or '-'}"
        if msg != last:
            print(f"  [{int(time.time() - start):>3}s] {msg}", flush=True)
            last = msg
        if ip and port:
            pod = {"id": pod_id, "ip": ip, "ssh_port": port}
            try:
                probe = subprocess.run(ssh_base(pod, "-o", "BatchMode=yes") + ["true"],
                                       capture_output=True, text=True, timeout=60)
            except subprocess.TimeoutExpired:
                probe = subprocess.CompletedProcess([], 255, "", "probe timed out after 60s")
            if probe.returncode == 0:
                return pod
            err = (probe.stderr.strip().splitlines() or ["(no output)"])[-1]
            msg = f"ssh not ready: {err}"
            if msg != last:
                print(f"  [{int(time.time() - start):>3}s] {msg}", flush=True)
                last = msg
        time.sleep(5)
    return None


def cmd_up(args):
    existing = load_json(POD_FILE)
    if existing:
        sys.exit(f"A pod is already recorded ({existing['id']}). Use `status`, or `down` first.")
    # Checked before launching so a typo doesn't cost a pod.
    sets = [] if args.no_setup or args.no_models else model_sets(args.models)
    attempts = CONFIG.get("launch_attempts", 3)
    timeout = CONFIG.get("launch_timeout_s", 240)
    for attempt in range(1, attempts + 1):
        pod = launch(args, timeout, disk_gb(sets))
        if pod:
            healthy, detail = health_check(pod)
            print(f"  host check: {detail}", flush=True)
            if healthy:
                break
            reason = "host is overloaded"
        else:
            # Usually stuck pulling the image on an uncached host.
            reason = f"no SSH after {timeout}s"
        # Either way the pod bills while unusable, so replace it.
        bad = load_json(POD_FILE)
        api("DELETE", f"/pods/{bad['id']}")
        POD_FILE.unlink(missing_ok=True)
        print(f"Pod {bad['id']}: {reason}; terminated it (attempt {attempt}/{attempts}).", flush=True)
    else:
        sys.exit("Could not get a reachable pod. Try again later or another GPU type (--gpu).")
    save_json(POD_FILE, pod)
    print(f"SSH ready: root@{pod['ip']} -p {pod['ssh_port']}")
    if args.no_setup:
        return
    run_setup(pod)
    if sets:
        rc = download_models(pod, sets)
        if rc != 0:
            sys.exit(f"Model download failed (exit {rc}); retry with: python rp.py models")
    print("Ready. Next: python rp.py connect")


def model_sets(names):
    """The named model sets from models.json, or default_models from config.json.

    Each set holds everything its model needs to run (text encoder, VAE, ...).
    """
    catalog = load_json(ROOT / "models.json")
    names = list(dict.fromkeys(names or CONFIG.get("default_models", [])))
    unknown = [n for n in names if n not in catalog]
    if unknown:
        sys.exit(f"Unknown model sets: {', '.join(unknown)}. Known: {', '.join(catalog)}")
    return names


def disk_gb(sets):
    """Container disk for the template plus the model sets; container_disk_gb is the minimum."""
    catalog = load_json(ROOT / "models.json")
    need = sum(catalog[n].get("size_gb", 0) for n in sets) + CONFIG.get("disk_headroom_gb", 30)
    return max(CONFIG["container_disk_gb"], math.ceil(need / 10) * 10)


def health_check(pod):
    """Time an SSH round trip and a direct 512 MB disk write on the pod.

    Some hosts are so oversubscribed that ComfyUI takes minutes to start and
    SSH stalls; catching that here lets `up` replace the pod straight away.
    """
    emit("phase", name="health")
    limits = CONFIG.get("health_limits", {"ssh_s": 10, "disk_write_s": 8})
    cmd = ("start=$(date +%s.%N); dd if=/dev/zero of=/root/.rp_health bs=1M count=512 "
           "oflag=direct status=none; end=$(date +%s.%N); rm -f /root/.rp_health; "
           "echo $start $end; cut -d' ' -f1 /proc/loadavg; nproc")
    t0 = time.time()
    try:
        res = subprocess.run(ssh_base(pod, "-o", "BatchMode=yes") + [cmd],
                             capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return False, "check timed out after 120s"
    total = time.time() - t0
    try:
        start, end, load, cpus = res.stdout.split()
        disk = float(end) - float(start)
    except ValueError:
        return False, f"check failed: {res.stderr.strip()[-200:]}"
    ssh_s = total - disk
    detail = f"ssh {ssh_s:.1f}s, 512 MB disk write {disk:.1f}s, load {load} on {cpus} CPUs"
    return ssh_s <= limits["ssh_s"] and disk <= limits["disk_write_s"], detail


def launch(args, timeout, disk):
    """Create a pod and wait for SSH. Returns the pod, or None if it never came up."""
    body = {
        "name": CONFIG["pod_name"],
        "templateId": CONFIG["template_id"],
        "gpuTypeIds": [args.gpu or CONFIG["gpu_type"]],
        "gpuCount": 1,
        "cloudType": CONFIG["cloud_type"],
        "containerDiskInGb": disk,
        "volumeInGb": 0,
        # Large video models are staged in pinned RAM; too little gets ComfyUI OOM-killed.
        "minRAMPerGPU": args.min_ram or CONFIG.get("min_ram_gb", 0),
        # SSH only: no public proxy URLs for ComfyUI, Jupyter or the file browser.
        "ports": ["22/tcp"],
        "supportPublicIp": True,
    }
    print(f"Launching {body['gpuTypeIds'][0]} ({body['cloudType']}) from template {body['templateId']}")
    emit("phase", name="launch", detail=body["gpuTypeIds"][0])
    wait_s = CONFIG.get("capacity_wait_s", 600)
    start = time.time()
    while True:
        try:
            created = api("POST", "/pods", body, exit_on_error=False)
            break
        except urllib.error.HTTPError as e:
            detail = getattr(e, "detail", "")
            if "no instances currently available" not in detail or time.time() - start > wait_s:
                sys.exit(f"RunPod API POST /pods failed: {e.code} {detail}")
            print(f"  no {body['gpuTypeIds'][0]} available yet; retrying "
                  f"({int(time.time() - start)}s of {wait_s}s)", flush=True)
            emit("phase", name="launch", detail="waiting for a free GPU")
            time.sleep(30)
    pod_id = created["id"]
    print(f"Pod {pod_id} created, ${created.get('costPerHr', '?')}/hr. Waiting for SSH...", flush=True)
    emit("phase", name="ssh", detail=pod_id)
    created_at = time.time()
    save_json(POD_FILE, {"id": pod_id, "created": created_at})
    pod = wait_for_ssh(pod_id, timeout)
    if pod:
        pod["created"] = created_at
    return pod


def run_setup(pod):
    emit("phase", name="setup")
    if CONFIG.get("comfyui_version"):
        print(f"Pinning ComfyUI {CONFIG['comfyui_version']} and installing custom nodes...")
        rc = ssh_script(pod, ROOT / "remote" / "comfyui.sh",
                        CONFIG["comfyui_version"], *CONFIG.get("custom_nodes", []))
        if rc != 0:
            sys.exit(f"ComfyUI update failed (exit {rc})")
    print("Running remote setup (RAM-only ComfyUI folders, localhost only)...")
    rc = ssh_script(pod, ROOT / "remote" / "setup.sh", *CONFIG.get("comfyui_args", []))
    if rc != 0:
        sys.exit(f"Remote setup failed (exit {rc})")


def cmd_setup(args):
    run_setup(current_pod())


def cmd_models(args):
    sys.exit(download_models(current_pod(), model_sets(args.sets)))


def download_models(pod, names):
    catalog = load_json(ROOT / "models.json")
    unknown = [n for n in names if n not in catalog]
    if unknown:
        sys.exit(f"Unknown model sets: {unknown}. Known: {list(catalog)}")
    # A repo maps to a list of paths (repo mirrors ComfyUI's models/ layout) or
    # to {"into": "<models subfolder>", "files": [...]} for repos that don't.
    specs = []
    for name in names:
        for repo, entry in catalog[name]["files"].items():
            into, files = ("", entry) if isinstance(entry, list) else (entry["into"], entry["files"])
            specs.append(f"{repo}::{into}::{','.join(files)}")
    total = sum(catalog[n].get("size_gb", 0) for n in names)
    label = ", ".join(catalog[n].get("name", n) for n in names)
    print(f"Downloading {label} (~{total:.0f} GB) on the pod")
    emit("phase", name="models", detail=label)
    return ssh_script(pod, ROOT / "remote" / "models.sh", *specs)


def cmd_status(args):
    pod = load_json(POD_FILE)
    if not pod:
        print("No pod recorded.")
        return
    info = api("GET", f"/pods/{pod['id']}")
    print(json.dumps({k: info.get(k) for k in (
        "id", "name", "desiredStatus", "costPerHr", "publicIp", "portMappings", "machine")},
        indent=2, default=str))


def cmd_ssh(args):
    pod = current_pod()
    sys.exit(subprocess.call(ssh_base(pod) + list(args.command)))


def cmd_down(args):
    from sync import sync_once
    pod = current_pod()
    if not args.no_sync:
        emit("phase", name="copy")
        print("Copying any outputs and workflows not yet saved locally...")
        try:
            n = sync_once(pod, CONFIG, ssh_base, ROOT)
            print(f"  {n} file(s) copied" if n else "  everything already saved")
        except Exception as e:  # noqa: BLE001 - report and let the user decide
            print(f"  final copy failed: {e}")
            if ask("Terminate anyway? [y/N] ").strip().lower() != "y":
                sys.exit("Final copy failed; the pod was not terminated.")
    if not args.yes:
        answer = ask(f"Terminate pod {pod['id']}? Everything on it is deleted. [y/N] ")
        if answer.strip().lower() != "y":
            print("Cancelled.")
            return
    emit("phase", name="terminate")
    api("DELETE", f"/pods/{pod['id']}")
    POD_FILE.unlink(missing_ok=True)
    print(f"Pod {pod['id']} terminated.")


def cmd_connect(args):
    from sync import run_connect
    run_connect(current_pod(), CONFIG, ssh_base, ROOT, open_browser=args.open)


def cmd_start(args):
    """Reuse the recorded pod if it's still running, otherwise launch one; then connect."""
    pod = load_json(POD_FILE)
    if pod:
        try:
            info = api("GET", f"/pods/{pod['id']}", exit_on_error=False)
        except urllib.error.HTTPError:
            info = None
        if info and info.get("desiredStatus") == "RUNNING" and pod.get("ssh_port"):
            print(f"Reusing running pod {pod['id']}")
            emit("reuse", pod=pod["id"])
            # The running pod may have been started with other model sets.
            rc = download_models(pod, model_sets(args.models))
            if rc != 0:
                sys.exit(f"Model download failed (exit {rc})")
        else:
            print(f"Recorded pod {pod['id']} is gone; launching a new one")
            POD_FILE.unlink(missing_ok=True)
            pod = None
    if not pod:
        cmd_up(argparse.Namespace(gpu=None, min_ram=None, no_setup=False, no_models=False,
                                  models=args.models))
    from sync import run_connect
    run_connect(current_pod(), CONFIG, ssh_base, ROOT, open_browser=not args.no_open)
    if events.ENABLED:
        return
    try:
        answer = ask("Terminate the pod now? Unsaved files are copied first. [y/N] ")
    except KeyboardInterrupt:
        answer = ""
    if answer.strip().lower() == "y":
        cmd_down(argparse.Namespace(yes=True, no_sync=False))
    else:
        print("Pod left running (and billing). Stop it later from the dashboard or: python rp.py down")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("up", help="launch a pod")
    up.add_argument("--gpu", help="override the GPU type id from config.json")
    up.add_argument("--min-ram", type=int, help="override min_ram_gb from config.json")
    up.add_argument("--no-setup", action="store_true", help="only launch; skip setup and models")
    up.add_argument("--no-models", action="store_true", help="skip downloading models")
    up.add_argument("--models", nargs="+", metavar="SET",
                    help="model sets from models.json (default: default_models in config.json)")
    up.set_defaults(func=cmd_up)
    sub.add_parser("setup", help="re-run the remote setup").set_defaults(func=cmd_setup)
    m = sub.add_parser("models", help="download model sets onto the pod")
    m.add_argument("sets", nargs="*",
                   help="model set names from models.json (default: default_models in config.json)")
    m.set_defaults(func=cmd_models)
    c = sub.add_parser("connect", help="tunnel to ComfyUI and sync the sync folder")
    c.add_argument("--open", action="store_true", help="open ComfyUI in the browser once reachable")
    c.set_defaults(func=cmd_connect)
    st = sub.add_parser("start", help="launch or reuse a pod, then connect and open the browser")
    st.add_argument("--models", nargs="+", metavar="SET",
                    help="model sets from models.json (default: default_models in config.json)")
    st.add_argument("--no-open", action="store_true", help="don't open ComfyUI in the browser")
    st.set_defaults(func=cmd_start)
    sub.add_parser("status", help="show the pod").set_defaults(func=cmd_status)
    s = sub.add_parser("ssh", help="shell on the pod")
    s.add_argument("command", nargs=argparse.REMAINDER)
    s.set_defaults(func=cmd_ssh)
    d = sub.add_parser("down", help="terminate the pod")
    d.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    d.add_argument("--no-sync", action="store_true", help="skip the final copy of outputs")
    d.set_defaults(func=cmd_down)
    args = p.parse_args()
    try:
        if events.ENABLED:
            events.watch_stdin()
        args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", flush=True)
        sys.exit(130)


if __name__ == "__main__":
    main()
