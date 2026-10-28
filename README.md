# GPU LLM server

Ansible deploys Ollama on Ubuntu with NVIDIA drivers, Docker, GPU runtime,
automatic startup, inference checks, and recovery. Optional public HTTPS access
uses the server's IP address; no DNS is required. VictoriaMetrics and Grafana
provide monitoring. Terraform and GitHub Actions are included.

```text
HTTPS client → Caddy authentication → request guard → Ollama → NVIDIA GPU
                  └─ /grafana/ → Grafana → VictoriaMetrics → collectors
```

Ollama and monitoring listeners bind to loopback. Model data persists in
`/var/lib/ollama`; monitoring data persists in `/var/lib/llm-monitoring`.

## Requirements

- Ubuntu 24.04, x86_64, a compatible NVIDIA GPU, and root or passwordless sudo.
- SSH key access with a verified server host key.
- Python 3.12+ on the Ansible controller; outbound access to package and model registries.
- Capacity for the selected model, context, and concurrency. The default is
  Qwen3 4B Q4 with a 4,096-token context and one concurrent request.

Secure Boot may require signed driver modules and MOK enrollment through the
provider console. A host CUDA Toolkit/NVCC installation is unnecessary for this
containerized deployment.

## Setup

Create a private working directory outside the repository:

```bash
WORK_DIR="$(mktemp -d /tmp/llm.XXXXXX)"
WORK_DIR="$(cd "$WORK_DIR" && pwd -P)"
export ANSIBLE_HOME="$WORK_DIR/ansible"
export ANSIBLE_LOCAL_TEMP="$WORK_DIR/ansible/tmp"
export XDG_CACHE_HOME="$WORK_DIR/cache"
export TMPDIR="$WORK_DIR"
python3 -m venv "$WORK_DIR/venv"
. "$WORK_DIR/venv/bin/activate"
python -m pip install -r requirements-dev.txt
cp inventory/example.yml "$WORK_DIR/inventory.yml"
chmod 600 "$WORK_DIR/inventory.yml"
```

Edit the inventory with the target address, SSH account, and administrator CIDRs.
Use an SSH agent or protected key reference; keep credentials outside Git.
The provider firewall must permit the controller's SSH source address.

```bash
ansible-playbook -i "$WORK_DIR/inventory.yml" playbooks/deploy.yml --syntax-check
ansible-playbook -i "$WORK_DIR/inventory.yml" playbooks/deploy.yml \
  --extra-vars gpu_host_allow_reboot=true
```

Reboot permission is needed when installing or updating the driver. Omit it on
runs that do not require a reboot. Driver installation, runtime configuration,
model digest verification, and inference acceptance are performed by the playbook.
A failed acceptance check stops the inference service and public admission.

## Public API without DNS

Add to the inventory:

```yaml
public_api_enabled: true
public_api_address: YOUR_PUBLIC_IPV4
public_api_password_hash_file: /absolute/protected/path/client/password.bcrypt
monitoring_enabled: true
```

Generate credentials into a new protected directory, replacing `YOUR_PUBLIC_IPV4`:

```bash
python scripts/create_api_credentials.py \
  --directory "$WORK_DIR/client" --address YOUR_PUBLIC_IPV4
```

Set `public_api_password_hash_file` to the generated hash file's absolute path,
then deploy. The helper writes owner-only files without printing the password.
Caddy stores only the bcrypt hash. TCP 80/443 must be reachable for Let's Encrypt
IP-certificate issuance and automatic renewal.

Example authenticated request to `https://YOUR_PUBLIC_IPV4/api/chat`:

```bash
curl --disable --config "$WORK_DIR/client/client.curl" \
  --header 'Content-Type: application/json' \
  --data '{"model":"qwen3:4b-q4_K_M","messages":[{"role":"user","content":"Explain GPU inference in two sentences."}],"stream":false,"think":false,"options":{"num_predict":128}}'
```

The client configuration supplies the public HTTPS address and authentication.
The response contains `message.content` and `done`. Native Ollama and
OpenAI-compatible completion routes are supported; model administration is private.

### API limits

| Control | Default |
|---|---|
| Generation | 6/minute, burst 2, one active request |
| Input | 32 KiB body, 8 KiB text, 32 messages |
| Output | 256 tokens maximum |
| Inference deadline | 120 seconds |
| API admission, per IP / global | 300 / 600 requests per minute |
| Grafana admission, per IP / global | 1,800 / 3,600 requests per minute |
| Unverified password attempts, per IP / global | 5 / 30 per minute |

Admission runs before password hashing; Grafana and API route budgets are
separate. Excess generation receives HTTP 429 with `Retry-After`. A connection
guard also limits new and simultaneous public TCP connections. These controls
bound application work; upstream volumetric protection requires the provider or
an external filtering service. Clients sharing one account share its quota.

## GPU configuration and acceptance

GPU mode is the default. CPU mode must be explicitly selected and never acts as
an automatic fallback. GPU placement, VRAM headroom, and recovery policies are
configured in [Ollama defaults](roles/ollama/defaults/main.yml); driver and runtime
settings are in [GPU host defaults](roles/gpu_host/defaults/main.yml).

