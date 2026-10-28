import os
import stat

import bcrypt
import pytest

from conftest import load_module


@pytest.fixture
def credentials():
    return load_module("api_credentials", "scripts/create_api_credentials.py")


@pytest.fixture
def bcrypt_reader():
    return load_module("protected_bcrypt", "lookup_plugins/protected_bcrypt.py")


@pytest.fixture
def generated(credentials, tmp_path):
    # No network call is made; this address is only parsed for the local file.
    directory = tmp_path / "protected"
    credentials.create_credentials(str(directory), "8.8.8.8", "llm")
    return directory


def test_generated_material_is_private_and_hash_matches(generated, bcrypt_reader, tmp_path):
    for path in generated.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert path.stat().st_uid == os.getuid()
    assert stat.S_IMODE(generated.stat().st_mode) == 0o700
    config = (generated / "client.curl").read_text()
    user_line = next(line for line in config.splitlines() if line.startswith('user = "'))
    password = user_line.split(":", 1)[1].rstrip('"')
    value = bcrypt_reader.read_bcrypt(str(generated / "password.bcrypt"), tmp_path / "checkout")
    if not bcrypt.checkpw(password.encode(), value.encode()):
        raise AssertionError("Generated password does not match the protected hash")
    assert 'proto = "=https"' in config
    assert "no-insecure" in config and "no-location-trusted" in config


def test_existing_credential_directory_is_never_overwritten(credentials, generated):
    with pytest.raises(FileExistsError):
        credentials.create_credentials(str(generated), "8.8.8.8", "llm")


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "2001:4860:4860::8888", "example.org", "https://8.8.8.8"])
def test_only_public_ipv4_addresses_are_accepted(credentials, tmp_path, address):
    with pytest.raises(ValueError):
        credentials.create_credentials(str(tmp_path / "new"), address, "llm")


@pytest.mark.parametrize("mode", [0o644, 0o660, 0o700])
def test_hash_with_shared_or_executable_permissions_is_rejected(generated, bcrypt_reader, tmp_path, mode):
    path = generated / "password.bcrypt"
    path.chmod(mode)
    with pytest.raises(ValueError):
        bcrypt_reader.read_bcrypt(str(path), tmp_path / "checkout")


def test_shared_parent_directory_is_rejected(generated, bcrypt_reader, tmp_path):
    generated.chmod(0o755)
    with pytest.raises(ValueError):
        bcrypt_reader.read_bcrypt(str(generated / "password.bcrypt"), tmp_path / "checkout")


def test_symlinked_credential_parent_is_rejected(generated, bcrypt_reader, tmp_path):
    link = tmp_path / "linked"
    link.symlink_to(generated, target_is_directory=True)
    with pytest.raises(OSError):
        bcrypt_reader.read_bcrypt(str(link / "password.bcrypt"), tmp_path / "checkout")


def test_repository_credential_is_rejected(generated, bcrypt_reader):
    with pytest.raises(ValueError):
        bcrypt_reader.read_bcrypt(str(generated / "password.bcrypt"), generated)


def test_configuration_injection_in_hash_file_is_rejected(generated, bcrypt_reader, tmp_path):
    (generated / "password.bcrypt").write_text("not-a-hash\n}\nrespond 200\n")
    with pytest.raises(ValueError):
        bcrypt_reader.read_bcrypt(str(generated / "password.bcrypt"), tmp_path / "checkout")
