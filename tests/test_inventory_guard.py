import os
from pathlib import Path

import pytest

from conftest import load_module


@pytest.fixture
def guard():
    return load_module("inventory_guard", "scripts/check_deploy_inventory.py")


@pytest.fixture
def protected_file(tmp_path):
    path = tmp_path / "controller" / "inventory.yml"
    path.parent.mkdir(mode=0o700)
    path.write_text("gpu_servers: {}\n")
    path.chmod(0o600)
    return path


def test_owner_only_inventory_outside_checkout_is_accepted(guard, protected_file, tmp_path):
    assert guard.validate_inventory(str(protected_file), str(tmp_path / "checkout")) == protected_file


@pytest.mark.parametrize("mode", [0o644, 0o660, 0o700])
def test_shared_or_executable_inventory_is_rejected(guard, protected_file, tmp_path, mode):
    protected_file.chmod(mode)
    with pytest.raises(ValueError):
        guard.validate_inventory(str(protected_file), str(tmp_path / "checkout"))


def test_repository_cannot_supply_deployment_inventory(guard, protected_file):
    with pytest.raises(ValueError):
        guard.validate_inventory(str(protected_file), str(protected_file.parent))


def test_symlinked_inventory_is_rejected(guard, protected_file, tmp_path):
    link = tmp_path / "inventory.yml"
    link.symlink_to(protected_file)
    with pytest.raises(ValueError):
        guard.validate_inventory(str(link), str(tmp_path / "checkout"))


def test_symlinked_parent_is_rejected(guard, protected_file, tmp_path):
    link = tmp_path / "linked-controller"
    link.symlink_to(protected_file.parent, target_is_directory=True)
    with pytest.raises(ValueError):
        guard.validate_inventory(str(link / protected_file.name), str(tmp_path / "checkout"))
