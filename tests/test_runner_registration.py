import json
import os
import secrets
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from conftest import load_module


@pytest.fixture
def creator(monkeypatch):
    module = load_module("runner_token_creator", "scripts/create_runner_registration_token.py")
    monkeypatch.setattr(module.shutil, "which", lambda *args, **kwargs: "/usr/bin/gh")
    return module


@pytest.fixture
def output(tmp_path):
    directory = tmp_path / "token-output"
    directory.mkdir(mode=0o700)
    return directory / "registration.json"


def creator_args(monkeypatch, output, repository="example/private-demo"):
    monkeypatch.setattr(sys, "argv", ["create-token", "--repository", repository, "--output", str(output)])


def forbidden_process(*args, **kwargs):
    raise AssertionError("Rejected input must not launch a credential-bearing process")


def test_creation_reserves_private_sink_before_request(creator, output, monkeypatch, capsys):
    token = secrets.token_urlsafe(32)
    calls = []
    creator_args(monkeypatch, output)
    for name in ("GH_DEBUG", "HTTPS_PROXY", "SSL_CERT_FILE"):
        monkeypatch.setenv(name, "untrusted-override")

    def run(arguments, **kwargs):
        calls.append(True)
        assert output.exists() and output.stat().st_size == 0
        assert stat.S_IMODE(output.stat().st_mode) == 0o600
        assert arguments[1:] == ["api", "--hostname", "github.com", "--method", "POST",
                                  "repos/example/private-demo/actions/runners/registration-token"]
        assert all(name not in kwargs["env"] for name in ("GH_DEBUG", "HTTPS_PROXY", "SSL_CERT_FILE"))
        assert kwargs["capture_output"] is True and "input" not in kwargs
        return subprocess.CompletedProcess(arguments, 0, json.dumps({"token": token}).encode(), b"")

    monkeypatch.setattr(creator.subprocess, "run", run)
    creator.main()
    if json.loads(output.read_text())["token"] != token:
        raise AssertionError("Protected output did not preserve the generated token")
    captured = capsys.readouterr()
    if token in captured.out or token in captured.err:
        raise AssertionError("Generated credential escaped to output")
    assert calls == [True]


@pytest.mark.parametrize("repository", ["example/..", "../demo", "example/.", "./demo", "example/demo?x=1"])
def test_creation_rejects_repository_path_abuse_before_request(creator, output, monkeypatch, repository):
    creator_args(monkeypatch, output, repository)
    monkeypatch.setattr(creator.subprocess, "run", forbidden_process)
    with pytest.raises(ValueError):
        creator.main()
    assert not output.exists()


@pytest.mark.parametrize("kind", ["existing", "symlink", "shared_parent", "shared_ancestor"])
def test_creation_rejects_unsafe_output_before_request(creator, output, monkeypatch, kind):
    if kind == "existing":
        output.touch(mode=0o600)
    elif kind == "symlink":
        output.symlink_to(output.parent / "missing")
    elif kind == "shared_parent":
        output.parent.chmod(0o755)
    else:
        output.parent.parent.chmod(0o777)
    creator_args(monkeypatch, output)
    monkeypatch.setattr(creator.subprocess, "run", forbidden_process)
    with pytest.raises((ValueError, OSError)):
        creator.main()


@pytest.mark.parametrize("kind", ["duplicate", "array", "failure", "timeout"])
def test_creation_failure_is_sanitized_and_removes_reserved_sink(creator, output, monkeypatch, capsys, kind):
    token = secrets.token_urlsafe(32)
    creator_args(monkeypatch, output)

    def run(arguments, **kwargs):
        if kind == "timeout":
            raise subprocess.TimeoutExpired(arguments, 60, output=token.encode())
        payload = json.dumps({"token": token})
        if kind == "duplicate":
            payload = "{" + ",".join([json.dumps("token") + ":" + json.dumps(token)] * 2) + "}"
        elif kind == "array":
            payload = json.dumps([token])
        return subprocess.CompletedProcess(arguments, 1 if kind == "failure" else 0,
                                           payload.encode(), token.encode())

    monkeypatch.setattr(creator.subprocess, "run", run)
    with pytest.raises((ValueError, RuntimeError, subprocess.TimeoutExpired)):
        creator.main()
    assert not output.exists()
    captured = capsys.readouterr()
    if token in captured.out or token in captured.err:
        raise AssertionError("Generated credential escaped to output")


