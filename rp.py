#!/usr/bin/env python3
"""Run ComfyUI on a RunPod pod that is reachable only over SSH.

Commands:
  start     launch (or reuse) a pod, connect, open ComfyUI in the browser
            (--models krea2 qwen21 etc. picks the model sets; see models.json,
            --gpu 4090 / 5090 / pro6000 the GPU; see gpu_types in config.json,
            --user-data ram / disk where ComfyUI keeps inputs, outputs and workflows)
  (dashboard.py serves a web UI for start/stop and runs these commands)
  up        launch a pod, run remote setup, download default models
  models    download model sets onto the pod (see models.json)
  lora      add, list, remove or download LoRAs (CivitAI or Hugging Face links)
  connect   open the SSH tunnel to ComfyUI; sync the local sync folder
  status    show the current pod
  ssh       open an interactive shell on the pod
  down      copy anything unsaved, then terminate the pod
"""
import argparse
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import events
import loras
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


def user_env(name):
    """An environment variable, or None. Keys live in env vars, never in files."""
    value = os.environ.get(name)
    if not value and sys.platform == "win32":
        # A user env var set after this shell started is only in the registry.
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                value = winreg.QueryValueEx(k, name)[0]
        except OSError:
            pass
    return value or None


def api_key():
    key = user_env("RUNPOD_API_KEY")
    if not key:
        sys.exit("RUNPOD_API_KEY is not set")
    return key


def lora_tokens():
    """Download keys for loras.sh, named as the script expects them."""
    keys = {"CIVITAI_TOKEN": user_env("CIVITAI_API_KEY"), "HF_TOKEN": user_env("HF_TOKEN")}
    return {k: v for k, v in keys.items() if v}


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


# ssh processes of running scripts, so background downloads can be stopped.
_scripts = set()
_scripts_stopped = threading.Event()


def ssh_script(pod, script_path, *args, secrets=None):
    """Pipe a local shell script to bash on the pod.

    `secrets` become shell variables at the top of the piped script: never in a
    command line on either machine, and not exported to the script's children.
    """
    quoted = " ".join(shlex_quote(a) for a in args)
    cmd = ssh_base(pod, "-o", "BatchMode=yes") + [f"bash -s -- {quoted}"]
    script = Path(script_path).read_bytes()
    if secrets:
        script = "".join(f"{k}={shlex_quote(v)}\n" for k, v in secrets.items()).encode() + script
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    _scripts.add(proc)
    try:
        proc.communicate(script)
    except BaseException:
        proc.kill()
        raise
    finally:
        _scripts.discard(proc)
    return proc.returncode


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
    args.gpu = gpu_type(args.gpu)
    user_data = user_data_mode(args.user_data)
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
    run_setup(pod, user_data)
    if getattr(args, "defer_downloads", False):
        return  # `start` downloads while ComfyUI is already in use
    if sets:
        rc = download_models(pod, sets)
        if rc != 0:
            sys.exit(f"Model download failed (exit {rc}); retry with: python rp.py models")
        record_models(pod, sets)
        download_loras(pod, sets)
    print("Ready. Next: python rp.py connect")


def gpu_type(name):
    """The RunPod GPU type id for a gpu_types alias (e.g. 5090) or a full id; default gpu_type."""
    if not name:
        return CONFIG["gpu_type"]
    aliases = CONFIG.get("gpu_types", {})
    return aliases.get(name, name)


def user_data_mode(mode):
    """Where ComfyUI keeps user data on the pod: "ram" (/dev/shm) or "disk"; default user_data."""
    mode = mode or CONFIG.get("user_data", "ram")
    if mode not in ("ram", "disk"):
        sys.exit(f"user_data must be ram or disk, not {mode!r}")
    return mode


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


def record_models(pod, sets):
    """Note in pod.json which model sets are on the pod (their LoRAs go along)."""
    pod["models"] = list(dict.fromkeys([*pod.get("models", []), *sets]))
    save_json(POD_FILE, pod)


