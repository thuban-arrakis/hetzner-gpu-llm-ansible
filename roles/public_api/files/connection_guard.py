#!/usr/bin/env python3
"""Manage only the HTTP(S) NEW-SYN ingress guard; never open a firewall port.

Limits apply independently to IPv4 and IPv6. Allowed packets RETURN to the
existing host firewall. Established connections and SSH never enter this chain.
Configuration is static in this root-owned file; there is no external input.
"""

import argparse
import fcntl
import os
from pathlib import Path
import shlex
import stat
import subprocess


CHAIN = "LLM_INGRESS"
MARKER = "llm-connection-guard:"
FAMILIES = (
    ("/usr/sbin/iptables", "/usr/sbin/iptables-restore", 32, "4"),
    ("/usr/sbin/ip6tables", "/usr/sbin/ip6tables-restore", 128, "6"),
)
INPUT_RULE = [
    "-p", "tcp", "-m", "multiport", "--dports", "80,443",
    "-m", "conntrack", "--ctstate", "NEW",
    "-m", "tcp", "--tcp-flags", "FIN,SYN,RST,ACK", "SYN",
    "-m", "comment", "--comment", MARKER + "ingress", "-j", CHAIN,
]


class GuardError(Exception):
    """A fixed, sanitized failure; subprocess output is never printed."""


