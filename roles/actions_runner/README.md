This role prepares the existing Ubuntu 24.04 ARM64 server; it never registers,
starts or enables a GitHub runner. Run `playbooks/bootstrap-runner.yml` against
one remote host in `gpu_servers`. Set `actions_runner_bcrypt_source_file` to an
existing owner-only controller file outside the repository, inside a protected
directory. The existing `protected_bcrypt` lookup validates its metadata and
contents. Only the bcrypt hash is copied; no API password or GitHub token is
accepted by the playbook.

All runner storage is below `/tmp/llm-actions-runner`:

| Path | Purpose |
| --- | --- |
| `runner/` | Checksum-pinned official Actions runner 2.338.2 |
| `home/`, `work/`, `tmp/`, `cache/` | Private runtime storage |
| `protected/deploy_ed25519` | Server-generated SSH private key, mode 0600 |
| `protected/known_hosts` | Loopback host key pinned from the server's existing Ed25519 host public key |
| `protected/password.bcrypt` | Validated authentication hash, mode 0600 |

The `llm-runner` account has no supplementary groups, sudo grant or password
login. Its dedicated SSH key authorizes root commands only from `127.0.0.1`,
with OpenSSH `restrict` disabling forwarding, agent forwarding, user rc and
PTY allocation. Deployment inventory must therefore use loopback SSH with
`ansible_ssh_use_tty: false`, the dedicated private-key path, strict host-key
checking and the pinned `known_hosts` file. This deployment credential grants
root command execution on this server; only trusted private-repository jobs
may use the runner.

Bootstrap installs `/etc/systemd/system/llm-actions-runner.service` disabled
and stopped, plus the separate protected registration helper. The controller
must then provide protected registration data, register the runner with work
directory `/tmp/llm-actions-runner/work`, and explicitly start its service.
Re-running bootstrap stops and disables an existing runner while updating it.
Runner state is deliberately under `/tmp`; if the OS clears that directory,
bootstrap and protected registration must be repeated before jobs can run.
