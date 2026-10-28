import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def watchdog(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "roles/ollama/files"))
    return load_module("watchdog", "roles/ollama/files/watchdog.py")


@pytest.fixture
def config():
    return {
        "api_url": "http://127.0.0.1:11434",
        "project_name": "gpu-llm",
        "compose_file": "/opt/ollama/compose.yml",
        "model": "qwen3:4b-q4_K_M",
        "failure_threshold": 2,
        "restart_cooldown": 300,
        "restart_limit": 3,
        "restart_window": 3600,
        "startup_timeout": 180,
    }
