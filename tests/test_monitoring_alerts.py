"""Evaluate provisioned PromQL with an optional isolated VictoriaMetrics binary.

Set VICTORIAMETRICS_BINARY to an official verified local binary. Every test uses
fresh temporary storage and a loopback port; no production endpoint is used.
"""

import json
import os
from pathlib import Path
import socket
import subprocess
import time
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

import pytest
import yaml

from conftest import ROOT


FILES = ROOT / "roles" / "monitoring" / "files"
RULES = {
    rule["title"]: rule["data"][0]["model"]["expr"]
    for group in yaml.safe_load((FILES / "grafana-alerts.yml").read_text())["groups"]
    for rule in group["rules"]
}
GPU_RULES = [name for name in RULES if name.startswith("Gpu")]


@pytest.fixture
def metrics_engine(tmp_path):
    binary = os.environ.get("VICTORIAMETRICS_BINARY")
    if not binary:
        pytest.skip("Set VICTORIAMETRICS_BINARY to run isolated PromQL behavioral tests")
    if not Path(binary).is_file():
        pytest.fail("VICTORIAMETRICS_BINARY is not a local file")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    opener = build_opener(ProxyHandler({}))
    process = subprocess.Popen([
        binary, f"-storageDataPath={tmp_path / 'data'}", f"-httpListenAddr=127.0.0.1:{port}",
        "-retentionPeriod=1d", "-memory.allowedBytes=268435456", "-storage.minFreeDiskSpaceBytes=0",
        "-search.latencyOffset=0", "-loggerLevel=ERROR",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                with opener.open(origin + "/health", timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                if process.poll() is not None:
                    pytest.fail("Isolated VictoriaMetrics failed to start")
                time.sleep(0.1)
        else:
            pytest.fail("Isolated VictoriaMetrics did not become ready")

        def query(expression):
            body = urlencode({"query": expression.replace("$__range", "1h"), "nocache": "1"}).encode()
            with opener.open(Request(origin + "/api/v1/query", data=body), timeout=5) as response:
                result = json.load(response)
            assert result["status"] == "success"
            return result["data"]["result"]

        def insert(text):
            with opener.open(Request(origin + "/api/v1/import/prometheus", data=text.encode()), timeout=5) as response:
                assert response.status == 204
            names = {
                line.split("{", 1)[0].split()[0] for line in text.splitlines()
                if line and not line.startswith("#") and " NaN" not in line
            }
            # Imports are asynchronous. Require fixture visibility before a
            # negative alert assertion, which would otherwise pass vacuously.
            for _ in range(40):
                with opener.open(Request(origin + "/internal/force_flush", data=b""), timeout=5) as response:
                    assert response.status == 200
                if all(query(name) for name in names):
                    break
                time.sleep(0.05)
            else:
                pytest.fail("Isolated fixture metrics were not ingested")

        yield insert, query
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def configure(insert, mode="nvidia", **metrics):
    lines = [f'llm_exporter_info{{job="llm",instance="fixture",accelerator="{mode}",model="fixture"}} 1']
    lines.extend(f'{name}{{job="llm",instance="fixture"}} {value}' for name, value in metrics.items())
    insert("\n".join(lines) + "\n")


def test_all_provisioned_alert_and_dashboard_queries_are_valid(metrics_engine):
    _, query = metrics_engine
    expressions = set(RULES.values())
    for path in (FILES / "dashboards").glob("*.json"):
        for panel in json.loads(path.read_text())["panels"]:
            expressions.update(target["expr"] for target in panel.get("targets", []))
    for expression in expressions:
        query(expression)


def test_cpu_only_host_does_not_trigger_any_gpu_rule(metrics_engine):
    insert, query = metrics_engine
    configure(insert, mode="cpu")
    for name in GPU_RULES:
        assert query(RULES[name]) == [], name


@pytest.mark.parametrize("placement,known,inference,expected", [
    (1, 1, 1, None),
    (0, 1, 1, "GpuInferencePlacementViolation"),
    ("NaN", 0, 1, "GpuInferenceResidencyUnknown"),
    ("NaN", "NaN", 0, None),
])
def test_post_inference_alerts_distinguish_idle_fallback_and_unknown(metrics_engine, placement, known, inference, expected):
    insert, query = metrics_engine
    configure(insert, llm_model_loaded=0, llm_inference_gpu_policy_satisfied=placement,
              llm_inference_gpu_check_success=known, llm_inference_probe_success=inference)
    for name in ("GpuInferencePlacementViolation", "GpuInferenceResidencyUnknown"):
        result = query(RULES[name])
        assert bool(result) == (name == expected), name
        if result:
            assert float(result[0]["value"][1]) == 1


def test_unsupported_ecc_and_normal_power_cap_are_not_hardware_faults(metrics_engine):
    insert, query = metrics_engine
    configure(insert)
    insert('llm_gpu_diagnostics_collection_success{job="node"} 1\n'
           'llm_gpu_ecc_supported{job="node",uuid="fixture",kind="uncorrected",scope="volatile"} 0\n'
           'llm_gpu_throttle_active{job="node",uuid="fixture",reason="sw_power_cap"} 1\n')
    for name in ("GpuOptionalDiagnosticsUnavailable", "GpuUncorrectedEccErrors",
                 "GpuCorrectedEccErrorsIncreasing", "GpuThermalOrPowerBrakeThrottle"):
        assert query(RULES[name]) == [], name


@pytest.mark.parametrize("sample,rule", [
    ('llm_gpu_ecc_errors_total{job="node",uuid="fixture",kind="uncorrected",scope="volatile"} 1\n',
     "GpuUncorrectedEccErrors"),
    ('llm_gpu_throttle_active{job="node",uuid="fixture",reason="hw_thermal_slowdown"} 1\n',
     "GpuThermalOrPowerBrakeThrottle"),
    ('llm_gpu_diagnostics_collection_success{job="node"} 0\n', "GpuOptionalDiagnosticsUnavailable"),
])
def test_supported_hardware_diagnostic_faults_fire(metrics_engine, sample, rule):
    insert, query = metrics_engine
    configure(insert)
    insert(sample)
    assert query(RULES[rule]), rule


def test_increasing_corrected_ecc_counter_fires(metrics_engine):
    insert, query = metrics_engine
    configure(insert)
    now = int(time.time() * 1000)
    metric = 'llm_gpu_ecc_errors_total{job="node",uuid="fixture",kind="corrected",scope="volatile"}'
    insert(f"{metric} 1 {now - 600000}\n{metric} 30 {now}\n")
    assert query(RULES["GpuCorrectedEccErrorsIncreasing"])


@pytest.mark.parametrize("count,success,expected", [(1, 1, "GpuXidDetected"), (0, 1, None),
                                                     (1, 0, "GpuXidCollectionUnavailable")])
def test_xid_rules_distinguish_recent_errors_from_collection_failure(metrics_engine, count, success, expected):
    insert, query = metrics_engine
    configure(insert)
    insert(f'llm_gpu_xid_events_recent{{job="node",instance="fixture"}} {count}\n'
           f'llm_gpu_xid_collection_success{{job="node",instance="fixture"}} {success}\n'
           f'llm_gpu_xid_collection_timestamp_seconds{{job="node",instance="fixture"}} {time.time()}\n')
    for name in ("GpuXidDetected", "GpuXidCollectionUnavailable"):
        assert bool(query(RULES[name])) == (name == expected), name