def disk_gb(sets):
    """Container disk for the template, the model sets and their LoRAs; container_disk_gb is the minimum."""
    catalog = load_json(ROOT / "models.json")
    lora_gb = sum(l.get("size", 0) for l in loras.load() if l["set"] in sets and loras.enabled(l)) / 1e9
    need = sum(catalog[n].get("size_gb", 0) for n in sets) + lora_gb + CONFIG.get("disk_headroom_gb", 30)
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
        "gpuTypeIds": [args.gpu],
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
            # RunPod words this two ways: "no instances currently available" and
            # "There are no longer any instances available".
            no_capacity = re.search(r"no (longer any|instances currently) (instances )?available", detail)
            if not no_capacity or time.time() - start > wait_s:
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


def run_setup(pod, user_data):
    emit("phase", name="setup", detail=user_data)
    if CONFIG.get("comfyui_version"):
        print(f"Pinning ComfyUI {CONFIG['comfyui_version']} and installing custom nodes...")
        rc = ssh_script(pod, ROOT / "remote" / "comfyui.sh",
                        CONFIG["comfyui_version"], *CONFIG.get("custom_nodes", []))
        if rc != 0:
            sys.exit(f"ComfyUI update failed (exit {rc})")
    where = "in RAM" if user_data == "ram" else "on the pod's disk"
    print(f"Running remote setup (ComfyUI user data {where}, localhost only)...")
    rc = ssh_script(pod, ROOT / "remote" / "setup.sh", user_data, *CONFIG.get("comfyui_args", []))
    if rc != 0:
        sys.exit(f"Remote setup failed (exit {rc})")
    pod["user_data"] = user_data
    save_json(POD_FILE, pod)


def cmd_setup(args):
    pod = current_pod()
    run_setup(pod, user_data_mode(args.user_data or pod.get("user_data")))


def cmd_models(args):
    sys.exit(download_models(current_pod(), model_sets(args.sets)))


def download_models(pod, names, background=False):
    catalog = load_json(ROOT / "models.json")
    unknown = [n for n in names if n not in catalog]
    if unknown:
        sys.exit(f"Unknown model sets: {unknown}. Known: {list(catalog)}")
    # A repo maps to a list of paths (repo mirrors ComfyUI's models/ layout) or
    # to {"into": "<models subfolder>", "files": [...]} for repos that don't.
    # A "repo" may also be an https:// URL base for files not on Hugging Face.
    specs = []
    for name in names:
        for repo, entry in catalog[name]["files"].items():
            into, files = ("", entry) if isinstance(entry, list) else (entry["into"], entry["files"])
            specs.append(f"{repo}::{into}::{','.join(files)}")
    total = sum(catalog[n].get("size_gb", 0) for n in names)
    label = ", ".join(catalog[n].get("name", n) for n in names)
    print(f"Downloading {label} (~{total:.0f} GB) on the pod")
    if background:
        emit("download", state="models", detail=label)
    else:
        emit("phase", name="models", detail=label)
    # Gated repos (e.g. FLUX.2 Klein) need HF_TOKEN and the license accepted on Hugging Face.
    token = user_env("HF_TOKEN")
    return ssh_script(pod, ROOT / "remote" / "models.sh", *specs,
                      secrets={"HF_TOKEN": token} if token else None)


def download_loras(pod, sets, ids=None, background=False):
    """Download the library's switched-on LoRAs for these model sets (or just `ids`) onto the pod.

    The library is read again after each pass, so LoRAs added meanwhile (from the
    dashboard) are picked up too. Files already on the pod are skipped. Returns the
    number of LoRAs that failed; a failed LoRA doesn't stop a launch.
    """
    done, failed = set(), 0
    while True:
        todo = [l for l in loras.load() if l["id"] not in done
                and (l["id"] in ids if ids else l["set"] in sets and loras.enabled(l))]
        if not todo:
            break
        if not done:
            if background:
                emit("download", state="loras")
            else:
                emit("phase", name="loras")
        for i, lora in enumerate(todo, 1):
            done.add(lora["id"])
            print(f"[{i}/{len(todo)}] LoRA {lora['name']} -> loras/{lora['set']}/{lora['file']}", flush=True)
            emit("lora", id=lora["id"], state="downloading")
            rc = ssh_script(pod, ROOT / "remote" / "loras.sh", lora["set"], lora["file"],
                            lora["download"], lora["auth"], secrets=lora_tokens())
            emit("lora", id=lora["id"], state="done" if rc == 0 else "error")
            if rc != 0:
                failed += 1
                print(f"  LoRA {lora['name']} failed (exit {rc})", flush=True)
    if failed:
        print(f"{failed} LoRA(s) failed; retry with: python rp.py lora download", flush=True)
    return failed


