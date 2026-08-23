"""SSH tunnel to ComfyUI plus syncing a local folder with the pod's RAM.

Local layout (<sync_dir>/...):
  outputs/    pod -> local, in <YYYY-MM-DD>/ folders by the day they were copied;
              existing files are never overwritten
  inputs/     both ways (ComfyUI's input folder)
  workflows/  both ways (saved workflows)
  settings/   both ways (comfy.settings.json, keybindings)
  loras/      local -> pod (an extra LoRA folder in the pod's RAM)

Both-way folders: latest change wins, and deletions are mirrored. Deleting a
local LoRA removes it from the pod. Outputs are never deleted on either side:
the local copies are the archive.
On connect, local files are uploaded before anything is pulled, so a fresh pod
picks up local workflows/settings instead of overwriting them with defaults.

A pod restart wipes its RAM, which would look like every file was deleted.
The pod's RAM holds a random session id; when it changes (or is missing),
nothing is deleted locally and local files are uploaded again instead.
"""
import datetime
import json
import queue
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path, PurePosixPath

RAM = "/dev/shm/comfy"
# kind -> (remote dir, extra find args, local subfolder, direction)
KINDS = {
    "o": (f"{RAM}/output", "", "outputs", "pull"),
    "i": (f"{RAM}/input", "", "inputs", "both"),
    "w": (f"{RAM}/user/default/workflows", "", "workflows", "both"),
    # Top-level files only: comfy.settings.json, comfy.keybindings.json, ...
    "s": (f"{RAM}/user/default", "-maxdepth 1", "settings", "both"),
    "l": (f"{RAM}/loras", "", "loras", "push"),
}
PULLED = [k for k, v in KINDS.items() if v[3] in ("pull", "both")]
PUSHED = [k for k, v in KINDS.items() if v[3] in ("push", "both")]
POLL_SECONDS = 3
END_MARK = "__END_OF_LISTING__"
UNSAFE = re.compile(r'[<>:"\\|?*\x00-\x1f]')
SKIP_SUFFIXES = (".tmp", ".part", ".partial")
SESSION_FILE = f"{RAM}/.session-id"
# Refuse to mirror more pod-side deletions than this in one go (per folder),
# unless they are at most half of that folder's files.
MASS_DELETE = 3

# Prints "S\t<session id>", then "<kind>\t<relpath>\t<size>\t<mtime>" for each
# pulled file, then END_MARK.
LIST_CMD = f"printf 'S\\t%s\\n' \"$(cat {SESSION_FILE} 2>/dev/null || echo none)\"; " + " ".join(
    f"find -L {KINDS[k][0]} {KINDS[k][1]} -type f -printf '{k}\\t%P\\t%s\\t%T@\\n' 2>/dev/null;"
    for k in PULLED
) + f" echo {END_MARK};"
# Runs on the pod for the lifetime of the connection.
WATCH_CMD = f"while :; do {LIST_CMD} sleep {POLL_SECONDS}; done"


def skipped(name):
    return name.startswith(".") or name.endswith(SKIP_SUFFIXES)


def parse_listing(lines):
    """Return ({(kind, relpath): (size, mtime)}, session id)."""
    files, session = {}, None
    for line in lines:
        parts = line.split("\t")
        if len(parts) == 2 and parts[0] == "S":
            session = parts[1]
            continue
        if len(parts) != 4:
            continue
        kind, rel, size, mtime = parts
        if kind not in KINDS or skipped(PurePosixPath(rel).name):
            continue
        files[(kind, rel)] = (int(size), mtime)
    return files, session


def safe_parts(rel):
    return [UNSAFE.sub("_", p) for p in PurePosixPath(rel).parts if p not in ("", ".", "..")]


def quote(s):
    return "'" + s.replace("'", "'\"'\"'") + "'"


def output_target(out_root, rel):
    """Dated, non-clobbering local path for an output file."""
    dest = Path(out_root, datetime.date.today().isoformat(), *safe_parts(rel))
    stem, suffix, n = dest.stem, dest.suffix, 1
    while dest.exists():
        dest = dest.with_name(f"{stem}_{n}{suffix}")
        n += 1
    return dest


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


