import json
import os
import shutil
import subprocess

from jinja2 import Environment, FileSystemLoader, StrictUndefined
import pytest
import yaml

from conftest import ROOT


def render_monitoring(overrides=None):
    role = ROOT / "roles" / "monitoring"
    values = yaml.safe_load((role / "defaults" / "main.yml").read_text())
    values["public_api_address"] = "192.0.2.10"
    values.update(overrides or {})
    environment = Environment(loader=FileSystemLoader(role / "templates"), undefined=StrictUndefined)
    environment.filters["to_json"] = json.dumps
    return environment.get_template("compose.yml.j2").render(values)


def test_monitoring_compose_has_private_listeners_and_unprivileged_containers():
    services = yaml.safe_load(render_monitoring())["services"]
    for service in services.values():
        assert "ports" not in service
        assert service["network_mode"] == "host"
        assert service["user"].split(":")[0] != "0"
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in service["security_opt"]
        assert "@sha256:" in service["image"]
        assert not any("docker.sock" in volume for volume in service["volumes"])
    assert "prometheus" not in services
    assert "-httpListenAddr=127.0.0.1:8428" in services["victoriametrics"]["command"]
    assert "--web.listen-address=127.0.0.1:9100" in services["node_exporter"]["command"]
    assert services["grafana"]["environment"]["GF_SERVER_HTTP_ADDR"] == "127.0.0.1"


def test_grafana_accepts_only_loopback_proxy_auth_and_creates_no_default_admin():
    settings = yaml.safe_load(render_monitoring())["services"]["grafana"]["environment"]
    assert settings["GF_AUTH_PROXY_WHITELIST"] == "127.0.0.1"
    assert settings["GF_AUTH_PROXY_ENABLE_LOGIN_TOKEN"] == "false"
    assert settings["GF_AUTH_BASIC_ENABLED"] == settings["GF_AUTH_ANONYMOUS_ENABLED"] == "false"
    assert settings["GF_SECURITY_DISABLE_INITIAL_ADMIN_CREATION"] == "true"
    assert settings["GF_AUTH_DISABLE_LOGIN_FORM"] == "true"
    assert settings["GF_USERS_ALLOW_SIGN_UP"] == "false"
    assert settings["GF_SECURITY_SECRET_KEY"].startswith("$$__file{")
    assert not any("PASSWORD" in key for key in settings)


@pytest.mark.parametrize("overrides,hard_limit,go_limit", [
    ({}, "1g", "768MiB"),
    ({"monitoring_grafana_memory": "2g", "monitoring_grafana_go_memory_limit": "1536MiB"}, "2g", "1536MiB"),
])
def test_rendered_grafana_memory_limit_keeps_go_runtime_below_container_cap(overrides, hard_limit, go_limit):
    grafana = yaml.safe_load(render_monitoring(overrides))["services"]["grafana"]
    assert grafana["mem_limit"] == hard_limit
    assert grafana["environment"]["GOMEMLIMIT"] == go_limit
    hard_mib = int(hard_limit.removesuffix("g")) * 1024
    go_mib = int(go_limit.removesuffix("MiB"))
    assert go_mib <= hard_mib * 0.75


def test_monitoring_compose_passes_actual_compose_schema(tmp_path):
    compose = None
    if shutil.which("docker"):
        check = subprocess.run(["docker", "compose", "version"], capture_output=True, check=False)
        if check.returncode == 0:
            compose = ["docker", "compose"]
    if compose is None and shutil.which("docker-compose"):
        compose = ["docker-compose"]
    if compose is None:
        if os.environ.get("CI"):
            pytest.fail("Docker Compose v2 is required in CI")
        pytest.skip("Docker Compose v2 is unavailable locally")
    rendered = tmp_path / "monitoring-compose.yml"
    rendered.write_text(render_monitoring())
    result = subprocess.run([*compose, "--file", str(rendered), "config", "--quiet"], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