| Setting | Default |
|---|---|
| `ollama_gpu_offload_policy` | `full`; `partial` requires explicit opt-in |
| `ollama_gpu_device_uuids` | `[]` exposes all GPUs; set full UUIDs to select devices |
| `ollama_gpu_memory_headroom_mib` | 512 MiB reserved and checked per selected GPU |
| `ollama_acceptance_min_context_fraction` | 0.75 minimum measured prompt coverage |
| `ollama_acceptance_prompt` | Generated capacity document; override for a representative workload |

Deployment acceptance exercises the configured model and context, verifies GPU
placement and sampled VRAM headroom at the configured concurrency, and fails if
the selected policy is not satisfied. GPU injection uses the NVIDIA runtime in
JIT-CDI mode; CPU containers omit that runtime entirely.
Re-run acceptance after changing model size, context, concurrency, or GPU assignment.
Container `mem_limit` bounds host RAM, not GPU VRAM.

The watchdog checks API availability, container GPU access, and loaded-model
placement. Idle model unloading is normal. Repeated failures trigger bounded
recovery with a cooldown; a missing model does not cause a restart loop.

**Hardware validation:** the running demo is CPU-only. Automated tests cover GPU
policy and failure handling, but real GPU inference, reboot recovery, and sustained
load still require an NVIDIA host.

## Monitoring

Enable `monitoring_enabled` with public HTTPS, then open
`https://YOUR_PUBLIC_IPV4/grafana/` using the API credentials. Grafana queries
VictoriaMetrics; there is no Prometheus server.

- **Host:** CPU, memory, disk, network, and collector availability.
- **Inference:** model health, synthetic inference latency, token rate, and GPU placement.
- **GPU:** utilization, VRAM, temperature, power, and supported hardware diagnostics.
- **Gateway:** traffic, admission rejections, and active inference.
- **Alerts:** provisioned Grafana rule states.

Inference metrics describe periodic synthetic probes, not every user request.
GPU metrics are disabled in CPU mode; unsupported hardware counters are reported
as unavailable rather than fabricated zeroes. Alert notifications are muted until
an external contact point is configured. Independent monitoring is needed to detect
a complete host outage.

VictoriaMetrics retains three days by default. Grafana has a 1 GiB container limit
and a 768 MiB Go runtime target. Size monitoring and Ollama together, leaving memory
for the OS and deployment runner.

```bash
sudo systemctl status ollama.service ollama-watchdog.timer \
  monitoring.service llm-exporter.service
curl --fail --silent http://127.0.0.1:8428/health
```

## CPU demo

Ubuntu 24.04 x86_64 or ARM64 can run a CPU-only functional demo:

```yaml
inference_accelerator: cpu
ollama_memory_limit: 5g
ollama_cpu_limit: "4"
ollama_context_length: 2048
ollama_parallel_requests: 1
```

These settings target a 4-vCPU, 8-GB VM. They exercise the API and monitoring;
they do not validate NVIDIA hardware or GPU throughput.

## GitHub Actions

- **Validate:** Ansible syntax/lint, Python and Go tests, Compose rendering,
  gateway integration checks, workflow lint, Terraform validation, and secret scans.
  Runs on GitHub-hosted runners without infrastructure credentials.
- **Deploy:** manually dispatched on `main`, using the remote runner labelled
  `self-hosted`, `linux`, `ARM64`, `llm-deploy` and the `gpu-production` environment.
  Runs Ansible and records the deployed commit and workflow URL.

Set environment variable `LLM_INVENTORY_FILE` to a runner-owned, mode-0600 static
inventory outside the checkout. Only trusted maintainers should dispatch deployment;
PR jobs must remain on GitHub-hosted runners. See the
[remote runner setup](roles/actions_runner/README.md) for bootstrap and registration.
For a GPU target that may reboot, run the deployment controller on a separate host.

## Terraform

- [Dedicated GPU server](terraform/README.md): manages the Robot firewall and
  inventory for an existing GEX server; does not order hardware.
- [CPU Cloud VM](terraform/cloud/README.md): defines the VM, public IPv4, SSH public
  key, and Cloud firewall.

CI runs `fmt`, `init -backend=false`, and `validate`; it never runs `plan` or
`apply`. Robot and Cloud use separate credentials. Inject credentials through a
secret manager, and protect Terraform state outside Git. Import existing resources
before managing them with Terraform.

## Validation

```bash
ansible-lint
python -m pytest -q
terraform -chdir=terraform fmt -check -recursive
terraform -chdir=terraform init -backend=false -input=false -lockfile=readonly
terraform -chdir=terraform validate
terraform -chdir=terraform/cloud init -backend=false -input=false -lockfile=readonly
terraform -chdir=terraform/cloud validate
```

Gateway integration tests require Caddy 2.11.7 (`CADDY_BINARY` can select the binary).
Alert integration tests require VictoriaMetrics 1.155.0 (`VICTORIAMETRICS_BINARY`).
Compose tests require the Docker Compose v2 CLI. CI installs and verifies the
test binaries. Role defaults pin the model, container images, and runtime versions.

## References

- [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
- [Ollama GPU support](https://docs.ollama.com/gpu)
- [Ollama API](https://docs.ollama.com/api)
- [VictoriaMetrics](https://docs.victoriametrics.com/victoriametrics/single-server-victoriametrics/)
