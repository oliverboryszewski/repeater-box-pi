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
    requested_stop = None
    def stop(signum, _frame):
        nonlocal requested_stop
        if requested_stop is None and child.poll() is None:
            requested_stop = signum
            child.send_signal(signum)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        for line in child.stdout:
            if safe_line(line):
                print(line, end="", flush=True)
        returncode = child.wait()
        # A requested stop may end through the child's signal handler or by the
        # signal itself. Preserve every unrelated failure as a service failure.
        if requested_stop is not None and returncode in (0, -requested_stop):
            return 0
        return returncode
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in CONFIGS:
        sys.exit("Use one fixed installed bench, receive-monitor, or live configuration.")
    sys.exit(run(sys.argv[1]))
