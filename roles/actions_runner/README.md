# Remote deployment runner

This role prepares an Ubuntu 24.04 ARM64 host and a dedicated `llm-runner`
account. Registration and service activation are separate steps.

## Bootstrap

Use the controller environment and protected inventory from the main README:

```bash
ansible-playbook -i "$WORK_DIR/inventory.yml" playbooks/bootstrap-runner.yml \
  --extra-vars "actions_runner_bcrypt_source_file=$WORK_DIR/client/password.bcrypt"
mkdir -m 700 "$WORK_DIR/runner-registration"
python scripts/create_runner_registration_token.py \
  --repository OWNER/REPOSITORY \
  --output "$WORK_DIR/runner-registration/registration-token.json"
```

The token helper uses an authenticated GitHub CLI and writes the token directly
to a protected file. Transfer that file securely to the runner's
`protected/registration-token.json`, owned by `llm-runner` with mode 0600.
Copy the deployment inventory to `protected/inventory.yml` with the same ownership
and permissions, preserving model, monitoring, and API settings.

For deployment onto the runner host, use `ansible_host: 127.0.0.1`, root SSH,
`ansible_ssh_use_tty: false`, and the key/host-key paths below. Require
`StrictHostKeyChecking=yes`, `IdentitiesOnly=yes`, and `RequestTTY=no` in
`ansible_ssh_common_args`; add `127.0.0.1/32` to the administrator CIDRs.
Point `public_api_password_hash_file` at `protected/password.bcrypt`.

On the remote host, register and start the runner:

```bash
sudo runuser -u llm-runner -- python3 /tmp/llm-actions-runner/configure_actions_runner.py \
  --repository OWNER/REPOSITORY --name llm-deploy
sudo systemctl enable --now llm-actions-runner.service
```

The helper removes the remote registration token after success. Remove the
controller's temporary token file too. Set `LLM_INVENTORY_FILE` in the GitHub
`gpu-production` environment to `/tmp/llm-actions-runner/protected/inventory.yml`,
then dispatch **Deploy to an existing LLM server** on `main`.

## Storage and permissions

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
