from pathlib import Path
import subprocess

import pytest

from conftest import load_module


@pytest.fixture
def gpu_collector():
    return load_module("gpu_textfile", "roles/monitoring/files/gpu_textfile.py")


def test_gpu_units_labels_and_optional_power(gpu_collector):
    metrics = "\n".join(gpu_collector.readings('0, GPU-test, NVIDIA Test, 42, 512, 16384, 25, N/A\n'))
    assert 'llm_gpu_memory_used_bytes{index="0",uuid="GPU-test"} 536870912' in metrics
    assert 'llm_gpu_utilization_ratio{index="0",uuid="GPU-test"} 0.25' in metrics
    assert 'llm_gpu_info{index="0",uuid="GPU-test",name="NVIDIA Test"} 1' in metrics
    assert "llm_gpu_power_watts" not in metrics


@pytest.mark.parametrize("text", [
    "", "0, GPU-test, GPU, 42, 512, 16384, 25\n",
    "0, GPU-test, GPU, nan, 512, 16384, 25, 50\n",
    "0, GPU-test, GPU, 42, -1, 16384, 25, 50\n",
    "0, GPU-test, GPU, 42, 512, 16384, 101, 50\n",
    "0, GPU-test, GPU, 42, 512, 16384, 25, 50\n" * 2,
])
def test_invalid_or_empty_samples_fail_closed(gpu_collector, text):
    with pytest.raises(ValueError):
        gpu_collector.readings(text)


def test_timeout_reports_collection_failure_without_stale_gpu_values(gpu_collector, monkeypatch):
    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == 10
        raise subprocess.TimeoutExpired(args[0], 10)
    monkeypatch.setattr(gpu_collector.subprocess, "run", timeout)
    metrics = gpu_collector.collect()
    assert "llm_gpu_collection_success 0\n" in metrics
    assert "llm_gpu_collection_timestamp_seconds " in metrics
    assert "llm_gpu_info" not in metrics


def test_metric_label_escaping(gpu_collector):
    assert gpu_collector.label('GPU "test"\\card\n') == 'GPU \\"test\\"\\\\card\\n'


def test_atomic_publication_replaces_link_without_touching_target(gpu_collector, tmp_path):
    directory = tmp_path / "metrics"
    directory.mkdir(mode=0o755)
    original = tmp_path / "unrelated"
    original.write_text("unchanged")
    destination = directory / "gpu.prom"
    destination.symlink_to(original)
    gpu_collector.publish(destination, "llm_gpu_collection_success 1\n")
    assert original.read_text() == "unchanged"
    assert not destination.is_symlink()
    assert destination.stat().st_mode & 0o777 == 0o644
    assert not list(directory.glob(".gpu-*"))


def test_world_writable_collector_directory_is_rejected(gpu_collector, tmp_path):
    directory = tmp_path / "unsafe"
    directory.mkdir()
    directory.chmod(0o777)
    with pytest.raises(ValueError):
        gpu_collector.publish(directory / "gpu.prom", "not written\n")


def test_symbolic_link_collector_directory_is_rejected(gpu_collector, tmp_path):
    directory = tmp_path / "link"
    directory.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        gpu_collector.publish(directory / "gpu.prom", "not written\n")
