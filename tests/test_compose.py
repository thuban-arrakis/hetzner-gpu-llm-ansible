import os
import shutil
import subprocess

import pytest

from conftest import ROOT, load_module


def test_compose_configuration_passes_docker_schema_validation(tmp_path):
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
    config.write_text(renderer.render(ROOT))
    result = subprocess.run(
        [*compose, "--file", str(config), "config", "--quiet"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
