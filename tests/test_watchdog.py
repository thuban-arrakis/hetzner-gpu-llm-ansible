import json
import io
from subprocess import CompletedProcess, TimeoutExpired

import pytest


def test_transient_failure_does_not_restart_a_busy_model(watchdog, config):
    action, state = watchdog.recovery_decision(config, {}, "unhealthy", 1000)
    assert action == "failure_observed"
    action, state = watchdog.recovery_decision(config, state, "healthy", 1060)
    assert action == "healthy"
    assert state["failures"] == 0
    assert state["restarts"] == []


def test_sustained_failure_consumes_budget_before_recovery(watchdog, config):
    _, state = watchdog.recovery_decision(config, {}, "unhealthy", 1000)
    action, state = watchdog.recovery_decision(config, state, "unhealthy", 1060)
    assert action == "restart"
    assert state["restarts"] == [1060]
    _, state = watchdog.recovery_decision(config, state, "unhealthy", 1120)
    action, state = watchdog.recovery_decision(config, state, "unhealthy", 1180)
    assert action == "restart_cooldown"
    assert state["restarts"] == [1060]


def test_restart_storm_is_bounded_and_budget_expires(watchdog, config):
    state = {"failures": 2, "restarts": [1000, 1400, 1800]}
    action, state = watchdog.recovery_decision(config, state, "unhealthy", 2500)
    assert action == "restart_limit_reached"
    action, state = watchdog.recovery_decision(config, state, "unhealthy", 6000)
    assert action == "restart"
    assert state["restarts"] == [6000]


def test_deleted_model_is_alerted_without_futile_restart(watchdog, config):
    action, state = watchdog.recovery_decision(config, {}, "missing_model", 1000)
    assert action == "required_model_missing"
    assert state["restarts"] == []


def test_startup_grace_does_not_consume_restart_budget(watchdog, config):
    action, state = watchdog.recovery_decision(config, {"failures": 10}, "starting", 1000)
    assert action == "starting"
    assert state["restarts"] == []


@pytest.mark.parametrize("url", [
    "http://0.0.0.0:11434", "http://192.0.2.1:11434",
    "https://127.0.0.1:11434", "http://127.0.0.1:11434/other",
    "http://127.0.0.1:11434?query=value", "http://127.0.0.1",
])
def test_watchdog_cannot_be_redirected_to_a_remote_service(watchdog, config, url):
    config["api_url"] = url
    with pytest.raises(ValueError):
        watchdog.validate_config(config)


def test_missing_container_is_recoverable(watchdog, config, monkeypatch):
    monkeypatch.setattr(watchdog, "command", lambda *a, **k: CompletedProcess([], 0, b"", b""))
    assert watchdog.probe_health(config) == "unhealthy"


def test_hung_gpu_or_docker_probe_participates_in_bounded_recovery(watchdog, config, monkeypatch):
    def timeout(*args, **kwargs):
        raise TimeoutExpired("docker", 20)

    monkeypatch.setattr(watchdog, "command", timeout)
    health = watchdog.probe_health(config)
    assert health == "unhealthy"
    action, state = watchdog.recovery_decision(config, {"failures": 1}, health, 1000)
    assert action == "restart"
    assert state["restarts"] == [1000]


def test_container_starting_does_not_need_an_http_probe(watchdog, config, monkeypatch):
    results = iter([
        CompletedProcess([], 0, b"a" * 64, b""),
        CompletedProcess([], 0, json.dumps({"Status": "running", "Health": {"Status": "starting"}}).encode(), b""),
    ])
    monkeypatch.setattr(watchdog, "command", lambda *a, **k: next(results))
    assert watchdog.probe_health(config) == "starting"


def test_probe_failure_does_not_execute_untrusted_container_identifiers(watchdog, config, monkeypatch):
    monkeypatch.setattr(watchdog, "command", lambda *a, **k: CompletedProcess([], 0, b"unexpected-container", b""))
    with pytest.raises(RuntimeError):
        watchdog.probe_health(config)


