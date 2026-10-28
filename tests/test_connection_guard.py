"""Exercise firewall plans with fake subprocesses; never touch host iptables."""

import shlex
from types import SimpleNamespace

import pytest

from conftest import load_module


@pytest.fixture
def guard():
    return load_module("connection_guard", "roles/public_api/files/connection_guard.py")


class FakeFirewall:
    def __init__(self, guard, existing=False, extra_input=None):
        self.guard = guard
        self.existing = existing
        self.extra_input = extra_input or []
        self.calls = []
        self.bad_rule = False
        self.fail_preflight = False
        self.reverse_rules = False

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        assert kwargs["capture_output"] and kwargs["timeout"] == 15
        if argv[0].endswith("-restore"):
            status = 1 if self.fail_preflight and "--test" in argv else 0
            return SimpleNamespace(returncode=status, stdout="", stderr="private diagnostics")
        binary, _, _, _, _, operation, chain, *rule = argv
        mask, suffix = (128, "6") if "ip6tables" in binary else (32, "4")
        if operation == "-S" and chain == self.guard.CHAIN:
            if not self.existing:
                return SimpleNamespace(returncode=1, stdout="", stderr="absent")
            rules = self.guard.rules(mask, suffix)
            if self.reverse_rules:
                rules.reverse()
            if self.bad_rule:
                rules.append(["-j", "ACCEPT"])
            output = "-N " + chain + "\n" + "\n".join(shlex.join(["-A", chain, *r]) for r in rules)
        elif operation == "-S" and chain == "INPUT":
            input_rules = [*self.extra_input]
            if self.existing:
                input_rules.append(self.guard.INPUT_RULE)
            output = "-P INPUT DROP\n" + "\n".join(shlex.join(["-A", "INPUT", *r]) for r in input_rules)
        elif operation == "-C":
            return SimpleNamespace(returncode=0 if self.existing else 1, stdout="", stderr="")
        else:
            raise AssertionError("Unexpected firewall command")
        return SimpleNamespace(returncode=0, stdout=output, stderr="")


def test_install_preflights_both_families_and_preserves_unrelated_rules(guard, monkeypatch):
    fake = FakeFirewall(guard)
    monkeypatch.setattr(guard.subprocess, "run", fake)
    assert guard.apply()
    restores = [(argv, kw["input"]) for argv, kw in fake.calls if argv[0].endswith("-restore")]
    assert len(restores) == 4
    assert all("--test" in argv for argv, _ in restores[:2])
    assert all("--test" not in argv for argv, _ in restores[2:])
    for argv, payload in restores:
        assert "--noflush" in argv
        assert payload.count("COMMIT") == 1
        assert ":INPUT" not in payload and "-F INPUT" not in payload and "-P " not in payload
        assert "--dports 80,443" in payload and "--ctstate NEW" in payload
        assert "--tcp-flags FIN,SYN,RST,ACK SYN" in payload
        assert "--connlimit-above 256 --connlimit-mask 0" in payload
        assert "--connlimit-above 32" in payload
        assert "--hashlimit-above 40/second --hashlimit-burst 80" in payload
        assert "--hashlimit-above 8/second --hashlimit-burst 16" in payload
        assert "--hashlimit-htable-max 4096" in payload
        assert "-j LOG" not in payload and "-j ACCEPT" not in payload
        assert "22" not in payload and "FORWARD" not in payload and "OUTPUT" not in payload
    assert "--connlimit-mask 32" in restores[0][1]
    assert "--connlimit-mask 128" in restores[1][1]


def test_unchanged_install_does_not_reset_live_limits(guard, monkeypatch):
    fake = FakeFirewall(guard, existing=True)
    monkeypatch.setattr(guard.subprocess, "run", fake)
    assert not guard.apply()
    assert not any(argv[0].endswith("-restore") for argv, _ in fake.calls)


def test_preflight_failure_never_commits_or_flushes_existing_rules(guard, monkeypatch):
    fake = FakeFirewall(guard, existing=True, extra_input=[["-p", "tcp", "--dport", "22", "-j", "ACCEPT"]])
    fake.fail_preflight = True
    monkeypatch.setattr(guard.subprocess, "run", fake)
    with pytest.raises(guard.GuardError):
        guard.apply()
    assert not any(argv[0].endswith("-restore") and "--test" not in argv for argv, _ in fake.calls)


def test_permissive_rule_reordering_is_repaired(guard, monkeypatch):
    fake = FakeFirewall(guard, existing=True)
    fake.reverse_rules = True
    monkeypatch.setattr(guard.subprocess, "run", fake)
    assert guard.plan(guard.FAMILIES[0]) is not None


def test_guard_jump_moves_before_accepts_without_rewriting_them(guard, monkeypatch):
    fake = FakeFirewall(guard, existing=True, extra_input=[["-j", "ufw-before-input"]])
    monkeypatch.setattr(guard.subprocess, "run", fake)
    _, payload = guard.plan(guard.FAMILIES[0])
    assert "-I INPUT 1 " in payload and "-D INPUT " in payload
    assert "ufw-before-input" not in payload


def test_unmanaged_rule_collision_is_rejected(guard, monkeypatch):
    fake = FakeFirewall(guard, existing=True)
    fake.bad_rule = True
    monkeypatch.setattr(guard.subprocess, "run", fake)
    with pytest.raises(guard.GuardError):
        guard.apply()
    assert not any(argv[0].endswith("-restore") for argv, _ in fake.calls)


def test_unexpected_ssh_jump_is_never_deleted_or_changed(guard, monkeypatch):
    unsafe = list(guard.INPUT_RULE)
    unsafe[unsafe.index("--dports") + 1] = "22"
    fake = FakeFirewall(guard, existing=True, extra_input=[unsafe])
    monkeypatch.setattr(guard.subprocess, "run", fake)
    with pytest.raises(guard.GuardError):
        guard.apply()
    assert not any(argv[0].endswith("-restore") for argv, _ in fake.calls)


def test_remove_only_deletes_managed_jump_and_chain(guard, monkeypatch):
    fake = FakeFirewall(guard, existing=True, extra_input=[["-j", "ufw-before-input"]])
    monkeypatch.setattr(guard.subprocess, "run", fake)
    assert guard.apply(remove=True)
    for argv, kwargs in fake.calls:
        if argv[0].endswith("-restore"):
            payload = kwargs["input"]
            assert "ufw-before-input" not in payload
            assert "-D INPUT " in payload
            assert "-F LLM_INGRESS\n-X LLM_INGRESS" in payload
            assert "-F INPUT" not in payload and ":INPUT" not in payload


def test_absent_remove_is_noop(guard, monkeypatch):
    monkeypatch.setattr(guard.subprocess, "run", FakeFirewall(guard))
    assert not guard.apply(remove=True)


def test_untrusted_source_never_invokes_firewall(guard, monkeypatch, capsys):
    monkeypatch.setattr(guard.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(guard, "execute", lambda *args, **kwargs: pytest.fail("firewall must not run"))
    monkeypatch.setattr("sys.argv", ["connection_guard.py", "--install"])
    assert guard.main() == 1
    assert capsys.readouterr().out == "connection_guard_failed\n"


def test_error_output_is_sanitized(guard, monkeypatch):
    monkeypatch.setattr(guard.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=2, stdout="must not surface", stderr="must not surface",
    ))
    with pytest.raises(guard.GuardError, match="^firewall command failed$"):
        guard.execute(["/usr/sbin/iptables", "-S"])