def cmd_lora(args):
    catalog = load_json(ROOT / "models.json")
    if args.action == "list":
        items = loras.load()
        for key in catalog:
            mine = [l for l in items if l["set"] == key]
            if mine:
                print(f"{catalog[key].get('name', key)} ({key}):")
                for l in mine:
                    version = f" {l['version']}" if l["version"] else ""
                    off = "" if loras.enabled(l) else "  (off: not downloaded)"
                    print(f"  {l['id']}  {l['name']}{version}  -> loras/{key}/{l['file']}{off}")
        if not items:
            print("No LoRAs yet. Add one with: python rp.py lora add <set> <link>")
    elif args.action == "add":
        if len(args.args) != 2 or args.args[0] not in catalog:
            sys.exit(f"Usage: rp.py lora add <set> <link>, with <set> one of: {', '.join(catalog)}")
        set_key, link = args.args
        try:
            lora = loras.add(set_key, link, user_env("CIVITAI_API_KEY"), user_env("HF_TOKEN"))
        except loras.LoraError as e:
            sys.exit(str(e))
        print(f"Added {lora['name']} as loras/{set_key}/{lora['file']}")
        pod = load_json(POD_FILE)
        if pod and pod.get("ssh_port") and set_key in pod.get("models", []):
            sys.exit(1 if download_loras(pod, [], ids=[lora["id"]]) else 0)
        print(f"It downloads whenever {catalog[set_key].get('name', set_key)} is on the pod.")
    elif args.action == "remove":
        if not args.args:
            sys.exit("Usage: rp.py lora remove <id> [<id> ...] (ids from: rp.py lora list)")
        for lora_id in args.args:
            gone = loras.remove(lora_id)
            print(f"Removed {gone['name']}" if gone else f"No LoRA {lora_id!r}")
    elif args.action in ("on", "off"):
        if not args.args:
            sys.exit(f"Usage: rp.py lora {args.action} <id> [<id> ...] (ids from: rp.py lora list)")
        for lora_id in args.args:
            item = loras.set_enabled(lora_id, args.action == "on")
            if not item:
                print(f"No LoRA {lora_id!r}")
            elif args.action == "on":
                print(f"{item['name']} downloads with {catalog.get(item['set'], {}).get('name', item['set'])}")
            else:
                print(f"{item['name']} stays in the list but isn't downloaded")
    elif args.action == "download":
        pod = current_pod()
        known = {l["id"] for l in loras.load()}
        unknown = [i for i in args.args if i not in known]
        if unknown:
            sys.exit(f"Unknown LoRA ids: {', '.join(unknown)} (see: rp.py lora list)")
        sets = pod.get("models") or model_sets(None)
        sys.exit(1 if download_loras(pod, sets, ids=args.args or None) else 0)


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


def download_in_background(pod, sets):
    """Download model sets and their LoRAs while ComfyUI is already in use.

    ComfyUI lists files that arrive later once its model lists are refreshed (R).
    Returns the thread; stop_downloads() ends it.
    """
    emit("download", state="start")

    def run():
        rc = download_models(pod, sets, background=True)
        if _scripts_stopped.is_set():
            return
        if rc != 0:
            print(f"Model download failed (exit {rc}); retry with: python rp.py models", flush=True)
            emit("download", state="error")
            return
        record_models(pod, sets)
        download_loras(pod, sets, background=True)
        if not _scripts_stopped.is_set():
            print("Models ready. Press R in ComfyUI to refresh its model lists.", flush=True)
            emit("download", state="done")

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def stop_downloads(thread):
    """End unfinished background downloads; the next start resumes them."""
    if not thread.is_alive():
        return
    _scripts_stopped.set()
    for proc in list(_scripts):
        proc.kill()
    thread.join(10)
    print("Stopped the unfinished model downloads; the next start resumes them.", flush=True)
    emit("download", state="stopped")


