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
    """Raise KeyboardInterrupt in the main thread on "stop" or stdin EOF."""
    def run():
        for line in sys.stdin:
            if line.strip() == "stop":
                break
        _thread.interrupt_main()
    threading.Thread(target=run, daemon=True).start()