def test_explicit_cpu_mode_checks_health_without_running_nvidia_smi(watchdog, config, monkeypatch):
    config["require_gpu"] = False
    commands = []

    def run(args, **kwargs):
        commands.append(args)
        if "ps" in args:
            return CompletedProcess([], 0, b"a" * 64, b"")
        if "inspect" in args:
            state = {"Status": "running", "Health": {"Status": "healthy"}}
            return CompletedProcess([], 0, json.dumps(state).encode(), b"")
        raise AssertionError("CPU watchdog must not invoke GPU commands")

    class Response(io.BytesIO):
        status = 200

    class Opener:
        def open(self, *args, **kwargs):
            return Response(json.dumps({"models": [{"name": config["model"]}]}).encode())

    monkeypatch.setattr(watchdog, "command", run)
    monkeypatch.setattr(watchdog, "build_opener", lambda *args: Opener())
    assert watchdog.probe_health(config) == "healthy"
    assert len(commands) == 2


def test_omitted_accelerator_check_keeps_gpu_required(watchdog, config, monkeypatch):
    results = iter([
        CompletedProcess([], 0, b"a" * 64, b""),
        CompletedProcess([], 0, json.dumps({"Status": "running", "Health": {"Status": "healthy"}}).encode(), b""),
        CompletedProcess([], 1, b"", b""),
    ])
    monkeypatch.setattr(watchdog, "command", lambda *a, **k: next(results))
    assert watchdog.probe_health(config) == "unhealthy"


@pytest.mark.parametrize("value", ["false", 0, None])
def test_gpu_checks_cannot_be_disabled_with_ambiguous_config(watchdog, config, value):
    config["require_gpu"] = value
    with pytest.raises(ValueError):
        watchdog.validate_config(config)


@pytest.mark.parametrize("models,expected", [
    ([], "healthy"),
    ([{"size": 1000, "size_vram": 1000}], "healthy"),
    ([{"size": 1000, "size_vram": 0}], "unhealthy"),
    ([{"size": 1000, "size_vram": 999}], "unhealthy"),
    ([{"size": 1000, "size_vram": 1001}], "unhealthy"),
    ([{"size": 1000}], "unhealthy"),
])
def test_working_nvml_does_not_hide_loaded_cpu_fallback_but_idle_is_healthy(
    watchdog, config, monkeypatch, models, expected,
):
    results = iter([
        CompletedProcess([], 0, b"a" * 64, b""),
        CompletedProcess([], 0, json.dumps({"Status": "running", "Health": {"Status": "healthy"}}).encode(), b""),
        CompletedProcess([], 0, b"GPU-12345678-1234-1234-1234-123456789abc\n", b""),
    ])
    monkeypatch.setattr(watchdog, "command", lambda *args, **kwargs: next(results))
    paths = []

    class Response(io.BytesIO):
        status = 200

    class Opener:
        def open(self, url, **kwargs):
            paths.append(url)
            content = [{"name": config["model"]}] if url.endswith("/api/tags") else [
                {"name": config["model"], **item} for item in models
            ]
            return Response(json.dumps({"models": content}).encode())

    monkeypatch.setattr(watchdog, "build_opener", lambda *args: Opener())
    assert watchdog.probe_health(config) == expected
    # Watchdog never wakes idle models or competes with user generation.
    assert paths == [config["api_url"] + "/api/tags", config["api_url"] + "/api/ps"]


@pytest.mark.parametrize("policy,fraction", [("automatic", 0.5), ("partial", 0), ("partial", 1.1)])
def test_invalid_placement_policy_is_rejected(watchdog, config, policy, fraction):
    config.update(gpu_offload_policy=policy, gpu_min_vram_fraction=fraction)
    with pytest.raises(ValueError):
        watchdog.validate_config(config)
