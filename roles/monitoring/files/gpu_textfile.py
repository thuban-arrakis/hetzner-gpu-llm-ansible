#!/usr/bin/env python3
"""Publish bounded NVIDIA readings atomically for node_exporter."""

import argparse
import csv
import io
import math
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time


QUERY = [
    "/usr/bin/nvidia-smi",
    "--query-gpu=index,uuid,name,temperature.gpu,memory.used,memory.total,utilization.gpu,power.draw",
    "--format=csv,noheader,nounits",
]


def label(value):
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def number(value, multiplier=1):
    result = float(value.strip()) * multiplier
    if not math.isfinite(result) or result < 0:
        raise ValueError("invalid GPU measurement")
    return result


def readings(text):
    lines = []
    seen = set()
    for row in csv.reader(io.StringIO(text), skipinitialspace=True):
        if len(row) != 8:
            raise ValueError("invalid GPU measurement row")
        index, uuid, name = (value.strip() for value in row[:3])
        if not index.isdecimal() or not uuid or index in seen:
            raise ValueError("invalid GPU identity")
        seen.add(index)
        labels = f'index="{index}",uuid="{label(uuid)}"'
        lines.append(f'llm_gpu_info{{{labels},name="{label(name)}"}} 1')
        values = {
            "llm_gpu_temperature_celsius": number(row[3]),
            "llm_gpu_memory_used_bytes": number(row[4], 1024 * 1024),
            "llm_gpu_memory_total_bytes": number(row[5], 1024 * 1024),
            "llm_gpu_utilization_ratio": number(row[6], 0.01),
        }
        if values["llm_gpu_utilization_ratio"] > 1:
            raise ValueError("invalid GPU utilization")
        if row[7].strip() not in ("N/A", "[N/A]", "[Not Supported]"):
            values["llm_gpu_power_watts"] = number(row[7])
        lines.extend(f"{metric}{{{labels}}} {value:.12g}" for metric, value in values.items())
    if not seen:
        raise ValueError("no GPU measurements")
    return lines


def collect():
    try:
        result = subprocess.run(QUERY, capture_output=True, text=True, check=True, timeout=10)
        metrics = readings(result.stdout)
        success = 1
    except (OSError, subprocess.SubprocessError, ValueError):
        metrics = []
        success = 0
    metrics += [
        f"llm_gpu_collection_success {success}",
        f"llm_gpu_collection_timestamp_seconds {time.time():.3f}",
    ]
    return "\n".join(metrics) + "\n"


def publish(destination, content):
    directory = destination.parent.lstat()
    if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.geteuid() or directory.st_mode & 0o022:
        raise ValueError("unsafe collector directory")
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=destination.parent, prefix=".gpu-", delete=False) as output:
            temporary_path = Path(output.name)
            output.write(content)
            output.flush()
            os.fchmod(output.fileno(), 0o644)
            os.fsync(output.fileno())
        # Replaces a stale file/link itself; never follows it or truncates its target.
        os.replace(temporary_path, destination)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


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
