#!/usr/bin/env python3
"""Bounded recovery for a private Ollama Compose service.

Only fixed event names reach the journal; model responses and Docker logs are
never emitted. This checker does not generate text or keep a model loaded.
"""

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from gpu_residency import evaluate_residency
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener


LOCK_PATH = Path("/run/ollama/maintenance.lock")
STATE_PATH = Path("/var/lib/ollama-watchdog/state.json")
MAX_RESPONSE_BYTES = 1024 * 1024


def event(name):
    """Deliberately do not accept free-form diagnostic values."""
    print(json.dumps({"event": name}), flush=True)


def command(args, timeout=20):
    return subprocess.run(args, capture_output=True, check=False, timeout=timeout)


def compose_command(config):
    return [
        "/usr/bin/docker", "compose", "--project-name", config["project_name"],
        "--file", config["compose_file"],
    ]


def validate_config(config):
    target = urlsplit(config["api_url"])
    if (
        target.scheme != "http" or target.hostname != "127.0.0.1"
        or target.username or target.password or target.query or target.fragment
        or target.path not in ("", "/") or not target.port
    ):
        raise ValueError("Only the private loopback API is supported")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", config["project_name"]):
        raise ValueError("Invalid Compose project name")
    if not Path(config["compose_file"]).is_absolute():
        raise ValueError("Compose file must be an absolute path")
    # Old configurations retain GPU checks. Only an explicit JSON false opts out.
    if type(config.get("require_gpu", True)) is not bool:
        raise ValueError("require_gpu must be a boolean")
    evaluate_residency([], config["model"], config.get("gpu_offload_policy", "full"),
                       config.get("gpu_min_vram_fraction", 0.5))
    for key in (
        "failure_threshold", "restart_cooldown", "restart_limit",
        "restart_window", "startup_timeout",
    ):
        if not isinstance(config[key], int) or config[key] <= 0:
            raise ValueError("Positive integer settings required")


def probe_health(config):
    """A hung Docker/GPU probe participates in the same bounded recovery policy."""
    try:
        return _probe_health(config)
    except subprocess.TimeoutExpired:
        return "unhealthy"


def _probe_health(config):
    """Return healthy/starting/unhealthy/missing_model without exposing data."""
    result = command(compose_command(config) + ["ps", "--all", "--quiet", "ollama"])
    if result.returncode:
        raise RuntimeError("Compose status unavailable")
    container = result.stdout.decode().strip()
    if not container:
        return "unhealthy"
    if not re.fullmatch(r"[a-f0-9]{12,64}", container):
        raise RuntimeError("Unexpected container identifier")
    result = command([
        "/usr/bin/docker", "inspect", "--format", "{{json .State}}", container,
    ])
    if result.returncode:
        return "unhealthy"
    state = json.loads(result.stdout)
    if state.get("Status") != "running":
        return "unhealthy"
    health = state.get("Health", {}).get("Status")
    if health == "starting":
        return "starting"
    if health != "healthy":
        return "unhealthy"

    # HTTP can remain healthy after a host driver/cgroup event removes CUDA
    # devices. Probe GPU access inside this container without loading a model.
    if config.get("require_gpu", True):
        result = command([
            "/usr/bin/docker", "exec", container, "nvidia-smi",
            "--query-gpu=uuid", "--format=csv,noheader",
        ])
        if result.returncode or not result.stdout.strip():
            return "unhealthy"

    # Do not route localhost probes through an environment-configured proxy.
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(config["api_url"] + "/api/tags", timeout=10) as response:
            if response.status != 200:
                return "unhealthy"
            body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            return "unhealthy"
        models = json.loads(body)["models"]
        if not isinstance(models, list):
            return "unhealthy"
        names = {item.get("name") for item in models if isinstance(item, dict)}
        if config["model"] not in names:
            return "missing_model"
        if config.get("require_gpu", True):
            with opener.open(config["api_url"] + "/api/ps", timeout=10) as response:
                if response.status != 200:
                    return "unhealthy"
                body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                return "unhealthy"
            placement = evaluate_residency(
                json.loads(body)["models"], config["model"],
                config.get("gpu_offload_policy", "full"),
                config.get("gpu_min_vram_fraction", 0.5),
            )
            # Loaded CPU fallback violates policy even when NVML still works.
            # A model unloaded by keep_alive is healthy and is not force-warmed.
            if placement == "violation":
                return "unhealthy"
        return "healthy"
    except (OSError, ValueError, KeyError, TypeError):
        return "unhealthy"


def recovery_decision(config, state, health, now):
    """Pure decision function; testable without Docker, a GPU, or networking."""
    restarts = [
        timestamp for timestamp in state.get("restarts", [])
        if now - timestamp < config["restart_window"]
    ]
    updated = {"restarts": restarts, "failures": 0}
    if health in ("healthy", "starting"):
        return health, updated
    if health == "missing_model":
        # Restarting cannot restore a deleted model. Alert instead of looping.
        return "required_model_missing", updated
    updated["failures"] = state.get("failures", 0) + 1
    if updated["failures"] < config["failure_threshold"]:
        return "failure_observed", updated
    if len(restarts) >= config["restart_limit"]:
        return "restart_limit_reached", updated
    if restarts and now - restarts[-1] < config["restart_cooldown"]:
        return "restart_cooldown", updated
    # Record attempted recovery before doing it; failures consume the budget.
    updated["restarts"] = restarts + [now]
    updated["failures"] = 0
    return "restart", updated


def save_state(state):
    temporary = STATE_PATH.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(state, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, STATE_PATH)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    # An explicit administrative stop must not be undone by the timer.
    if command(["/usr/bin/systemctl", "is-active", "--quiet", "ollama.service"]).returncode:
        return 0
    with open(args.config, encoding="utf-8") as stream:
        config = json.load(stream)
    validate_config(config)
    lock = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            event("maintenance_active")
            return 0
        # Recheck after the lock in case an administrator stopped the service.
        if command(["/usr/bin/systemctl", "is-active", "--quiet", "ollama.service"]).returncode:
            return 0
        state = {"restarts": [], "failures": 0}
        if STATE_PATH.exists():
            with STATE_PATH.open(encoding="utf-8") as stream:
                state = json.load(stream)
        action, state = recovery_decision(config, state, probe_health(config), time.time())
        save_state(state)
        if action == "restart":
            event("recovery_started")
            result = command(
                compose_command(config) + [
                    "up", "--detach", "--force-recreate", "--wait", "--wait-timeout",
                    str(config["startup_timeout"]), "--pull", "never", "ollama",
                ],
                timeout=config["startup_timeout"] + 60,
            )
            event("recovery_succeeded" if result.returncode == 0 else "recovery_failed")
            return 0 if result.returncode == 0 else 1
        if action not in ("healthy", "starting"):
            event(action)
            return 1
        return 0
    finally:
        os.close(lock)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.TimeoutExpired):
        event("watchdog_error")
        sys.exit(1)
