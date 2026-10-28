# Private GPU LLM on Hetzner

Ansible configures an existing Ubuntu NVIDIA GPU server and runs a local Ollama
API in Docker. Terraform manages the dedicated server's Robot network firewall.
GitHub Actions validates the code; a separate manual workflow can run Ansible
from a protected management runner.

**Delivery scope:** code and validation only. No Terraform plan/apply, server
purchase, remote Ansible deployment, or real GPU inference has been performed as
part of this submission. Static checks cannot prove hardware compatibility;
the deployment playbook includes the hardware and inference acceptance checks.

## Architecture and security boundary

```mermaid
flowchart LR
    C[Authorized operator] -->|SSH key + pinned host key| F[Robot and host firewalls]
    F --> H[Ubuntu 24.04 GPU server]
    H -->|SSH local forwarding| A[127.0.0.1:11434]
    A --> O[Unprivileged Ollama container]
    O --> G[NVIDIA GPU]
    O --> V[Persistent model storage]
```

- The API is **private by default**. Ollama has no built-in API authentication;
  the playbook refuses a public bind address. Use an SSH tunnel for encrypted,
  authenticated access. Ports 80, 443, and 11434 are not opened in Robot.
- The container is non-root, has a read-only root filesystem, drops Linux
  capabilities, prohibits new privileges, and has memory/CPU/process/log limits.
- Model data persists in `/var/lib/ollama`; deployment files are root-managed in
  `/opt/ollama`. Cloud features are disabled with `OLLAMA_NO_CLOUD=1`.
- SSH password and keyboard-interactive authentication are disabled; existing
  SSH keys and host-key checking remain required. SSH forwarding stays enabled.
- Docker is root-equivalent administration. No user is added to its group.
  Docker Engine 28+ is required because older releases had a localhost-published
  port exposure issue on the same L2 network. UFW alone is not the security
  boundary for Docker-published ports.
- This assumes a dedicated, clean inference host. Existing unrelated UFW rules
  are retained; review and remove any pre-existing broad ingress rules before
  treating the host as SSH-only. Never reuse this configuration blindly on a
  multi-purpose production server.

## Prerequisites

**Target:** Ubuntu 24.04 LTS, x86_64, systemd, a physical/passed-through compatible
NVIDIA GPU, SSH key access, and root or passwordless sudo. Budget at least 16 GB
system RAM and 30 GB free disk for the example; a GPU with 8 GB+ VRAM is a
practical starting point for this small quantized model and 4096-token context.
Actual capacity depends on model, context, and concurrency. A Hetzner GEX45
(24 GB GPU VRAM) comfortably exceeds these example requirements.

**Controller:** Python 3.12+, OpenSSH, and Terraform 1.15.x for the Terraform
directory. The target's Python is supplied by Ubuntu. Internet egress is needed
for Ubuntu, Docker, NVIDIA, Docker Hub, and the Ollama model registry.

Hetzner's GEX GPU machines are **dedicated Robot servers**, not GPU-capable
`hcloud_server` instances. Order/provision the machine, install Ubuntu, and
enroll your SSH key separately. This repository intentionally targets an
existing GEX server and does not automate a hardware purchase.

Verify the server's SSH host-key fingerprint through a trusted channel such as
the provider console before adding it to `known_hosts`. Do not disable host-key
verification or trust an unauthenticated `ssh-keyscan` result by itself.

## Configure and deploy with Ansible

Create a controller environment and a private local inventory:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
cp inventory/example.yml inventory/local.yml
chmod 600 inventory/local.yml
```

Edit `inventory/local.yml`:

- Replace the documentation IP with your server address.
- Set the existing SSH account and port. Use `ssh-agent` or your local SSH
  configuration for the private-key reference; never put keys/passwords here.
- Replace `server_security_admin_cidrs` with your controller's public IPv4
  egress address or approved VPN range. The host role checks the current SSH
  source, when available, to reduce lockout risk. Robot must allow the same
  source; when using a jump host, this is the jump host's egress address.

Run syntax validation first. For a fresh driver install, explicitly allow the
maintenance reboot:

```bash
ansible-playbook -i inventory/local.yml playbooks/deploy.yml --syntax-check
ansible-playbook -i inventory/local.yml playbooks/deploy.yml \
  --extra-vars gpu_host_allow_reboot=true
