import json
import math
import subprocess
import threading
from pathlib import Path

import pytest

from conftest import load_module


@pytest.fixture
def exporter_module():
    return load_module("llm_exporter", "roles/monitoring/files/llm_exporter.py")


@pytest.fixture
def exporter_config(exporter_module):
    return exporter_module.validate_config({})


def health_result():
    return {"available": 1, "loaded": 1, "resident_bytes": 3000, "vram_bytes": 0, "cpu_bytes": 3000}


def test_health_records_only_configured_model_memory(exporter_module, exporter_config):
    def request(config, path):
        if path == "/api/tags":
            return {"models": [{"name": config["model"]}, {"name": "untrusted-response-label"}]}
        return {"models": [
            {"name": config["model"], "size": 3000, "size_vram": 0},
            {"name": "untrusted-response-label", "size": 999999, "size_vram": 999999},
        ]}

    assert exporter_module.collect_health(exporter_config, request) == health_result()


def test_model_absence_is_not_an_api_outage(exporter_module, exporter_config):
    result = exporter_module.collect_health(exporter_config, lambda *args: {"models": []})
    exporter = exporter_module.Exporter(exporter_config, runner=lambda _: result, clock=lambda: 100)
    exporter.run_probe("health")
    assert exporter.metrics["llm_api_up"] == 1
    assert exporter.metrics["llm_model_available"] == 0
    assert exporter.metrics["llm_model_loaded"] == 0
    assert exporter.metrics["llm_health_last_success_timestamp_seconds"] == 100


def test_synthetic_request_has_small_fixed_budget_and_discards_text(exporter_module, exporter_config):
    requests = []

    def request(config, path, body):
        requests.append((path, body))
        return {"response": "untrusted-generated-content", "done": True, "eval_count": 2, "eval_duration": 500_000_000}

    result = exporter_module.collect_inference(exporter_config, request)
    assert result == {"output_tokens": 2, "tokens_per_second": 4}
    assert requests[0][0] == "/api/generate"
    assert requests[0][1]["options"] == {"num_predict": 8, "num_ctx": 2048, "temperature": 0}
    assert requests[0][1]["stream"] is False
    assert requests[0][1]["think"] is False
    assert "untrusted-generated-content" not in json.dumps(result)


@pytest.mark.parametrize("response", [
    {"done": False, "response": "ok", "eval_count": 2, "eval_duration": 10},
    {"done": True, "response": "", "eval_count": 2, "eval_duration": 10},
    {"done": True, "response": "ok", "eval_count": 9, "eval_duration": 10},
    {"done": True, "response": "ok", "eval_count": 2, "eval_duration": 0},
])
def test_invalid_or_unbounded_inference_is_rejected(exporter_module, exporter_config, response):
    with pytest.raises(ValueError):
        exporter_module.collect_inference(exporter_config, lambda *args: response)


def test_timeout_preserves_last_success_and_clears_current_readings(exporter_module, exporter_config):
    def timeout(_):
        raise subprocess.TimeoutExpired("fixed-probe", 120)

    exporter = exporter_module.Exporter(exporter_config, runner=lambda _: {"output_tokens": 2, "tokens_per_second": 4},
                                       clock=lambda: 100)
    exporter.run_probe("inference")
    exporter.runner = timeout
    exporter.run_probe("inference")
    assert exporter.metrics["llm_inference_probe_requests_total"] == 2
    assert exporter.metrics["llm_inference_probe_errors_total"] == 1
    assert exporter.metrics["llm_inference_probe_timeouts_total"] == 1
    assert exporter.metrics["llm_inference_probe_success"] == 0
    assert exporter.metrics["llm_inference_last_success_timestamp_seconds"] == 100
    assert math.isnan(exporter.metrics["llm_inference_probe_tokens_per_second"])


def test_health_failures_do_not_report_fake_zero_memory_or_error_text(exporter_module, exporter_config):
    def failure(_):
        raise ValueError("untrusted-response-content")

    exporter = exporter_module.Exporter(exporter_config, runner=failure)
    exporter.run_probe("health")
    assert exporter.metrics["llm_api_up"] == 0
    assert exporter.metrics["llm_health_probe_errors_total"] == 1
    assert math.isnan(exporter.metrics["llm_model_vram_bytes"])
    assert b"untrusted-response-content" not in exporter.render()