class Progress:
    """Single-line transfer progress: bar, percent, bytes, speed, current file."""

    def __init__(self, label, total_bytes, total_files):
        self.label, self.total, self.files = label, max(total_bytes, 1), total_files
        self.done_bytes, self.done_files, self.name = 0, 0, ""
        self.start = self.last_draw = time.time()
        self.tty = sys.stdout.isatty()

    def file(self, name):
        self.name = name

    def add(self, n):
        self.done_bytes += n
        if time.time() - self.last_draw >= 0.1:
            self.draw()

    def file_done(self):
        self.done_files += 1
        self.draw()

    def draw(self, final=False):
        self.last_draw = time.time()
        if not self.tty and not final:
            return
        frac = min(self.done_bytes / self.total, 1.0)
        width = 24
        bar = "#" * int(frac * width) + "-" * (width - int(frac * width))
        speed = self.done_bytes / max(time.time() - self.start, 1e-6)
        text = (f"  {self.label} [{bar}] {frac:4.0%}  {human(self.done_bytes)}/{human(self.total)}"
                f"  {human(speed)}/s  {self.done_files}/{self.files}  {self.name}")
        cols = shutil.get_terminal_size((100, 20)).columns - 1
        text = text[:cols]
        if self.tty:
            end = "\n" if final else ""
            sys.stdout.write("\r" + text.ljust(cols) + end)
        else:
            sys.stdout.write(text + "\n")
        sys.stdout.flush()

    def finish(self):
        self.name = ""
        self.draw(final=True)


class CountingReader:
    """File wrapper that reports bytes read, for upload progress."""

    def __init__(self, f, on_read):
        self.f, self.on_read = f, on_read

    def read(self, n=-1):
        data = self.f.read(n)
        self.on_read(len(data))
        return data


def fetch(pod, ssh_base, remote_dir, rels, target, progress=None):
    """Stream files from remote_dir as a tar; write each to target(relpath)."""
    cmd = ssh_base(pod, "-o", "BatchMode=yes") + [
        f"tar -C {quote(remote_dir)} -cf - -- " + " ".join(quote(r) for r in rels)]
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
            if progress:
                progress.file(PurePosixPath(member.name).name)
            with open(tmp, "wb") as f:
                while chunk := src.read(1 << 20):
                    f.write(chunk)
                    if progress:
                        progress.add(len(chunk))
            tmp.replace(dest)
            written.append((member.name, dest))
            if progress:
                progress.file_done()
    # tarfile stops at the end-of-archive marker; drain tar's block padding or
    # ssh blocks writing it into a full pipe and never exits.
    proc.stdout.read()
    proc.wait()
    return written


def upload(pod, ssh_base, remote_dir, local_root, paths, progress=None):
    """Stream local files to remote_dir as a tar."""
    cmd = ssh_base(pod, "-o", "BatchMode=yes") + [
        # Extract into a staging dir, then move each file into place (same
        # tmpfs, so the move is atomic): ComfyUI never sees a partial file and
        # an interrupted upload leaves nothing behind.
        f"set -e; dest={quote(remote_dir)}; mkdir -p \"$dest\" {RAM}/.incoming; "
        f"tmp=$(mktemp -d {RAM}/.incoming/up.XXXXXX); trap 'rm -rf \"$tmp\"' EXIT; "
        "tar -C \"$tmp\" -xf -; cd \"$tmp\"; "
        "find . -type f | while IFS= read -r f; do "
        "mkdir -p \"$dest/$(dirname \"$f\")\"; mv -f \"$f\" \"$dest/$f\"; done"]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    with tarfile.open(fileobj=proc.stdin, mode="w|") as tar:
        for p in paths:
            info = tar.gettarinfo(p, arcname=p.relative_to(local_root).as_posix())
            if progress:
                progress.file(p.name)
            with open(p, "rb") as f:
                tar.addfile(info, CountingReader(f, progress.add) if progress else f)
            if progress:
                progress.file_done()
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError(f"upload to {remote_dir} failed")


def local_sig(path):
    st = path.stat()
    return [st.st_size, st.st_mtime_ns]


