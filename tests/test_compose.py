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


def test_gpu_uses_explicit_nvidia_runtime_and_cpu_hides_all_devices():
    renderer = load_module("compose_renderer", "scripts/render_compose.py")
    gpu = yaml.safe_load(renderer.render(ROOT))["services"]["ollama"]
    cpu = yaml.safe_load(renderer.render(ROOT, "cpu"))["services"]["ollama"]
    assert gpu["runtime"] == "nvidia"
    assert gpu["environment"]["NVIDIA_VISIBLE_DEVICES"] == "all"
    assert "deploy" not in gpu
    assert gpu["environment"]["OLLAMA_GPU_OVERHEAD"] == str(512 * 1048576)
    assert "runtime" not in cpu
    assert "deploy" not in cpu
    assert "NVIDIA_DRIVER_CAPABILITIES" not in cpu["environment"]
    assert cpu["environment"]["NVIDIA_VISIBLE_DEVICES"] == "void"
    assert cpu["environment"]["CUDA_VISIBLE_DEVICES"] == "-1"
    assert cpu["environment"]["OLLAMA_VULKAN"] == "0"
    assert cpu["ports"] == gpu["ports"] == ["127.0.0.1:11434:11434"]


def test_selected_gpu_uuid_is_visible_without_docker_device_request_injection():
    import json
    from jinja2 import Environment, FileSystemLoader, StrictUndefined

    role = ROOT / "roles/ollama"
    config = yaml.safe_load((role / "defaults/main.yml").read_text())
    uuid = "GPU-12345678-1234-1234-1234-123456789abc"
    config.update(inference_accelerator="nvidia", ollama_gpu_device_uuids=[uuid])
    environment = Environment(loader=FileSystemLoader(role / "templates"), undefined=StrictUndefined)
    environment.filters["to_json"] = json.dumps
    rendered = yaml.safe_load(environment.get_template("compose.yml.j2").render(config))
    service = rendered["services"]["ollama"]
    assert "deploy" not in service
    assert service["environment"]["NVIDIA_VISIBLE_DEVICES"] == uuid
    assert service["environment"]["CUDA_VISIBLE_DEVICES"] == uuid
