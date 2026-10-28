import os
import shutil
import subprocess

import pytest
import yaml

from conftest import ROOT, load_module


@pytest.mark.parametrize("accelerator", ["nvidia", "cpu"])
def test_compose_configuration_passes_docker_schema_validation(tmp_path, accelerator):
    compose = None
    if shutil.which("docker"):
        available = subprocess.run(["docker", "compose", "version"], capture_output=True, check=False)
        if available.returncode == 0:
            compose = ["docker", "compose"]
    if compose is None and shutil.which("docker-compose"):
        compose = ["docker-compose"]
    if compose is None:
        if os.environ.get("CI"):
            pytest.fail("Docker Compose v2 is required in CI")
        pytest.skip("Docker Compose v2 is unavailable locally")
    renderer = load_module("compose_renderer", "scripts/render_compose.py")
    config = tmp_path / "compose.yml"
    config.write_text(renderer.render(ROOT, accelerator))
    result = subprocess.run(
        [*compose, "--file", str(config), "config", "--quiet"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_cpu_mode_omits_device_reservations_and_gpu_mode_retains_them():
    renderer = load_module("compose_renderer", "scripts/render_compose.py")
    gpu = yaml.safe_load(renderer.render(ROOT))["services"]["ollama"]
    cpu = yaml.safe_load(renderer.render(ROOT, "cpu"))["services"]["ollama"]
    assert gpu["deploy"]["resources"]["reservations"]["devices"][0]["driver"] == "nvidia"
    assert "deploy" not in cpu
    assert "NVIDIA_DRIVER_CAPABILITIES" not in cpu["environment"]
    assert cpu["environment"]["CUDA_VISIBLE_DEVICES"] == "-1"
    assert cpu["environment"]["OLLAMA_VULKAN"] == "0"
    assert cpu["ports"] == gpu["ports"] == ["127.0.0.1:11434:11434"]
