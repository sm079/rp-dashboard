"""SSH tunnel to ComfyUI plus copying outputs and saved workflows home.

- Outputs land in <outputs_dir>/<YYYY-MM-DD>/<path inside ComfyUI's output dir>,
  dated by the local day they were copied. Existing files are never overwritten.
- Saved workflows are mirrored to <workflows_dir>/ (latest version wins) and
  pushed back to the pod by `rp.py up` / `rp.py setup`.
"""
import datetime
import io
import json
import queue
import re
import subprocess
import tarfile
import threading
import time
from pathlib import Path, PurePosixPath

RAM = "/dev/shm/comfy"
REMOTE_DIRS = {"o": f"{RAM}/output", "w": f"{RAM}/user/default/workflows"}
POLL_SECONDS = 3
END_MARK = "__END_OF_LISTING__"
UNSAFE = re.compile(r'[<>:"\\|?*\x00-\x1f]')

# Prints "<kind>\t<relpath>\t<size>\t<mtime>" for each file, then END_MARK.
LIST_CMD = " ".join(
    f"find -L {d} -type f -printf '{k}\\t%P\\t%s\\t%T@\\n' 2>/dev/null;" for k, d in REMOTE_DIRS.items()
) + f" echo {END_MARK};"
# Runs on the pod for the lifetime of the connection.
WATCH_CMD = f"while :; do {LIST_CMD} sleep {POLL_SECONDS}; done"


def parse_listing(lines):
    """Return {(kind, relpath): (size, mtime)}."""
    files = {}
    for line in lines:
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        kind, rel, size, mtime = parts
        name = PurePosixPath(rel).name
        if kind not in REMOTE_DIRS or name.startswith(".") or name.endswith((".tmp", ".part")):
            continue
        files[(kind, rel)] = (int(size), mtime)
    return files


def safe_parts(rel):
    return [UNSAFE.sub("_", p) for p in PurePosixPath(rel).parts if p not in ("", ".", "..")]


def output_target(out_root, rel):
    """Dated, non-clobbering local path for an output file."""
    dest = Path(out_root, datetime.date.today().isoformat(), *safe_parts(rel))
    stem, suffix, n = dest.stem, dest.suffix, 1
    while dest.exists():
        dest = dest.with_name(f"{stem}_{n}{suffix}")
        n += 1
    return dest


def fetch(pod, ssh_base, remote_dir, rels, target):
    """Stream files from remote_dir as a tar; write each to target(relpath)."""
    quoted = " ".join("'" + r.replace("'", "'\"'\"'") + "'" for r in rels)
    cmd = ssh_base(pod, "-o", "BatchMode=yes") + [f"tar -C '{remote_dir}' -cf - -- {quoted}"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    written = []
    with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
        for member in tar:
            if not member.isfile():
                continue
            src = tar.extractfile(member)
            dest = target(member.name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".partial")
            with open(tmp, "wb") as f:
                while chunk := src.read(1 << 20):
                    f.write(chunk)
            tmp.replace(dest)
            written.append((member.name, dest))
    proc.wait()
    return written


def push_workflows(pod, ssh_base, wf_root):
    """Upload local saved workflows into the pod's RAM user dir."""
    files = [p for p in Path(wf_root).rglob("*.json")] if Path(wf_root).is_dir() else []
    if not files:
        return 0
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for p in files:
            tar.add(p, arcname=p.relative_to(wf_root).as_posix())
    remote = REMOTE_DIRS["w"]
    cmd = ssh_base(pod, "-o", "BatchMode=yes") + [f"mkdir -p '{remote}' && tar -C '{remote}' -xf -"]
    subprocess.run(cmd, input=buf.getvalue(), check=True)
    return len(files)


class Syncer:
    def __init__(self, pod, config, ssh_base, root):
        self.pod, self.ssh_base = pod, ssh_base
        self.out_root = (root / config.get("outputs_dir", "outputs")).resolve()
        self.wf_root = (root / config.get("workflows_dir", "workflows")).resolve()
        self.state_file = root / ".state" / "synced.json"
        try:
            self.synced = set(json.loads(self.state_file.read_text()).get(pod["id"], []))
        except (FileNotFoundError, ValueError):
            self.synced = set()

    def save_state(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps({self.pod["id"]: sorted(self.synced)}))

    def copy(self, keys, files):
        for kind in REMOTE_DIRS:
            rels = [rel for k, rel in keys if k == kind]
            if not rels:
                continue
            if kind == "o":
                target = lambda rel: output_target(self.out_root, rel)
            else:
                target = lambda rel: Path(self.wf_root, *safe_parts(rel))
            for rel, dest in fetch(self.pod, self.ssh_base, REMOTE_DIRS[kind], rels, target):
                size, mtime = files[(kind, rel)]
                self.synced.add(f"{kind}|{rel}|{size}|{mtime}")
                base = self.out_root if kind == "o" else self.wf_root.parent
                print(f"  saved {dest.relative_to(base)}", flush=True)
        self.save_state()

    def pending(self, files):
        return [key for key, (size, mtime) in files.items()
                if f"{key[0]}|{key[1]}|{size}|{mtime}" not in self.synced]


def start_session(pod, ssh_base, local_port):
    """One SSH connection that carries the ComfyUI tunnel and the file watcher."""
    cmd = ssh_base(pod, "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
                   "-L", f"127.0.0.1:{local_port}:127.0.0.1:8188") + [WATCH_CMD]
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True)
    listings = queue.Queue()

    def reader():
        block = []
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line == END_MARK:
                listings.put(parse_listing(block))
                block = []
            else:
                block.append(line)

    threading.Thread(target=reader, daemon=True).start()
    return proc, listings


def run_connect(pod, config, ssh_base, root):
    syncer = Syncer(pod, config, ssh_base, root)
    local_port = config.get("local_port", 8188)
    session, listings = start_session(pod, ssh_base, local_port)
    print(f"ComfyUI:   http://localhost:{local_port}")
    print(f"Outputs:   {syncer.out_root}\\<date>\\")
    print(f"Workflows: {syncer.wf_root}\\")
    print("Ctrl+C to stop syncing; the pod keeps running.", flush=True)
    seen = {}
    try:
        while True:
            if session.poll() is not None:
                print("Connection lost; reconnecting...", flush=True)
                time.sleep(3)
                session, listings = start_session(pod, ssh_base, local_port)
                continue
            try:
                files = listings.get(timeout=POLL_SECONDS * 3)
            except queue.Empty:
                continue
            # Copy a file once it looks the same on two consecutive polls,
            # so half-written videos aren't fetched.
            ready = [key for key in syncer.pending(files) if seen.get(key) == files[key]]
            seen = files
            if ready:
                syncer.copy(ready, files)
    except KeyboardInterrupt:
        print("\nStopped. The pod is still running (python rp.py down to terminate).")
    finally:
        session.terminate()
        syncer.save_state()


def sync_once(pod, config, ssh_base, root):
    """Copy everything not yet copied. Returns the number of files copied."""
    syncer = Syncer(pod, config, ssh_base, root)
    cmd = ssh_base(pod, "-o", "BatchMode=yes") + [LIST_CMD]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"could not list pod files: {res.stderr.strip()}")
    files = parse_listing(res.stdout.splitlines())
    todo = syncer.pending(files)
    if todo:
        syncer.copy(todo, files)
    return len(todo)