def test_scrapes_use_cached_metrics_and_inference_never_overlaps(exporter_module, exporter_config):
    started, finish = threading.Event(), threading.Event()
    calls = []

    def runner(kind):
        calls.append(kind)
        started.set()
        finish.wait(timeout=5)
        return {"output_tokens": 2, "tokens_per_second": 4}

    exporter = exporter_module.Exporter(exporter_config, runner=runner)
    worker = threading.Thread(target=exporter.run_probe, args=("inference",))
    worker.start()
    try:
        assert started.wait(timeout=2)
        assert exporter.run_probe("inference") is False
        for _ in range(10):
            assert b"llm_inference_probe_in_progress 1" in exporter.render()
        assert calls == ["inference"]
    finally:
        finish.set()
        worker.join(timeout=2)
    assert exporter.metrics["llm_inference_probe_requests_total"] == 1


def test_health_collection_remains_independent_of_inference(exporter_module, exporter_config):
    exporter = exporter_module.Exporter(exporter_config, runner=lambda _: health_result())
    exporter._probe_locks["inference"].acquire()
    try:
        assert exporter.run_probe("health") is True
        assert exporter.metrics["llm_api_up"] == 1
    finally:
        exporter._probe_locks["inference"].release()


def test_only_static_info_labels_are_exposed(exporter_module, exporter_config):
    exporter = exporter_module.Exporter(exporter_config, runner=lambda _: health_result())
    exporter.run_probe("health")
    output = exporter.render().decode()
    sample_lines = [line for line in output.splitlines() if not line.startswith("#")]
    labels = [line for line in sample_lines if "{" in line]
    assert len(labels) == 1
    assert labels[0].startswith('llm_exporter_info{accelerator="cpu",model="qwen3:4b-q4_K_M"}')
    assert len(sample_lines) == len(exporter_module.METRICS) + 1


@pytest.mark.parametrize("setting", [
    {"api_url": "http://192.0.2.1:11434"},
    {"api_url": "http://127.0.0.1:11434?override=true"},
    {"listen_host": "0.0.0.0"},
    {"model": 'bad:model"label'},
    {"inference_interval_seconds": 1},
    {"inference_timeout_seconds": 3600},
    {"health_interval_seconds": True},
    {"context_length": 0},
    {"context_length": 131072},
    {"unknown": "arbitrary"},
])
def test_unsafe_or_excessive_probe_configuration_is_rejected(exporter_module, setting):
    with pytest.raises(ValueError):
        exporter_module.validate_config(setting)


def test_child_process_has_hard_timeout_and_fixed_probe_arguments(exporter_module, exporter_config, monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        raise subprocess.TimeoutExpired("probe", kwargs["timeout"])

    monkeypatch.setattr(exporter_module.subprocess, "run", run)
    exporter = exporter_module.Exporter(exporter_config, config_path="/etc/llm-monitoring/llm-exporter.json")
    exporter.run_probe("inference")
    assert calls[0][1]["timeout"] == 122
    assert calls[0][0][-2:] == ["--probe", "inference"]
    assert Path(calls[0][0][1]).name == "llm_exporter.py"
    assert exporter.metrics["llm_inference_probe_timeouts_total"] == 1


def test_redirects_are_never_followed(exporter_module):
    assert exporter_module.NoRedirects().redirect_request(None, None, 302, None, None, "https://example.org") is None


def test_child_socket_timeout_is_counted_as_timeout(exporter_module, exporter_config, monkeypatch):
    monkeypatch.setattr(exporter_module.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 2, b"", b""))
    exporter = exporter_module.Exporter(exporter_config, config_path="/etc/llm-monitoring/llm-exporter.json")
    exporter.run_probe("health")
    assert exporter.metrics["llm_health_probe_timeouts_total"] == 1
    assert exporter.metrics["llm_health_probe_errors_total"] == 1


def test_probe_context_matches_the_configured_service(exporter_module, exporter_config):
    exporter_config["context_length"] = 4096
    requests = []

    def request(config, path, body):
        requests.append(body)
        return {"response": "ok", "done": True, "eval_count": 2, "eval_duration": 500_000_000}

    exporter_module.collect_inference(exporter_config, request)
    assert requests[0]["options"]["num_ctx"] == 4096
    assert requests[0]["options"]["num_predict"] == 8