@pytest.fixture
def configured_fixture(tmp_path, monkeypatch):
    module = load_module("runner_configurator", "scripts/configure_actions_runner.py")
    base = tmp_path / "runner-base"
    base.mkdir(mode=0o700)
    for name in ("runner", "protected", "home", "tmp", "cache", "work"):
        (base / name).mkdir(mode=0o700)
    script = base / "runner/config.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o700)
    token = secrets.token_urlsafe(32)
    source = base / "protected/registration-token.json"
    source.write_text(json.dumps({"token": token}))
    source.chmod(0o600)
    monkeypatch.setattr(module, "BASE", base)
    account = SimpleNamespace(pw_name="llm-runner", pw_dir=str(base / "home"), pw_gid=os.getgid())
    monkeypatch.setattr(module.pwd, "getpwuid", lambda uid: account)
    monkeypatch.setenv("HOME", account.pw_dir)
    monkeypatch.setattr(sys, "argv", ["configure-runner", "--repository", "example/private-demo",
                                     "--name", "demo-arm-runner"])
    return module, base, source, token


def test_configuration_token_only_in_environment_and_removed_afterward(configured_fixture, monkeypatch, capsys):
    module, base, source, token = configured_fixture
    environment_reference = []

    def run(arguments, **kwargs):
        if any(token in argument for argument in arguments) or "input" in kwargs:
            raise AssertionError("Credential must not enter arguments or stdin")
        if kwargs["env"].get("ACTIONS_RUNNER_INPUT_TOKEN") != token:
            raise AssertionError("Registration environment did not receive the token")
        assert kwargs["env"]["HOME"] == os.environ["HOME"]
        assert kwargs["capture_output"] is True
        environment_reference.append(kwargs["env"])
        for name in (".runner", ".credentials", ".credentials_rsaparams"):
            (base / "runner" / name).write_text("{}")
        return subprocess.CompletedProcess(arguments, 0, token.encode(), token.encode())

    monkeypatch.setattr(module.subprocess, "run", run)
    module.main()
    assert not source.exists()
    assert "ACTIONS_RUNNER_INPUT_TOKEN" not in environment_reference[0]
    for name in (".runner", ".credentials", ".credentials_rsaparams"):
        assert stat.S_IMODE((base / "runner" / name).stat().st_mode) == 0o600
    captured = capsys.readouterr()
    if token in captured.out or token in captured.err:
        raise AssertionError("Generated credential escaped to output")


@pytest.mark.parametrize("kind", ["shared_base", "shared_runner", "script_symlink", "script_shared",
                                 "token_symlink", "token_hardlink", "token_shared", "home_mismatch",
                                 "duplicate", "array", "already_configured", "repository_traversal"])
def test_configuration_rejects_unsafe_inputs_before_process(configured_fixture, monkeypatch, kind):
    module, base, source, token = configured_fixture
    if kind == "shared_base":
        base.chmod(0o777)
    elif kind == "shared_runner":
        (base / "runner").chmod(0o755)
    elif kind == "script_symlink":
        script = base / "runner/config.sh"
        script.rename(base / "runner/real-config.sh")
        script.symlink_to(base / "runner/real-config.sh")
    elif kind == "script_shared":
        (base / "runner/config.sh").chmod(0o777)
    elif kind == "token_symlink":
        source.rename(source.parent / "real-token.json")
        source.symlink_to(source.parent / "real-token.json")
    elif kind == "token_hardlink":
        os.link(source, source.parent / "linked-token.json")
    elif kind == "token_shared":
        source.chmod(0o644)
    elif kind == "home_mismatch":
        monkeypatch.setenv("HOME", str(base / "another-home"))
    elif kind == "duplicate":
        source.write_text("{" + ",".join([json.dumps("token") + ":" + json.dumps(token)] * 2) + "}")
    elif kind == "array":
        source.write_text(json.dumps([token]))
    elif kind == "already_configured":
        (base / "runner/.runner").symlink_to(base / "missing")
    elif kind == "repository_traversal":
        monkeypatch.setattr(sys, "argv", ["configure-runner", "--repository", "example/..", "--name", "demo"])
    monkeypatch.setattr(module.subprocess, "run", forbidden_process)
    with pytest.raises((ValueError, OSError)):
        module.main()


@pytest.mark.parametrize("timeout", [False, True])
def test_configuration_failure_retains_source_and_clears_environment(configured_fixture, monkeypatch, capsys, timeout):
    module, base, source, token = configured_fixture
    environment_reference = []

    def run(arguments, **kwargs):
        environment_reference.append(kwargs["env"])
        if timeout:
            raise subprocess.TimeoutExpired(arguments, 180, output=token.encode())
        return subprocess.CompletedProcess(arguments, 1, token.encode(), token.encode())

    monkeypatch.setattr(module.subprocess, "run", run)
    with pytest.raises((RuntimeError, subprocess.TimeoutExpired)):
        module.main()
    assert source.exists()
    assert "ACTIONS_RUNNER_INPUT_TOKEN" not in environment_reference[0]
    captured = capsys.readouterr()
    if token in captured.out or token in captured.err:
        raise AssertionError("Generated credential escaped to output")