def execute(argv, payload=None, allowed=(0,)):
    try:
        result = subprocess.run(
            argv, input=payload, capture_output=True, text=True, timeout=15,
            check=False, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise GuardError("firewall command failed") from error
    if result.returncode not in allowed:
        raise GuardError("firewall command failed")
    return result


def command(binary, *args, allowed=(0,)):
    return execute([binary, "--wait", "5", "--table", "filter", *args], allowed=allowed)


def tagged(rule, purpose):
    return [*rule, "-m", "comment", "--comment", MARKER + purpose]


def rules(mask, family):
    return [
        tagged([
            "-m", "hashlimit", "--hashlimit-above", "40/second", "--hashlimit-burst", "80",
            "--hashlimit-mode", "srcip", "--hashlimit-srcmask", "0",
            "--hashlimit-name", "llm_global" + family,
            "--hashlimit-htable-size", "16", "--hashlimit-htable-max", "16",
            "--hashlimit-htable-expire", "60000", "-j", "DROP",
        ], "global-rate"),
        tagged([
            "-m", "hashlimit", "--hashlimit-above", "8/second", "--hashlimit-burst", "16",
            "--hashlimit-mode", "srcip", "--hashlimit-srcmask", str(mask),
            "--hashlimit-name", "llm_ip" + family,
            "--hashlimit-htable-size", "1024", "--hashlimit-htable-max", "4096",
            "--hashlimit-htable-expire", "60000", "-j", "DROP",
        ], "ip-rate"),
        tagged([
            "-m", "connlimit", "--connlimit-above", "256", "--connlimit-mask", "0",
            "--connlimit-saddr", "-j", "DROP",
        ], "global-connections"),
        tagged([
            "-m", "connlimit", "--connlimit-above", "32", "--connlimit-mask", str(mask),
            "--connlimit-saddr", "-j", "DROP",
        ], "ip-connections"),
        tagged(["-j", "RETURN"], "continue-host-firewall"),
    ]


def option(tokens, name):
    try:
        return tokens[tokens.index(name) + 1]
    except (ValueError, IndexError):
        return None


def inspect(binary):
    current = command(binary, "-S", CHAIN, allowed=(0, 1))
    chain_rules = []
    for line in current.stdout.splitlines():
        tokens = shlex.split(line)
        if tokens[:2] == ["-A", CHAIN]:
            if not (option(tokens, "--comment") or "").startswith(MARKER):
                raise GuardError("reserved chain contains unmanaged rules")
            chain_rules.append(tokens[2:])
        elif tokens != ["-N", CHAIN]:
            raise GuardError("unexpected reserved chain state")
    input_rules = []
    jumps = []
    for line in command(binary, "-S", "INPUT").stdout.splitlines():
        tokens = shlex.split(line)
        if tokens[:2] != ["-A", "INPUT"]:
            continue
        input_rules.append(tokens[2:])
        if option(tokens, "-j") == CHAIN or option(tokens, "-g") == CHAIN:
            # Refuse collisions or modified broad jumps instead of touching SSH.
            if (
                option(tokens, "--comment") != MARKER + "ingress"
                or option(tokens, "-p") != "tcp" or option(tokens, "--dports") != "80,443"
                or option(tokens, "--ctstate") != "NEW"
                or option(tokens, "--tcp-flags") != "FIN,SYN,RST,ACK"
                or "SYN" not in tokens or "!" in tokens
            ):
                raise GuardError("reserved ingress jump is not owned or safe")
            jumps.append(tokens[2:])
    return current.returncode == 0, chain_rules, input_rules, jumps


def plan(family, remove=False):
    binary, restore, mask, suffix = family
    exists, installed, input_rules, jumps = inspect(binary)
    if remove:
        if not exists and not jumps:
            return None
        lines = ["*filter"]
        lines.extend(shlex.join(["-D", "INPUT", *jump]) for jump in jumps)
        if exists:
            lines.extend([f"-F {CHAIN}", f"-X {CHAIN}"])
    else:
        expected = rules(mask, suffix)
        correct = (
            exists and len(installed) == len(expected) and len(jumps) == 1
            and bool(input_rules) and input_rules[0] == jumps[0]
            and [option(rule, "--comment") for rule in installed]
            == [option(rule, "--comment") for rule in expected]
            and command(binary, "-C", "INPUT", *INPUT_RULE, allowed=(0, 1)).returncode == 0
        )
        if correct:
            for rule in expected:
                if command(binary, "-C", CHAIN, *rule, allowed=(0, 1)).returncode != 0:
                    correct = False
                    break
        if correct:
            return None
        # --noflush preserves all undeclared chains. Declaring only our chain
        # rebuilds it transactionally; the old rules remain until COMMIT.
        lines = ["*filter", f":{CHAIN} - [0:0]"]
        lines.extend(shlex.join(["-A", CHAIN, *rule]) for rule in expected)
        lines.extend(shlex.join(["-D", "INPUT", *jump]) for jump in jumps)
        lines.append(shlex.join(["-I", "INPUT", "1", *INPUT_RULE]))
    lines.extend(["COMMIT", ""])
    return restore, "\n".join(lines)


def apply(remove=False):
    plans = [change for family in FAMILIES if (change := plan(family, remove)) is not None]
    # Preflight both address families before modifying either. Each family is a
    # separate kernel transaction; an error never intentionally removes an old
    # guard. A concurrent external firewall edit may still require a rerun.
    for binary, payload in plans:
        execute([binary, "--wait", "5", "--noflush", "--test"], payload)
    for binary, payload in plans:
        execute([binary, "--wait", "5", "--noflush"], payload)
    return bool(plans)


def trusted_source():
    if os.geteuid() != 0:
        raise GuardError("root is required")
    source = Path(__file__).absolute()
    for path in (source, *source.parents):
        info = path.lstat()
        expected_type = stat.S_ISREG if path == source else stat.S_ISDIR
        if not expected_type(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise GuardError("guard source must be root managed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--install", action="store_true")
    action.add_argument("--remove", action="store_true")
    args = parser.parse_args()
    try:
        trusted_source()
        descriptor = os.open(
            "/run/llm-connection-guard.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600,
        )
        with os.fdopen(descriptor, "w") as lock:
            info = os.fstat(lock.fileno())
            if (
                not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise GuardError("unsafe guard lock")
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            changed = apply(remove=args.remove)
        print("connection_guard_changed" if changed else "connection_guard_unchanged")
        return 0
    except (GuardError, OSError, ValueError):
        print("connection_guard_failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
