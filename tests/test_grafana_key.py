from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import load_module


@pytest.fixture
def generator(tmp_path, monkeypatch):
    module = load_module("grafana_key", "roles/monitoring/files/generate_grafana_key.py")
    module.KEY_PATH = tmp_path / "signing-key"
    real_lstat = Path.lstat

    def metadata(path, *args, **kwargs):
        result = real_lstat(path, *args, **kwargs)
        if path == tmp_path:
            return SimpleNamespace(st_mode=result.st_mode, st_uid=0)
        return result

    monkeypatch.setattr(Path, "lstat", metadata)
    return module


def test_new_key_stays_in_owner_only_file_and_emits_nothing(generator, monkeypatch, capsys):
    monkeypatch.setattr(generator.os, "fchown", lambda *args: None)
    assert generator.main() == 2
    metadata = generator.KEY_PATH.stat()
    assert metadata.st_size == 65
    assert metadata.st_mode & 0o777 == 0o400
    assert capsys.readouterr() == ("", "")


def test_existing_key_with_wrong_metadata_is_not_replaced(generator, capsys):
    generator.KEY_PATH.touch(mode=0o644)
    assert generator.main() == 1
    assert generator.KEY_PATH.stat().st_size == 0
    assert capsys.readouterr() == ("", "")


def test_symbolic_link_is_rejected_without_writing_target(generator, tmp_path):
    target = tmp_path / "other"
    target.touch(mode=0o600)
    generator.KEY_PATH.symlink_to(target)
    assert generator.main() == 1
    assert target.stat().st_size == 0


def test_writable_parent_directory_is_rejected(generator, tmp_path):
    tmp_path.chmod(0o777)
    assert generator.main() == 1
    assert not generator.KEY_PATH.exists()