class Syncer:
    def __init__(self, pod, config, ssh_base, root):
        self.pod, self.ssh_base = pod, ssh_base
        base = (Path(root) / config.get("sync_dir", "sync")).resolve()
        self.base = base
        self.roots = {kind: base / KINDS[kind][2] for kind in KINDS}
        for path in self.roots.values():
            path.mkdir(parents=True, exist_ok=True)
        self.state_file = Path(root) / ".state" / "sync-state.json"
        state = {}
        try:
            saved = json.loads(self.state_file.read_text())
            if saved.get("pod") == pod["id"]:
                state = saved
        except (FileNotFoundError, ValueError):
            pass
        # Remote versions already handled: "kind|rel|size|mtime".
        self.pulled = set(state.get("pulled", []))
        # Local versions already in sync with the pod: "kind|rel" -> [size, mtime_ns].
        self.local = state.get("local", {})
        # The pod RAM session these records belong to.
        self.session = state.get("session")
        # Remote paths we just uploaded; their next listed version is ours.
        self.acks = set()
        self.last_scan = {}
        self.warned = set()

    def save_state(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(
            {"pod": self.pod["id"], "session": self.session,
             "pulled": sorted(self.pulled), "local": self.local}))

    def ensure_session(self):
        """Make sure the pod has a RAM session id; reset records if it's new.

        Returns True if the records were reset (fresh or restarted pod RAM)."""
        cmd = self.ssh_base(self.pod, "-o", "BatchMode=yes") + [
            f"[ -s {SESSION_FILE} ] || cat /proc/sys/kernel/random/uuid > {SESSION_FILE}; "
            f"cat {SESSION_FILE}"]
        current = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()
        if current == self.session:
            return False
        if self.session is None and self.local:
            # Records from before session ids existed, for this same pod: adopt.
            self.session = current
            self.save_state()
            return False
        if self.session is not None:
            print("Pod RAM was reset (pod restarted); re-uploading local files, "
                  "deleting nothing locally.", flush=True)
        self.session = current
        self.pulled, self.local, self.acks, self.last_scan = set(), {}, set(), {}
        self.save_state()
        return True

    # --- deletions --------------------------------------------------------

    def apply_deletions(self, files, prev_files):
        """Mirror deletions of files that were in sync on both sides.

        A pod-side deletion counts only if the file is missing from two
        consecutive listings, wasn't just uploaded, and is unchanged locally."""
        scan = self.scan_local()
        local_gone, remote_gone = {}, {}
        for key, sig in list(self.local.items()):
            kind, rel = key.split("|", 1)
            if kind not in KINDS or KINDS[kind][3] == "pull":
                continue
            if (kind, rel) not in scan:
                if self.roots[kind].is_dir():
                    local_gone.setdefault(kind, []).append(rel)
            elif (KINDS[kind][3] == "both" and (kind, rel) not in files
                  and (kind, rel) not in prev_files and (kind, rel) not in self.acks
                  and scan[(kind, rel)] == sig):
                remote_gone.setdefault(kind, []).append(rel)

        changed = False
        for kind, rels in local_gone.items():
            cmd = self.ssh_base(self.pod, "-o", "BatchMode=yes") + [
                f"cd {quote(KINDS[kind][0])} && rm -f -- " + " ".join(quote(r) for r in rels)]
            if subprocess.run(cmd, capture_output=True).returncode != 0:
                continue
            for rel in rels:
                self.local.pop(f"{kind}|{rel}", None)
                print(f"  deleted on pod  {KINDS[kind][2]}/{rel}", flush=True)
            changed = True

        for kind, rels in remote_gone.items():
            tracked = sum(1 for k in self.local if k.startswith(kind + "|"))
            if len(rels) > MASS_DELETE and len(rels) > tracked / 2:
                if kind not in self.warned:
                    print(f"  NOT deleting {len(rels)} local {KINDS[kind][2]} files that vanished "
                          "from the pod at once; delete them by hand if that was intended.",
                          flush=True)
                    self.warned.add(kind)
                continue
            for rel in rels:
                Path(self.roots[kind], *safe_parts(rel)).unlink(missing_ok=True)
                self.local.pop(f"{kind}|{rel}", None)
                print(f"  deleted locally {KINDS[kind][2]}/{rel}", flush=True)
            changed = True
        if changed:
            self.save_state()

    # --- pod -> local ----------------------------------------------------

    def pending_pulls(self, files):
        todo = []
        for (kind, rel), (size, mtime) in files.items():
            key = f"{kind}|{rel}|{size}|{mtime}"
            if key in self.pulled:
                continue
            if (kind, rel) in self.acks:
                self.acks.discard((kind, rel))
                self.pulled.add(key)
                continue
            todo.append((kind, rel))
        return todo

    def pull(self, keys, files):
        for kind in PULLED:
            rels = [rel for k, rel in keys if k == kind]
            if not rels:
                continue
            local = self.roots[kind]
            if kind == "o":
                target = lambda rel: output_target(local, rel)
            else:
                target = lambda rel: Path(local, *safe_parts(rel))
            total = sum(files[(kind, r)][0] for r in rels)
            progress = Progress(f"<- {KINDS[kind][2]:<9}", total, len(rels))
            written = fetch(self.pod, self.ssh_base, KINDS[kind][0], rels, target, progress)
            progress.finish()
            for rel, dest in written:
                size, mtime = files[(kind, rel)]
                self.pulled.add(f"{kind}|{rel}|{size}|{mtime}")
                if kind != "o":
                    self.local[f"{kind}|{rel}"] = local_sig(dest)
                print(f"  saved   {dest.relative_to(self.base)}", flush=True)
        self.save_state()

    # --- local -> pod ----------------------------------------------------

    def scan_local(self):
        """Return {(kind, rel): sig} for local files in pushed folders."""
        found = {}
        for kind in PUSHED:
            local = self.roots[kind]
            for p in local.rglob("*"):
                if p.is_file() and not skipped(p.name):
                    found[(kind, p.relative_to(local).as_posix())] = local_sig(p)
        return found

    def push(self, require_stable=True):
        """Upload new or changed local files. With require_stable, a file must
        look the same on two consecutive scans (so half-copied files wait)."""
        scan = self.scan_local()
        ready = [key for key, sig in scan.items()
                 if self.local.get(f"{key[0]}|{key[1]}") != sig
                 and (not require_stable or self.last_scan.get(key) == sig)]
        self.last_scan = scan
        for kind in PUSHED:
            rels = [rel for k, rel in ready if k == kind]
            if not rels:
                continue
            local = self.roots[kind]
            paths = [local / r for r in rels]
            progress = Progress(f"-> {KINDS[kind][2]:<9}", sum(scan[(kind, r)][0] for r in rels), len(rels))
            upload(self.pod, self.ssh_base, KINDS[kind][0], local, paths, progress)
            progress.finish()
            for rel in rels:
                self.local[f"{kind}|{rel}"] = scan[(kind, rel)]
                if KINDS[kind][3] == "both":
                    self.acks.add((kind, rel))
                print(f"  sent    {KINDS[kind][2]}/{rel}", flush=True)
        if ready:
            self.save_state()
        return len(ready)


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


