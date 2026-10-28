"""Exercise driver failure/retry control flow without changing host packages.

Ansible executes the real role task ordering, assertions, conditions, and reboot
checkpoint. OS package/driver/reboot operations are replaced with local stubs;
this is not a GPU hardware acceptance test.
"""

import copy
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

from conftest import ROOT


def run_driver_tasks(tmp_path, *, failure=None, usable_driver=False, allow_reboot=True):
    tasks = yaml.safe_load((ROOT / "roles/gpu_host/tasks/driver.yml").read_text())
    marker = tmp_path / "gpu-reboot-required"
    rebooted = tmp_path / "rebooted"
    paths = {
        "/var/run/gpu-host-driver-reboot-required": str(marker),
        "/var/run/reboot-required": str(tmp_path / "os-reboot-required"),
        "/sys/firmware/efi": str(tmp_path / "absent-efi"),
    }
    for task in tasks:
        action = next(key for key in task if key.startswith("ansible.builtin."))
        arguments = task[action]
        if action == "ansible.builtin.copy":
            arguments["dest"] = paths[arguments["dest"]]
            arguments.pop("owner", None)
            arguments.pop("group", None)
        elif action == "ansible.builtin.stat":
            arguments["path"] = paths[arguments["path"]]
        elif action == "ansible.builtin.command":
            command = arguments["argv"][0]
            if command == "nvidia-smi":
                code = "print('580.65.06')" if usable_driver or "after" in task["register"] else "raise SystemExit(1)"
            elif command == "/usr/sbin/modinfo":
                code = "print('580.65.06')"
            elif command == "/usr/bin/mokutil":
                code = "print('SecureBoot disabled')"
            elif command == "/usr/bin/ubuntu-drivers":
                code = "print('Driver package installation stub')"
            else:
                raise AssertionError(f"Unstubbed host command: {command}")
            arguments["argv"] = [sys.executable, "-c", code]
        elif action == "ansible.builtin.apt":
            del task[action]
            task["ansible.builtin.debug"] = {"msg": "Package operation stub"}
        elif action == "ansible.builtin.package_facts":
            del task[action]
            task["ansible.builtin.set_fact"] = {"ansible_facts": {"packages": {}}}
        elif action == "ansible.builtin.reboot":
            del task[action]
            task["ansible.builtin.command"] = {
                "argv": [sys.executable, "-c", (
                    "from pathlib import Path; "
                    f"Path({str(rebooted)!r}).write_text('rebooted'); "
                    f"Path({str(marker)!r}).unlink(missing_ok=True)"
                )],
            }
        elif action not in {"ansible.builtin.assert", "ansible.builtin.set_fact"}:
            raise AssertionError(f"Unstubbed host action: {action}")
        if task["name"] == failure:
            for key in list(task):
                if key.startswith("ansible.builtin.") or key in {"register", "changed_when"}:
                    del task[key]
            task["ansible.builtin.fail"] = {"msg": "Injected interrupted installation"}

    defaults = yaml.safe_load((ROOT / "roles/gpu_host/defaults/main.yml").read_text())
    defaults.update({
        "gpu_host_driver_package": "nvidia-driver-580-open",
        "gpu_host_allow_reboot": allow_reboot,
        "ansible_facts": {"kernel": "test", "packages": {"nvidia-driver-580-open": []} if usable_driver else {}},
        "ansible_python_interpreter": sys.executable,
    })
    playbook = tmp_path / "driver-test.yml"
    playbook.write_text(yaml.safe_dump([{
        "name": "Driver checkpoint regression",
        "hosts": "localhost",
        "connection": "local",
        "gather_facts": False,
        "vars": defaults,
        "tasks": tasks,
    }], sort_keys=False))
    ansible = shutil.which("ansible-playbook") or str(Path(sys.executable).with_name("ansible-playbook"))
    env = dict(os.environ, ANSIBLE_HOME=str(tmp_path / "ansible"),
               ANSIBLE_LOCAL_TEMP=str(tmp_path / "ansible/local"),
               ANSIBLE_REMOTE_TEMP=str(tmp_path / "ansible/remote"),
               ANSIBLE_NOCOLOR="1")
    return subprocess.run(
        [ansible, "-i", "localhost,", str(playbook)],
        cwd=ROOT, env=env, text=True, capture_output=True, check=False,
    )


@pytest.mark.parametrize("failure", [
    "Install the explicitly selected Ubuntu NVIDIA driver",
    "Inspect the installed NVIDIA kernel module before rebooting",
    "Install nvidia-smi utilities matching the selected driver family",
])
def test_interrupted_driver_changes_preserve_reboot_checkpoint(tmp_path, failure):
    result = run_driver_tasks(tmp_path, failure=failure)
    assert result.returncode != 0
    assert "Injected interrupted installation" in result.stdout
    assert (tmp_path / "gpu-reboot-required").is_file(), result.stdout + result.stderr
    assert not (tmp_path / "rebooted").exists()


@pytest.mark.parametrize("allow_reboot", [False, True])
def test_retry_honors_checkpoint_even_if_driver_now_looks_usable(tmp_path, allow_reboot):
    marker = tmp_path / "gpu-reboot-required"
    marker.write_text("Interrupted previous driver installation\n")
    result = run_driver_tasks(tmp_path, usable_driver=True, allow_reboot=allow_reboot)
    assert (result.returncode == 0) is allow_reboot, result.stdout + result.stderr
    assert (tmp_path / "rebooted").exists() is allow_reboot
    assert marker.exists() is not allow_reboot
    if not allow_reboot:
        assert "A host reboot is pending" in result.stdout


@pytest.mark.parametrize("mode,success", [("jit-cdi", True), ("legacy", False), ("auto", False), (None, False)])
def test_runtime_verification_rejects_implicit_or_legacy_device_injection(tmp_path, mode, success):
    tasks = yaml.safe_load((ROOT / "roles/gpu_host/tasks/docker.yml").read_text())
    task = next(task for task in tasks if task["name"] == "Verify that NVIDIA device injection uses JIT-CDI")
    arguments = copy.deepcopy(task["ansible.builtin.command"]["argv"])
    config = tmp_path / "config.toml"
    config.write_text("[nvidia-container-runtime]\n" + (f'mode = "{mode}"\n' if mode is not None else ""))
    arguments[0] = sys.executable
    arguments[-1] = arguments[-1].replace("/etc/nvidia-container-runtime/config.toml", str(config))
    result = subprocess.run(arguments, text=True, capture_output=True, check=False)
    assert (result.returncode == 0) is success, result.stdout + result.stderr