```

Subsequent runs can omit the reboot opt-in when no driver update is needed:

```bash
ansible-playbook -i inventory/local.yml playbooks/deploy.yml
```

The playbook rejects CPU-only hosts, installs or verifies the NVIDIA driver,
installs Docker and NVIDIA Container Toolkit, configures the runtime, applies
host security, starts Ollama, installs the model, and verifies actual inference
with nonzero reported GPU residency. An existing working driver at or above
the configured baseline is preserved. A missing/old driver with Secure Boot
enabled fails safely; prepare properly signed modules/MOK enrollment through
your approved procedure rather than disabling Secure Boot automatically.

Model download can take several minutes. Repeated runs do not re-download an
installed model or recreate an unchanged service. A complete `--check` run on a
pristine GPU host cannot simulate package installation, reboots, or CUDA; use
the explicit syntax/lint checks and the post-deployment acceptance checks.

## Example API request

In one terminal, keep an SSH tunnel open (replace the example hostname):

```bash
ssh -N -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=yes \
  -L 127.0.0.1:11434:127.0.0.1:11434 root@gpu.example.org
```

In another terminal:

```bash
curl --fail --silent --show-error http://127.0.0.1:11434/api/chat \
  --header 'Content-Type: application/json' \
  --data '{
    "model": "qwen3:4b-q4_K_M",
    "messages": [{"role": "user", "content": "Explain GPU inference in two sentences."}],
    "stream": false,
    "think": false,
    "options": {"num_ctx": 4096, "num_predict": 128}
  }'
