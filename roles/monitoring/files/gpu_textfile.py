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
import xml.etree.ElementTree as ET


QUERY = [
    "/usr/bin/nvidia-smi",
    "--query-gpu=index,uuid,name,temperature.gpu,memory.used,memory.total,utilization.gpu,power.draw",
    "--format=csv,noheader,nounits",
]
DIAGNOSTIC_QUERY = ["/usr/bin/nvidia-smi", "-q", "-x"]
UNSUPPORTED = {"", "N/A", "[N/A]", "[Not Supported]", "Not Supported"}
# Idle and application-clock limits are normal operating states, not faults.
THROTTLE_REASONS = (
    "sw_thermal_slowdown", "hw_thermal_slowdown", "hw_power_brake_slowdown", "sw_power_cap",
)


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
        if row[7].strip() not in UNSUPPORTED:
            values["llm_gpu_power_watts"] = number(row[7])
        lines.extend(f"{metric}{{{labels}}} {value:.12g}" for metric, value in values.items())
    if not seen:
        raise ValueError("no GPU measurements")
    return lines


def diagnostics(text):
    """Optional fields are absent on many consumer cards; never fabricate zeros."""
    if len(text.encode()) > 2 * 1024 * 1024 or "<!ENTITY" in text:
        raise ValueError("invalid diagnostic document")
    root = ET.fromstring(text)
    devices = root.findall("gpu")
    if root.tag != "nvidia_smi_log" or not devices:
        raise ValueError("missing diagnostic devices")
    lines, seen = ["# TYPE llm_gpu_ecc_errors_total counter"], set()
    for gpu in devices:
        uuid = (gpu.findtext("uuid") or "").strip()
        if not uuid or uuid in seen:
            raise ValueError("invalid diagnostic identity")
        seen.add(uuid)
        identity = f'uuid="{label(uuid)}"'
        for kind, tag in (("corrected", "single_bit"), ("uncorrected", "double_bit")):
            for scope in ("volatile", "aggregate"):
                raw = gpu.findtext(f"ecc_errors/{scope}/{tag}/total")
                # Newer XML schemas may use the corrected/uncorrected wording.
                if raw is None:
                    raw = gpu.findtext(f"ecc_errors/{scope}/{kind}/total")
                raw = (raw or "").strip()
                supported = raw not in UNSUPPORTED
                labels = f'{identity},kind="{kind}",scope="{scope}"'
                lines.append(f"llm_gpu_ecc_supported{{{labels}}} {int(supported)}")
                if supported:
                    if not raw.isdecimal():
                        raise ValueError("invalid ECC count")
                    lines.append(f"llm_gpu_ecc_errors_total{{{labels}}} {int(raw)}")
        for reason in THROTTLE_REASONS:
            raw = None
            for prefix in ("clocks_event", "clocks_throttle"):
                raw = gpu.findtext(f"{prefix}_reasons/{prefix}_reason_{reason}")
                if raw is not None:
                    break
            raw = (raw or "").strip()
            supported = raw not in UNSUPPORTED
            labels = f'{identity},reason="{reason}"'
            lines.append(f"llm_gpu_throttle_supported{{{labels}}} {int(supported)}")
            if supported:
                if raw not in ("Active", "Not Active"):
                    raise ValueError("invalid clock event state")
                lines.append(f"llm_gpu_throttle_active{{{labels}}} {int(raw == 'Active')}")
    return lines


def collect():
    try:
        result = subprocess.run(QUERY, capture_output=True, text=True, check=True, timeout=10)
        metrics = readings(result.stdout)
        success = 1
    except (OSError, subprocess.SubprocessError, ValueError):
        metrics = []
        success = 0
    diagnostic_success = 0
    if success:
        try:
            result = subprocess.run(DIAGNOSTIC_QUERY, capture_output=True, text=True, check=True, timeout=6)
            metrics.extend(diagnostics(result.stdout))
            diagnostic_success = 1
        except (OSError, subprocess.SubprocessError, ValueError, ET.ParseError):
            # Optional diagnostics must not erase independently valid core data.
            pass
    metrics += [
        f"llm_gpu_collection_success {success}",
        f"llm_gpu_diagnostics_collection_success {diagnostic_success}",
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
