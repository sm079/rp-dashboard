#!/usr/bin/env python3
"""Local web dashboard for rp.py: launch, sync and terminate the pod from a browser.

Serves http://127.0.0.1:<dashboard_port> (config.json, default 8287) and runs
rp.py commands as child processes in event mode (see events.py), streaming
their state and output to the page over server-sent events.

  python dashboard.py               start (or focus) the dashboard
  python dashboard.py --no-browser  don't open a browser tab
"""
import collections
import json
import mimetypes
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import events
import rp
from sync import KINDS, port_in_use, skipped

ROOT = rp.ROOT
WEB = ROOT / "web"
CONFIG = rp.CONFIG
MODELS = rp.load_json(ROOT / "models.json", {})
# The model sets picked last time, preselected on the next start.
SELECTION_FILE = rp.STATE_DIR / "dashboard.json"
PORT = CONFIG.get("dashboard_port", 8287)
COMFY_PORT = CONFIG.get("local_port", 8288)
SYNC_DIR = (ROOT / CONFIG.get("sync_dir", "sync")).resolve()
OUTPUTS = SYNC_DIR / "outputs"
IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
VIDEOS = {".mp4", ".webm", ".mov"}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# Seconds a stopping child gets to finish cleanly before its process tree is killed.
STOP_GRACE = 15
ERROR_WORDS = re.compile(r"\b(fail(ed|ure)?|error|refused|lost|denied|NOT deleting|Traceback)\b", re.I)
MODEL_STEP = re.compile(r"^\[(\d+)/(\d+)\]\s+(\S+)")


def python_exe():
    """python.exe, also when the dashboard itself runs under pythonw.exe."""
    exe = Path(sys.executable)
    if exe.stem.lower() == "pythonw":
        console = exe.with_name("python" + exe.suffix)
        if console.exists():
            return str(console)
    return str(exe)


def kill_tree(pid):
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       capture_output=True, creationflags=NO_WINDOW)
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


class Hub:
    """Fan-out of server-sent events to every open page, plus a log backlog."""

    def __init__(self):
        self.lock = threading.Lock()
        self.clients = set()
        self.log = collections.deque(maxlen=400)

    def subscribe(self):
        q = queue.Queue(maxsize=2000)
        with self.lock:
            self.clients.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.clients.discard(q)

    def send(self, event, data):
        msg = f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()
        with self.lock:
            for q in self.clients:
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    pass

    def add_log(self, text, level=None, replace=False):
        if level is None:
            stripped = text.strip()
            if ERROR_WORDS.search(stripped):
                level = "error"
            elif stripped.startswith(("saved", "Ready", "SSH ready", "Reusing")) or stripped.endswith("terminated."):
                level = "ok"
            elif stripped.startswith(("sent", "deleted", "Uploaded")):
                level = "sync"
            else:
                level = "info"
        entry = {"t": time.time(), "text": text.rstrip(), "level": level, "replace": replace}
        with self.lock:
            if replace and self.log and self.log[-1]["replace"]:
                self.log[-1] = entry
            else:
                self.log.append(entry)
        self.send("log", entry)

    def backlog(self):
        with self.lock:
            return list(self.log)


HUB = Hub()


