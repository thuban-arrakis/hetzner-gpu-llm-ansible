import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import subprocess
from subprocess import CompletedProcess
import sys
import threading
import time

import pytest

from conftest import ROOT, load_module


UUID = "GPU-12345678-1234-1234-1234-123456789abc"


@pytest.fixture
def acceptance(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "roles/ollama/files"))
    return load_module("gpu_acceptance", "roles/ollama/files/gpu_acceptance.py")


@pytest.fixture
def capacity_config():
    return {
        "api_url": "http://127.0.0.1:11434", "model": "test:latest", "require_gpu": True,
        "context_length": 4096, "parallel_requests": 2, "predict_tokens": 64, "timeout": 60,
        "prompt": "", "min_context_fraction": 0.75, "gpu_offload_policy": "full",
        "gpu_min_vram_fraction": 0.5, "gpu_headroom_mib": 512, "gpu_device_uuids": [UUID],
    }


def fake_request(config, *, vram=1000, prompt_tokens=3800, context=4096):
    def request(path, payload=None):
        if path == "/api/generate":
            assert payload["options"]["num_ctx"] == config["context_length"]
            assert payload["keep_alive"] == "5m"
            return {"done": True, "response": "READY", "eval_count": 2,
                    "prompt_eval_count": prompt_tokens}
        return {"models": [{"name": config["model"], "size": 1000, "size_vram": vram,
                            "context_length": context}]}
    return request


def test_acceptance_exercises_parallel_context_and_reports_only_sanitized_summary(acceptance, capacity_config):
    arrived = threading.Barrier(2)
    base_request = fake_request(capacity_config)

    def request(path, payload=None):
        if path == "/api/generate":
            # A sequential implementation deadlocks/fails this barrier.
            arrived.wait(timeout=2)
            assert len(payload["prompt"]) > 15000
        return base_request(path, payload)

    result = acceptance.run_acceptance(capacity_config, request, lambda: {UUID: 900})
    assert result["accepted"] and result["requests"] == 2
    assert result["minimum_prompt_tokens"] == 3800
    assert result["minimum_sampled_free_vram_mib"] == 900
    assert "READY" not in json.dumps(result)


@pytest.mark.parametrize("vram", [0, 950, 1001, -1, None, True])
def test_tokens_alone_do_not_pass_full_gpu_acceptance(acceptance, capacity_config, vram):
    with pytest.raises(ValueError, match="placement policy"):
        acceptance.run_acceptance(capacity_config, fake_request(capacity_config, vram=vram), lambda: {UUID: 900})


def test_partial_offload_requires_explicit_policy_and_minimum(acceptance, capacity_config):
    capacity_config["gpu_offload_policy"] = "partial"
    assert acceptance.run_acceptance(
        capacity_config, fake_request(capacity_config, vram=600), lambda: {UUID: 900}
    )["accepted"]
    with pytest.raises(ValueError, match="placement policy"):
        acceptance.run_acceptance(
            capacity_config, fake_request(capacity_config, vram=499), lambda: {UUID: 900}
        )


def test_short_prompt_cannot_claim_context_capacity(acceptance, capacity_config):
    with pytest.raises(ValueError, match="exercise the configured context"):
        acceptance.run_acceptance(
            capacity_config, fake_request(capacity_config, prompt_tokens=12), lambda: {UUID: 900}
        )


def test_runtime_context_mismatch_fails_acceptance(acceptance, capacity_config):
    with pytest.raises(ValueError, match="context does not match"):
        acceptance.run_acceptance(
            capacity_config, fake_request(capacity_config, context=2048), lambda: {UUID: 900}
        )


def test_sampled_low_headroom_rejects_successful_gpu_generation(acceptance, capacity_config):
    samples = iter([{UUID: 900}, {UUID: 200}])
    with pytest.raises(ValueError, match="headroom"):
        acceptance.run_acceptance(capacity_config, fake_request(capacity_config), lambda: next(samples))


def test_cpu_demo_never_queries_gpu_and_keeps_small_smoke_payload(acceptance, capacity_config):
    capacity_config["require_gpu"] = False
    base = fake_request(capacity_config, vram=0, prompt_tokens=12)

    def request(path, payload=None):
        if path == "/api/generate":
            assert len(payload["prompt"]) < 150
            assert payload["options"]["num_predict"] == 16
        return base(path, payload)

    def gpu_query():
        raise AssertionError("CPU mode must not query NVIDIA")

    assert acceptance.run_acceptance(capacity_config, request, gpu_query)["gpu_policy"] == "cpu"


