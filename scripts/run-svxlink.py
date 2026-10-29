#!/usr/bin/python3
"""Run fixed installed SvxLink configurations without retaining RF digits."""
import re
import signal
import subprocess
import sys

CONFIGS = frozenset(("/etc/repeater-box/svxlink-bench.conf", "/etc/repeater-box/svxlink-rx-monitor.conf",
                     "/etc/repeater-box/svxlink-live.conf"))
SENSITIVE = re.compile(r"(?:\bdigit=[0-9A-F*#]|dtmf_(?:cmd|digit)_received|Processing macro command:|Macro command found:|DTMF command|DTMF digit)")


def safe_line(line):
    return not SENSITIVE.search(line)


def run(config):
    if config not in CONFIGS:
        raise ValueError("Unsupported local engine configuration")
    child = subprocess.Popen(["/usr/bin/svxlink", "--config=" + config],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, errors="replace", bufsize=1)
    def stop(signum, _frame):
        if child.poll() is None:
            child.send_signal(signum)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        for line in child.stdout:
            if safe_line(line):
                print(line, end="", flush=True)
        return child.wait()
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in CONFIGS:
        sys.exit("Use one fixed installed bench, receive-monitor, or live configuration.")
    sys.exit(run(sys.argv[1]))