def open_when_ready(url, timeout=120):
    """Open url in the default browser once it responds."""
    def wait():
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                urllib.request.urlopen(url, timeout=3)
                webbrowser.open(url)
                return
            except OSError:
                time.sleep(1)
    threading.Thread(target=wait, daemon=True).start()


def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True


def run_connect(pod, config, ssh_base, root, open_browser=False):
    local_port = config.get("local_port", 8288)
    # The tunnel's port doubles as a lock: two syncs at once would upload the
    # same files concurrently and corrupt each other's transfers.
    if port_in_use(local_port):
        sys.exit(f"Port {local_port} is already in use, most likely by another start/connect "
                 "window that is already syncing. Use that one, or close it first.")
    syncer = Syncer(pod, config, ssh_base, root)
    syncer.ensure_session()
    # Local first, so a fresh pod's defaults don't overwrite local settings.
    sent = syncer.push(require_stable=False)
    if sent:
        print(f"Uploaded {sent} local file(s)")
    session, listings = start_session(pod, ssh_base, local_port)
    if open_browser:
        open_when_ready(f"http://localhost:{local_port}/")
    print(f"ComfyUI: http://localhost:{local_port}")
    print(f"Synced:  {syncer.base}\\  (outputs, inputs, workflows, settings, loras)")
    print("Ctrl+C to stop syncing; the pod keeps running.", flush=True)
    seen, prev_files = {}, None
    try:
        while True:
            if session.poll() is not None:
                print("Connection lost; reconnecting...", flush=True)
                time.sleep(3)
                session, listings = start_session(pod, ssh_base, local_port)
                continue
            try:
                files, session = listings.get(timeout=POLL_SECONDS * 3)
            except queue.Empty:
                continue
            if session != syncer.session:
                # Pod RAM was wiped (or set up again): start over, deleting nothing.
                syncer.ensure_session()
                syncer.push(require_stable=False)
                seen, prev_files = {}, None
                continue
            if prev_files is not None:
                syncer.apply_deletions(files, prev_files)
            prev_files = files
            # Pull a file once it looks the same on two consecutive listings,
            # so half-written videos aren't fetched.
            ready = [key for key in syncer.pending_pulls(files) if seen.get(key) == files[key]]
            seen = files
            if ready:
                syncer.pull(ready, files)
            syncer.push()
    except KeyboardInterrupt:
        print("\nStopped syncing.")
    finally:
        session.terminate()
        syncer.save_state()


def sync_once(pod, config, ssh_base, root):
    """Pull everything not yet copied home. Returns the number of files copied."""
    syncer = Syncer(pod, config, ssh_base, root)
    cmd = ssh_base(pod, "-o", "BatchMode=yes") + [LIST_CMD]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"could not list pod files: {res.stderr.strip()}")
    files, session = parse_listing(res.stdout.splitlines())
    if session != syncer.session:
        # Records belong to another RAM session; pull outputs as new.
        syncer.pulled, syncer.local, syncer.session = set(), {}, session
    todo = syncer.pending_pulls(files)
    if todo:
        syncer.pull(todo, files)
    return len(todo)
