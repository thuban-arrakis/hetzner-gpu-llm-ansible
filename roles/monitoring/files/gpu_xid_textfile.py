#!/usr/bin/env python3
"""Publish numeric recent Xid diagnostics; never expose kernel journal content."""

import argparse
import json
from pathlib import Path
import re
import subprocess
import time

from gpu_textfile import publish


WINDOW_SECONDS = 900
MAX_EVENTS = 256
QUERY = [
    "/usr/bin/journalctl", "--quiet", "--no-pager", "--boot=0", "--since=-15min",
    "--lines=257", "--output=json", "--output-fields=MESSAGE,__REALTIME_TIMESTAMP",
    "--grep=NVRM: Xid", "--case-sensitive=yes", "_TRANSPORT=kernel",
]
AVAILABILITY_QUERY = [
    "/usr/bin/journalctl", "--quiet", "--no-pager", "--boot=0", "--lines=1",
    "--output=json", "--output-fields=__REALTIME_TIMESTAMP", "_TRANSPORT=kernel",
]
XID = re.compile(r"NVRM: Xid \((?:PCI:)?[0-9a-fA-F:.]+\):\s*([1-9][0-9]{0,3})(?:,|\s|$)")


def readings(text, now):
    if len(text.encode()) > 2 * 1024 * 1024:
        raise ValueError("journal response exceeds limit")
    count, latest, last_code = 0, 0, 0
    rows = text.splitlines()
    if len(rows) > MAX_EVENTS + 1:
        raise ValueError("journal response exceeds event limit")
    for line in rows:
        row = json.loads(line)
        if not isinstance(row, dict) or not isinstance(row.get("MESSAGE"), str):
            raise ValueError("invalid journal event")
        timestamp = row.get("__REALTIME_TIMESTAMP")
        if not isinstance(timestamp, str) or not timestamp.isdecimal():
            raise ValueError("invalid journal timestamp")
        timestamp = int(timestamp) / 1_000_000
        match = XID.search(row["MESSAGE"])
        if not match:
            raise ValueError("unrecognized Xid event")
        # Journal retention/reboot and bounded windows are explicit. Old events
        # must not be replayed forever as though they were newly observed.
        if not now - WINDOW_SECONDS <= timestamp <= now:
            continue
        count += 1
        if timestamp >= latest:
            latest, last_code = timestamp, int(match.group(1))
    return [
        "# TYPE llm_gpu_xid_events_recent gauge",
        f"llm_gpu_xid_events_recent {count}",
        f"llm_gpu_xid_last_seen_timestamp_seconds {latest:.3f}",
        f"llm_gpu_xid_last_code {last_code}",
        f"llm_gpu_xid_window_truncated {int(len(rows) > MAX_EVENTS)}",
    ]


def collect():
    now = time.time()
    try:
        available = subprocess.run(AVAILABILITY_QUERY, capture_output=True, text=True, timeout=4, check=False)
        if available.returncode or available.stderr.strip() or len(available.stdout) > 4096:
            raise ValueError("kernel journal unavailable")
        event = json.loads(available.stdout)
        if not isinstance(event, dict) or not str(event.get("__REALTIME_TIMESTAMP", "")).isdecimal():
            raise ValueError("current boot kernel journal unavailable")
        result = subprocess.run(QUERY, capture_output=True, text=True, timeout=6, check=False)
        # journalctl --grep returns 1 when no entries match, not an outage. Any
        # diagnostic on stderr is treated as unknown instead of healthy zero.
        if result.returncode not in (0, 1) or result.stderr.strip() or (result.returncode == 1 and result.stdout.strip()):
            raise ValueError("journal collection unavailable")
        metrics = readings(result.stdout, now)
        success = 1
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        metrics, success = [], 0
    metrics.extend([
        f"llm_gpu_xid_collection_success {success}",
        f"llm_gpu_xid_collection_timestamp_seconds {now:.3f}",
    ])
    return "\n".join(metrics) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        publish(args.output, collect())
    except (OSError, ValueError):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
