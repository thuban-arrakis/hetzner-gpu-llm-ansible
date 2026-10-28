import pytest

from conftest import load_module


@pytest.mark.parametrize("size,vram,policy,fraction,expected", [
    (1000, 1000, "full", 0.5, "ok"),
    (1000, 999, "full", 0.5, "violation"),
    (1000, 600, "partial", 0.5, "ok"),
    (1000, 600, "partial", 0.7, "violation"),
    (1000, 0, "partial", 0.1, "violation"),
    (0, 0, "full", 0.5, "violation"),
    (1000, 1001, "full", 0.5, "violation"),
    (1000, True, "full", 0.5, "violation"),
    (1000, "1000", "full", 0.5, "violation"),
])
def test_residency_policy_matches_exact_full_offload_and_opt_in_partial(size, vram, policy, fraction, expected):
    module = load_module("gpu_residency", "roles/ollama/files/gpu_residency.py")
    assert module.evaluate_residency(
        [{"name": "model:tag", "size": size, "size_vram": vram}], "model:tag", policy, fraction
    ) == expected


def test_idle_and_malformed_residency_are_different_states():
    module = load_module("gpu_residency", "roles/ollama/files/gpu_residency.py")
    assert module.evaluate_residency([], "model:tag") == "idle"
    assert module.evaluate_residency([None], "model:tag") == "violation"
    assert module.evaluate_residency({}, "model:tag") == "violation"
    assert module.evaluate_residency([{"name": "model:tag"}] * 2, "model:tag") == "violation"