class Job:
    """One rp.py child process run in event mode."""

    def __init__(self, ctl, name, args, reusing=False):
        self.ctl, self.name, self.args, self.reusing = ctl, name, args, reusing
        self.phase, self.detail = None, ""
        self.models = None
        self.started = time.time()
        self.stopping = False
        self.then = None          # follow-up after exit: ("down", no_sync) or ("cleanup",)
        self.last_line = ""
        self.last_transient = 0.0
        env = {**os.environ, "RP_EVENTS": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        HUB.add_log(f"$ rp.py {' '.join(args)}", "cmd")
        self.proc = subprocess.Popen(
            [python_exe(), "-u", str(ROOT / "rp.py"), *args], cwd=ROOT, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=NO_WINDOW)
        threading.Thread(target=self.read, daemon=True).start()

    def read(self):
        buf = b""
        out = self.proc.stdout
        while True:
            chunk = out.read1(65536)
            if not chunk:
                break
            buf += chunk
            # Lines end in \n, or in \r for in-place progress (hf download bars).
            while True:
                m = re.search(rb"\r\n|\n|\r", buf)
                if not m or (m.group() == b"\r" and m.end() == len(buf)):
                    break  # a lone \r at the end may be half of \r\n
                line, sep, buf = buf[:m.start()], m.group(), buf[m.end():]
                self.line(line.decode("utf-8", "replace"), transient=sep == b"\r")
        if buf:
            self.line(buf.decode("utf-8", "replace"), transient=False)
        code = self.proc.wait()
        self.ctl.job_finished(self, code)

    def line(self, text, transient):
        if text.startswith(events.PREFIX):
            try:
                self.event(json.loads(text[len(events.PREFIX):]))
            except ValueError:
                pass
            return
        if not text.strip():
            return
        if transient:
            now = time.time()
            if now - self.last_transient < 0.25:
                return
            self.last_transient = now
        else:
            self.last_line = text.strip()
        m = MODEL_STEP.match(text.strip())
        if m and self.phase == "models":
            self.models = {"i": int(m.group(1)), "n": int(m.group(2)), "repo": m.group(3)}
            self.ctl.changed()
        HUB.add_log(text, replace=transient)

    def event(self, ev):
        kind = ev.get("type")
        if kind == "phase":
            self.phase, self.detail = ev.get("name"), ev.get("detail", "")
            self.ctl.changed()
            if self.phase == "live":
                self.ctl.poke_comfy()
        elif kind == "reuse":
            self.reusing = True
            self.ctl.changed()
        elif kind == "transfer":
            self.ctl.on_transfer(ev)
        elif kind == "heartbeat":
            HUB.send("heartbeat", {"t": time.time()})

    def stop(self):
        """Ask the child to stop as if Ctrl+C was pressed; kill it if it hangs."""
        if self.stopping:
            return
        self.stopping = True
        try:
            self.proc.stdin.write(b"stop\n")
            self.proc.stdin.close()
        except OSError:
            pass

        def deadline():
            if self.proc.poll() is None:
                HUB.add_log("Still running; forcing it to stop.", "error")
                kill_tree(self.proc.pid)
        threading.Timer(STOP_GRACE, deadline).start()

    def info(self):
        return {"name": self.name, "phase": self.phase, "detail": self.detail,
                "models": self.models, "started": self.started, "reusing": self.reusing,
                "stopping": self.stopping}


class Controller:
    def __init__(self):
        self.lock = threading.RLock()
        self.job = None
        self.pod_info = None       # last API view of the recorded pod
        self.error = None          # {"message", "action"} from the last failed job
        self.comfy_up = False
        self.external_sync = False
        self.transfer = None
        saved = (rp.load_json(SELECTION_FILE) or {}).get("models")
        self.models = [m for m in saved or CONFIG.get("default_models", []) if m in MODELS]
        self.files = {"folders": {}, "outputs": [], "today": 0}
        self.wake = threading.Event()
        self.rescan = threading.Event()

    # --- state ------------------------------------------------------------

    def state_name(self):
        job = self.job
        if job and job.name == "down":
            return "stopping"
        if job and job.name == "start":
            if job.stopping and job.then:
                return "stopping"
            return "live" if job.phase == "live" else "launching"
        pod = rp.load_json(rp.POD_FILE)
        if pod and not (self.pod_info or {}).get("gone"):
            return "paused"
        return "idle"

    def snapshot(self):
        with self.lock:
            pod = rp.load_json(rp.POD_FILE)
            pod_view = None
            if pod:
                pod_view = {"id": pod["id"], "created": pod.get("created"),
                            "ready": bool(pod.get("ssh_port")), **(self.pod_info or {})}
            return {
                "state": self.state_name(),
                "job": self.job.info() if self.job else None,
                "pod": pod_view,
                "comfy": self.comfy_up,
                "comfy_url": f"http://localhost:{COMFY_PORT}/",
                "external_sync": self.external_sync,
                "error": self.error,
                "models": self.models,
                "catalog": [{"key": k, "name": m.get("name", k), "description": m.get("description", ""),
                             "size_gb": m.get("size_gb", 0)} for k, m in MODELS.items()],
                "gpu": CONFIG.get("gpu_type", ""),
                "transfer": self.transfer,
                **self.files,
            }

    def changed(self):
        HUB.send("state", self.snapshot())

    # --- actions ----------------------------------------------------------

    def start(self, models):
        with self.lock:
            if self.job:
                return "Something is already running."
            if not isinstance(models, list) or not models:
                return "Pick at least one model."
            unknown = [str(m) for m in models if not isinstance(m, str) or m not in MODELS]
            if unknown:
                return f"Unknown model sets: {', '.join(unknown)}."
            if port_in_use(COMFY_PORT):
                return (f"Port {COMFY_PORT} is in use, probably by a sync running in a terminal. "
                        "Stop that one first.")
            pod = rp.load_json(rp.POD_FILE)
            reusing = bool(pod and pod.get("ssh_port") and (self.pod_info or {}).get("status") == "RUNNING")
            self.models = list(dict.fromkeys(models))
            self.error, self.transfer, self.comfy_up = None, None, False
            rp.save_json(SELECTION_FILE, {"models": self.models})
            args = ["start", "--models", *self.models] + (["--no-open"] if reusing else [])
            self.job = Job(self, "start", args, reusing=reusing)
        self.changed()
        self.wake.set()

    def stop(self):
        """Stop syncing, or cancel a launch (and terminate the half-made pod)."""
        with self.lock:
            job = self.job
            if not job or job.name != "start":
                return "Nothing to stop."
            if job.phase not in ("connect", "live") and not job.reusing:
                job.then = ("cleanup",)
            job.stop()
        self.changed()

    def terminate(self, no_sync=False):
        with self.lock:
            job = self.job
            if job and job.name == "down":
                return "Already terminating."
            if job:
                job.then = ("down", no_sync)
                job.stop()
            elif not rp.load_json(rp.POD_FILE):
                return "No pod to terminate."
            elif (self.pod_info or {}).get("gone"):
                # Already terminated elsewhere (e.g. the RunPod console): just forget it.
                rp.POD_FILE.unlink(missing_ok=True)
                self.pod_info = None
            else:
                self.run_down(no_sync)
        self.changed()

    def run_down(self, no_sync):
        self.error, self.transfer, self.comfy_up = None, None, False
        args = ["down", "-y"] + (["--no-sync"] if no_sync else [])
        self.job = Job(self, "down", args)

    def job_finished(self, job, code):
        with self.lock:
            if self.job is not job:
                return
            self.job = None
            self.transfer = None
            self.comfy_up = False
            if code != 0 and not job.stopping:
                message = job.last_line or f"rp.py {job.name} exited with code {code}"
                action = "force_down" if job.name == "down" else None
                self.error = {"message": message, "action": action}
                HUB.add_log(f"{job.name} failed (exit {code})", "error")
            elif job.name == "down" and code == 0:
                self.pod_info = None
                HUB.add_log("Pod terminated. Nothing is billing.", "ok")
            elif job.name == "start":
                HUB.add_log("Sync stopped." if job.phase == "live" else "Launch cancelled.", "info")
            follow = job.then
            if follow and rp.load_json(rp.POD_FILE):
                if follow[0] == "down":
                    self.run_down(follow[1])
                elif follow[0] == "cleanup":
                    HUB.add_log("Terminating the unfinished pod.", "info")
                    self.run_down(True)
        self.rescan.set()
        self.wake.set()
        self.changed()

    def on_transfer(self, ev):
        with self.lock:
            self.transfer = None if ev.get("final") else ev
        HUB.send("transfer", ev)
        if ev.get("final"):
            self.rescan.set()

    def poke_comfy(self):
        threading.Thread(target=self.check_comfy, daemon=True).start()

    # --- background pollers -----------------------------------------------

    def check_comfy(self):
        up = False
        job = self.job
        if job and job.phase == "live":
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{COMFY_PORT}/", timeout=2) as r:
                    r.read()
                up = True
            except urllib.error.HTTPError:
                up = True
            except OSError:
                up = False
        if up != self.comfy_up:
            self.comfy_up = up
            if up:
                HUB.add_log(f"ComfyUI is reachable at http://localhost:{COMFY_PORT}", "ok")
            self.changed()

    def poll_pod(self):
        """Refresh the API view of the recorded pod."""
        while True:
            pod = rp.load_json(rp.POD_FILE)
            info = None
            if pod:
                try:
                    raw = rp.api("GET", f"/pods/{pod['id']}", exit_on_error=False)
                    info = pod_view(raw)
                except urllib.error.HTTPError as e:
                    info = {"gone": True} if e.code == 404 else self.pod_info
                except (OSError, ValueError, SystemExit) as e:
                    info = {**(self.pod_info or {}), "api_error": str(e)}
            external = not self.job and port_in_use(COMFY_PORT)
            if info != self.pod_info or external != self.external_sync:
                self.pod_info, self.external_sync = info, external
                self.changed()
            busy = self.job and self.job.name == "start" and self.job.phase != "live"
            self.wake.wait(5 if busy else 15)
            self.wake.clear()

    def poll_comfy(self):
        while True:
            self.check_comfy()
            time.sleep(3)

    def poll_files(self):
        while True:
            files = scan_files()
            if files != self.files:
                with self.lock:
                    self.files = files
                self.changed()
            self.rescan.wait(10)
            self.rescan.clear()


def pod_view(raw):
    raw = raw or {}
    gpu = raw.get("gpu") or {}
    machine = raw.get("machine") or {}
    return {
        "status": raw.get("desiredStatus"),
        "cost_per_hr": raw.get("adjustedCostPerHr") or raw.get("costPerHr"),
        "gpu_name": gpu.get("displayName") or machine.get("gpuDisplayName") or gpu.get("id"),
        "ram_gb": raw.get("memoryInGb"),
        "vcpus": raw.get("vcpuCount"),
        "location": machine.get("location") or machine.get("dataCenterId") or raw.get("dataCenterId"),
        "started_at": raw.get("lastStartedAt"),
    }


def scan_files():
    folders = {}
    for kind, (_, _, sub, direction) in KINDS.items():
        root = SYNC_DIR / sub
        n = 0
        if root.is_dir():
            n = sum(1 for p in root.rglob("*") if p.is_file() and not skipped(p.name))
        folders[sub] = {"count": n, "direction": direction}
    outputs = []
    if OUTPUTS.is_dir():
        for p in OUTPUTS.rglob("*"):
            if p.is_file() and p.suffix.lower() in IMAGES | VIDEOS and not skipped(p.name):
                st = p.stat()
                outputs.append({"path": p.relative_to(OUTPUTS).as_posix(), "name": p.name,
                                "kind": "video" if p.suffix.lower() in VIDEOS else "image",
                                "mtime": st.st_mtime, "size": st.st_size})
    outputs.sort(key=lambda o: o["mtime"], reverse=True)
    today = time.strftime("%Y-%m-%d")
    n_today = sum(1 for o in outputs if o["path"].startswith(today + "/"))
    return {"folders": folders, "outputs": outputs[:12], "today": n_today}


CTL = Controller()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "runpod-helper"

    def log_message(self, *args):  # pythonw has no stderr
        pass

    def allowed(self):
        """Only this machine's pages: blocks DNS rebinding and cross-site posts."""
        hosts = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
        if self.headers.get("Host") not in hosts:
            return False
        origin = self.headers.get("Origin")
        return origin is None or urlparse(origin).netloc in hosts

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, cache="no-cache"):
        size = path.stat().st_size
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        start, end = 0, size - 1
        rng = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range", ""))
        if rng and size:
            if rng.group(1):
                start = int(rng.group(1))
                end = int(rng.group(2)) if rng.group(2) else size - 1
            else:
                start = max(size - int(rng.group(2) or 0), 0)
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        length = end - start + 1 if size else 0
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", cache)
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def do_GET(self):
        if not self.allowed():
            return self.send_error(403)
        path = urlparse(self.path).path
        if path == "/api/state":
            return self.send_json(CTL.snapshot())
        if path == "/api/events":
            return self.stream()
        if path.startswith("/media/"):
            target = (OUTPUTS / unquote(path[len("/media/"):])).resolve()
            if target.is_relative_to(OUTPUTS) and target.is_file():
                return self.send_file(target, cache="max-age=3600")
            return self.send_error(404)
        name = "index.html" if path == "/" else path.lstrip("/")
        target = (WEB / unquote(name)).resolve()
        if target.is_relative_to(WEB) and target.is_file():
            return self.send_file(target)
        self.send_error(404)

    def do_POST(self):
        if not self.allowed() or self.headers.get("Content-Type") != "application/json":
            return self.send_error(403)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self.send_error(400)
        path = urlparse(self.path).path
        if path == "/api/start":
            err = CTL.start(body.get("models", CTL.models))
        elif path == "/api/stop":
            err = CTL.stop()
        elif path == "/api/terminate":
            err = CTL.terminate(bool(body.get("no_sync")))
        elif path == "/api/dismiss":
            CTL.error = None
            CTL.changed()
            err = None
        elif path == "/api/open-folder":
            SYNC_DIR.mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(SYNC_DIR)  # noqa: S606 - local folder, fixed path
            else:
                webbrowser.open(SYNC_DIR.as_uri())
            err = None
        elif path == "/api/quit":
            self.send_json({"ok": True})
            threading.Thread(target=quit_dashboard, args=(self.server,), daemon=True).start()
            return
        else:
            return self.send_error(404)
        if err:
            return self.send_json({"ok": False, "error": err}, 409)
        self.send_json({"ok": True})

    def stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = HUB.subscribe()
        try:
            hello = (f"event: state\ndata: {json.dumps(CTL.snapshot())}\n\n"
                     f"event: logs\ndata: {json.dumps(HUB.backlog())}\n\n")
            self.wfile.write(hello.encode())
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=15)
                except queue.Empty:
                    msg = b": ping\n\n"
                self.wfile.write(msg)
                self.wfile.flush()
        except OSError:
            pass
        finally:
            HUB.unsubscribe(q)
            self.close_connection = True


def quit_dashboard(server):
    """Stop syncing cleanly (the pod keeps running), then shut down."""
    job = CTL.job
    if job:
        job.then = None
        job.stop()
        try:
            job.proc.wait(timeout=STOP_GRACE + 5)
        except subprocess.TimeoutExpired:
            pass
    HUB.send("bye", {})
    time.sleep(0.3)
    server.shutdown()


def main():
    url = f"http://localhost:{PORT}/"
    if port_in_use(PORT):
        # Already running: just bring it up.
        webbrowser.open(url)
        return
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    for target in (CTL.poll_pod, CTL.poll_comfy, CTL.poll_files):
        threading.Thread(target=target, daemon=True).start()
    HUB.add_log(f"Dashboard ready at {url}", "info")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.4, webbrowser.open, [url]).start()
    print(f"Dashboard: {url}  (Ctrl+C to quit; a running pod keeps running)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if CTL.job:
            CTL.job.then = None
            CTL.job.stop()


if __name__ == "__main__":
    main()