def cmd_start(args):
    """Reuse the recorded pod if it's still running, otherwise launch one; then connect.

    ComfyUI opens as soon as it's set up; models and LoRAs download meanwhile.
    """
    sets = model_sets(args.models)
    pod = load_json(POD_FILE)
    if pod:
        try:
            info = api("GET", f"/pods/{pod['id']}", exit_on_error=False)
        except urllib.error.HTTPError:
            info = None
        if info and info.get("desiredStatus") == "RUNNING" and pod.get("ssh_port"):
            print(f"Reusing running pod {pod['id']}")
            emit("reuse", pod=pod["id"])
        else:
            print(f"Recorded pod {pod['id']} is gone; launching a new one")
            POD_FILE.unlink(missing_ok=True)
            pod = None
    if not pod:
        cmd_up(argparse.Namespace(gpu=args.gpu, min_ram=None, no_setup=False, no_models=False,
                                  models=args.models, user_data=args.user_data, defer_downloads=True))
    # A reused pod may have been started with other model sets; files already
    # there are skipped.
    downloads = download_in_background(current_pod(), sets)
    from sync import run_connect
    try:
        run_connect(current_pod(), CONFIG, ssh_base, ROOT, open_browser=not args.no_open)
    finally:
        stop_downloads(downloads)
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


USER_DATA_HELP = ("where ComfyUI keeps inputs, outputs, workflows and settings on the pod: "
                  "ram or disk (default: user_data in config.json)")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("up", help="launch a pod")
    up.add_argument("--gpu", help="GPU alias from gpu_types in config.json (4090, 5090, pro6000) "
                                  "or a RunPod GPU type id (default: gpu_type)")
    up.add_argument("--min-ram", type=int, help="override min_ram_gb from config.json")
    up.add_argument("--user-data", choices=["ram", "disk"], help=USER_DATA_HELP)
    up.add_argument("--no-setup", action="store_true", help="only launch; skip setup and models")
    up.add_argument("--no-models", action="store_true", help="skip downloading models")
    up.add_argument("--models", nargs="+", metavar="SET",
                    help="model sets from models.json (default: default_models in config.json)")
    up.set_defaults(func=cmd_up)
    se = sub.add_parser("setup", help="re-run the remote setup")
    se.add_argument("--user-data", choices=["ram", "disk"],
                    help="where ComfyUI keeps user data (default: as the pod was set up)")
    se.set_defaults(func=cmd_setup)
    m = sub.add_parser("models", help="download model sets onto the pod")
    m.add_argument("sets", nargs="*",
                   help="model set names from models.json (default: default_models in config.json)")
    m.set_defaults(func=cmd_models)
    lo = sub.add_parser("lora", help="LoRAs from CivitAI or Hugging Face links, per model set",
                        description="list | add <set> <link> | remove <id>... | on <id>... | off <id>... | "
                                    "download [<id>...] (default: the switched-on LoRAs of the model "
                                    "sets on the pod). LoRAs switched off stay listed but aren't downloaded.")
    lo.add_argument("action", choices=["list", "add", "remove", "on", "off", "download"])
    lo.add_argument("args", nargs="*")
    lo.set_defaults(func=cmd_lora)
    c = sub.add_parser("connect", help="tunnel to ComfyUI and sync the sync folder")
    c.add_argument("--open", action="store_true", help="open ComfyUI in the browser once reachable")
    c.set_defaults(func=cmd_connect)
    st = sub.add_parser("start", help="launch or reuse a pod, then connect and open the browser")
    st.add_argument("--models", nargs="+", metavar="SET",
                    help="model sets from models.json (default: default_models in config.json)")
    st.add_argument("--gpu", help="GPU for a new pod: alias from gpu_types in config.json "
                                  "(4090, 5090, pro6000) or a RunPod GPU type id (default: gpu_type)")
    st.add_argument("--user-data", choices=["ram", "disk"], help=USER_DATA_HELP + " (a new pod only)")
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
