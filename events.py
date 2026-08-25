"""Machine-readable progress for the dashboard.

With RP_EVENTS=1 in the environment, rp.py and sync.py print event lines
("@@" + JSON) between their normal output, never prompt, and treat "stop" on
stdin (or stdin closing) like Ctrl+C.
"""
import _thread
import json
import os
import sys
import threading

ENABLED = bool(os.environ.get("RP_EVENTS"))
PREFIX = "@@"


def emit(kind, **data):
    if ENABLED:
        print(PREFIX + json.dumps({"type": kind, **data}), flush=True)


def watch_stdin():
    """Raise KeyboardInterrupt in the main thread on "stop" or stdin EOF.

    The control pipe is moved off fd 0, which then reads from the null device:
    child processes (ssh) would otherwise inherit the pipe, and Windows
    OpenSSH can block on an inherited pipe forever, even for `ssh host true`.
    """
    control = os.fdopen(os.dup(0), encoding="utf-8", errors="replace")  # dup: not inheritable
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    os.close(null)
    sys.stdin = open(os.devnull, encoding="utf-8")

    def run():
        for line in control:
            if line.strip() == "stop":
                break
        _thread.interrupt_main()
    threading.Thread(target=run, daemon=True).start()