@pytest.mark.parametrize("count", [None, "response text", True, -1, 999999])
def test_cpu_summary_cannot_forward_non_numeric_or_invalid_token_count(acceptance, capacity_config, count):
    capacity_config["require_gpu"] = False
    with pytest.raises(ValueError, match="Invalid prompt token count"):
        acceptance.run_acceptance(capacity_config, fake_request(capacity_config, vram=0, prompt_tokens=count))


def test_selected_gpu_query_does_not_accept_other_device_data(acceptance, capacity_config, monkeypatch):
    commands = []

    def run(args, **kwargs):
        commands.append(args)
        return CompletedProcess(args, 0, b"GPU-other, 900\n", b"")

    monkeypatch.setattr(acceptance.subprocess, "run", run)
    with pytest.raises(ValueError, match="incomplete"):
        acceptance.read_gpu_memory(capacity_config)
    assert commands[0][-1] == "--id=" + UUID


def test_just_completed_inference_cannot_be_reported_as_idle(acceptance, capacity_config):
    base = fake_request(capacity_config)

    def request(path, payload=None):
        return {"models": []} if path == "/api/ps" else base(path, payload)

    with pytest.raises(ValueError, match="Loaded model context"):
        acceptance.run_acceptance(capacity_config, request, lambda: {UUID: 900})


def test_repeated_deployment_uses_unique_prompt_prefixes_for_all_requests(acceptance, capacity_config):
    prompts = []
    base = fake_request(capacity_config)

    def request(path, payload=None):
        if path == "/api/generate":
            prompts.append(payload["prompt"])
        return base(path, payload)

    acceptance.run_acceptance(capacity_config, request, lambda: {UUID: 900})
    acceptance.run_acceptance(capacity_config, request, lambda: {UUID: 900})
    assert len(prompts) == len(set(prompts)) == 4
    assert len({prompt.splitlines()[0] for prompt in prompts}) == 4


def test_parallel_capacity_checks_per_slot_context_not_multiplied_total(acceptance, capacity_config):
    capacity_config["parallel_requests"] = 4
    assert acceptance.run_acceptance(
        capacity_config, fake_request(capacity_config, context=4096), lambda: {UUID: 900}
    )["requests"] == 4
    with pytest.raises(ValueError, match="context does not match"):
        acceptance.run_acceptance(
            capacity_config, fake_request(capacity_config, context=16384), lambda: {UUID: 900}
        )


def test_dripped_http_response_cannot_outlive_whole_acceptance_deadline(capacity_config, tmp_path):
    class DripHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            try:
                for _ in range(100):
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(0.05)
            except OSError:
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), DripHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    capacity_config.update(require_gpu=False, timeout=1, parallel_requests=1,
                           api_url=f"http://127.0.0.1:{server.server_port}")
    config_file = tmp_path / "acceptance.json"
    config_file.write_text(json.dumps(capacity_config))
    try:
        started = time.monotonic()
        result = subprocess.run(
            [sys.executable, str(ROOT / "roles/ollama/files/gpu_acceptance.py"), "--config", str(config_file)],
            capture_output=True, text=True, timeout=4, check=False,
        )
        assert time.monotonic() - started < 3
        assert result.returncode == 1
        assert json.loads(result.stdout)["check"] == "Acceptance wall-clock deadline exceeded"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("gpu", [True, False])
def test_worker_verifies_reload_survival_before_gpu_acceptance_only(acceptance, capacity_config, tmp_path, monkeypatch, gpu):
    capacity_config["require_gpu"] = gpu
    config_file = tmp_path / "acceptance.json"
    config_file.write_text(json.dumps(capacity_config))
    monkeypatch.setattr(sys, "argv", ["gpu_acceptance.py", "--config", str(config_file), "--worker"])
    operations = []

    def command(args, **kwargs):
        assert args == ["/usr/bin/systemctl", "daemon-reload"]
        operations.append("reload")
        return CompletedProcess(args, 0, b"", b"")

    def accept(config):
        operations.append("inference")
        return {"accepted": True}

    monkeypatch.setattr(acceptance.subprocess, "run", command)
    monkeypatch.setattr(acceptance, "run_acceptance", accept)
    assert acceptance.main() == 0
    assert operations == (["reload", "inference"] if gpu else ["inference"])