```

The response contains `message.content` and `done`. This request carries no API
key because SSH already authenticates and encrypts the connection. Other local
users on the GPU host can also reach localhost: use a dedicated trusted host.

## Startup, health, and recovery

`ollama.service` owns the Compose lifecycle and starts at boot. Docker's
`unless-stopped` policy handles process exits. The container healthcheck runs
`ollama list`; `/api/tags` establishes API/model availability.

Docker does not restart a container simply because it is unhealthy. The
`ollama-watchdog.timer` therefore checks the container, the model catalogue,
and container-side `nvidia-smi` every 60 seconds. Recovery requires repeated
failure, has a five-minute cooldown, and is capped at three attempts per hour.
A shared lock prevents a recovery from interrupting model downloads or service
maintenance. A missing model raises a fixed journal event instead of causing
an endless restart loop. An explicit administrative service stop is respected.

The deployment acceptance check also generates real tokens and checks
`/api/ps` for `size_vram > 0`. This proves GPU participation, not that every layer
is offloaded. On failed acceptance, the service is disabled/stopped instead of
leaving CPU-only inference enabled. The periodic watchdog is a liveness/device
check; it does not continuously generate tokens or prove inference quality.

Useful commands on the GPU host:

```bash
sudo systemctl status ollama.service ollama-watchdog.timer
sudo journalctl -u ollama.service -u ollama-watchdog.service --since '30 minutes ago'
sudo docker compose --project-name gpu-llm --file /opt/ollama/compose.yml ps
nvidia-smi
curl --fail --silent http://127.0.0.1:11434/api/tags
sudo systemctl stop ollama.service
```

Do not enable verbose request/prompt logging in a shared log sink. The watchdog
emits only fixed event names, not model responses or container logs.

## Terraform: dedicated-server network policy

See [terraform/README.md](terraform/README.md) for variables, provider scope,
authentication boundaries, and rules. The configuration:

1. Looks up an existing GEX by Robot server number.
2. Enables the Robot firewall with IPv4 SSH access from one to three admin CIDRs.
3. Allows essential replies to outbound traffic, with IPv6 filtering enabled.
4. Exports a static Ansible inventory as YAML.

No Terraform plan or apply is part of validation or any workflow. The pinned
`futo-org/hetzner` provider is community-maintained; its source and lockfile are
included for review. The firewall has `prevent_destroy` because provider
deletion would disable its protection.

Only after a separately authorized infrastructure run has populated state can
the generated inventory be exported:

```bash
umask 077
terraform -chdir=terraform output -raw ansible_inventory_yaml > inventory/local.auto.yml
```

Alternatively use the manual Ansible inventory without Terraform. Local
inventories, real tfvars, state, plans, credential files, and model data are
excluded from Git. `.terraform.lock.hcl` is deliberately committed.

## Credentials and the existing Keychain API

This repository contains no credentials and needs none for static validation.
The inspected `hetzner-api` Keychain profile targets the **Hetzner Cloud API**;
it cannot authenticate the **Robot API** used by dedicated GEX servers. Its
Cloud token must not be reused as a Robot password or sent to a different API.

A future Terraform run requires an approved secret-store integration to inject
`HETZNER_ROBOT_USERNAME` and `HETZNER_ROBOT_PASSWORD` into the provider process.
The checked-in provider block intentionally contains no credentials. Do not
paste credentials into commands/chat, commit them in tfvars, export Terraform
debug logs, or modify the Keychain routing policy as a shortcut. No Robot
credential was requested or created for this code-only submission.

Ansible uses the controller's SSH agent and verified `known_hosts`; private keys
stay outside the repository. Use protected, encrypted remote Terraform state
with locking for team operation, and restrict state access even though the
current resources do not intentionally store passwords in state.

## GitHub Actions

- **Validate:** GitHub-hosted Ubuntu runner; Ansible syntax and production lint,
  Python recovery/security tests, rendered Compose schema check, Terraform
  formatting and validation, and checksum-pinned Gitleaks scans. It has read-only
  repository permissions and no infrastructure credentials. No plan/apply.
- **Deploy:** manual `workflow_dispatch` on `main` only, never a PR trigger. It
  requires a separate trusted management runner labelled `llm-deploy` and the
  `gpu-production` environment. Configure environment approval where available,
  branch protection, and restrict workflow dispatch to trusted maintainers.
  Do not install this runner on the inference server or allow untrusted PR jobs
  on it. Put a runner-owned, non-executable mode-0600 static YAML inventory
  outside the checkout, set environment variable configuration
  `LLM_INVENTORY_FILE` as a GitHub environment **variable**, and provide its SSH
  agent/known_hosts out of band. No SSH private key is interpolated into workflow
  scripts. The runner needs Python 3.12+ and OpenSSH.

The manual workflow performs Ansible deployment when deliberately invoked; it
does not provision or purchase servers. Private-repository Actions minutes may
be billable according to the account's GitHub plan.

## Local validation

```bash
. .venv/bin/activate
ansible-playbook -i inventory/example.yml playbooks/deploy.yml --syntax-check
ansible-lint
python -m pytest -q
terraform -chdir=terraform fmt -check -recursive
terraform -chdir=terraform init -backend=false -input=false -lockfile=readonly
terraform -chdir=terraform validate
```

The Compose schema test needs the Docker Compose v2 CLI, not a running GPU or
Docker daemon. It skips when the local plugin is unavailable; CI explicitly
requires the plugin. Python tests exercise restart budgets/cooldowns, transient
failure behavior, missing-model handling, and protected deployment inventory
validation. They are not a substitute for a real GPU deployment or an
idempotence run on a representative host.

## Configuration and improvements

Role defaults are documented in `roles/*/defaults/main.yml`. The example pins
Ollama 0.40.12 by OCI digest, NVIDIA Container Toolkit 1.20.1-1, and
`qwen3:4b-q4_K_M` by model manifest digest. Change the model name and approved
digest together after reviewing a new artifact. A digest mismatch fails
deployment. Never set a mutable `latest` image for unattended inference.

For a larger deployment:

- Add NVIDIA DCGM Exporter, Prometheus, and Grafana on a private monitoring
  network. Alert on GPU temperature, ECC/Xid errors, memory pressure, failed
  inference, queue depth, and watchdog recovery limits. Keep metrics private.
- Put a separately authenticated TLS gateway in front of the API for application
  clients. Use mTLS or OIDC, per-client quotas, request/context limits, and rate
  limiting. Expose inference routes only; keep model pull/delete administration
  private. Do not simply publish Ollama's raw port.
- Use vLLM when continuous batching and multi-GPU throughput justify its greater
  model/runtime complexity. Benchmark time-to-first-token and tokens/second on
  representative prompts before changing serving engines.
- Use multiple GPU nodes with readiness-aware routing and queue backpressure.
  One server is not HA. Preserve model artifacts/digests and deployment
  configuration so failed hardware can be replaced reproducibly.
- Centralize secret management, enforce signed/reviewed configuration changes,
  and use a protected state backend. Keep prompts out of default logs.

## Primary references

- [Hetzner GPU server documentation](https://docs.hetzner.com/robot/dedicated-server/server-lines/gpu-server/)
- [Ubuntu NVIDIA driver installation](https://documentation.ubuntu.com/server/how-to/graphics/install-nvidia-drivers/)
- [Docker Engine on Ubuntu](https://docs.docker.com/engine/install/ubuntu/)
- [Docker port publishing security](https://docs.docker.com/engine/network/port-publishing/)
- [NVIDIA Container Toolkit installation](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
- [Ollama Docker deployment](https://docs.ollama.com/docker)
- [Ollama API](https://docs.ollama.com/api)
- [Ollama model catalogue](https://registry.ollama.com/library/qwen3/tags)
- [Pinned Robot provider](https://github.com/futo-org/terraform-provider-hetzner/tree/v1.1.0)
