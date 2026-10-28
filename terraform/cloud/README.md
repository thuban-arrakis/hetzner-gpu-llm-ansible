# Hetzner Cloud CPU demo

This is a separate Terraform root for an ordinary **CPU-only Cloud VM**. It does
not order a dedicated GEX server and does not satisfy the assignment's NVIDIA
GPU requirement. The default CAX21 has 4 shared ARM vCPUs, 8 GB RAM and 80 GB NVMe.
Check current price and capacity before provisioning; alternatives are explicit
variables, never automatic fallbacks. Hetzner lists Cloud servers as having
[no setup fee](https://docs.hetzner.com/general/billing-and-account-management/billing-at-hetzner/payment-overview/).
Usage and IPv4 charges still apply until the resources are deleted.

The configuration creates one VM, a managed IPv4 address, an uploaded public SSH
key, and a stateful Cloud firewall attached at creation. SSH is restricted to the
configured administrator networks. TCP 80/443 serve ACME and authenticated HTTPS;
the raw Ollama port is not exposed. IPv6 is disabled for this IPv4-only demo.
No DNS zone, record, or registration is created. Ansible configures the model and
Caddy's trusted IP certificate afterward.

## Configuration and validation

Use an existing Ed25519 SSH **public** key file, then copy
`terraform.tfvars.example` to an ignored local tfvars file and replace its values.
The provider reads `HCLOUD_TOKEN` from an approved secret-store process
integration. Do not paste tokens into shell commands or put credentials into
Terraform variables, user data, files, or the repository. Robot credentials are
not valid for this Cloud API.

Offline configuration checks need no provider credentials:

```bash
terraform -chdir=terraform/cloud fmt -check
terraform -chdir=terraform/cloud init -backend=false -input=false -lockfile=readonly
terraform -chdir=terraform/cloud validate
```

No plan or apply is executed by CI. A separate authorized infrastructure run is
needed to create resources. If the demo was created through the API, reconcile
it into Terraform state using resource imports before managing it here; blindly
applying this root would create a second VM. Use protected state storage.

After provisioning, export the non-secret inventory into a private file:

```bash
umask 077
terraform -chdir=terraform/cloud output -raw ansible_inventory_yaml > inventory/local.cpu.yml
```

Add the external protected bcrypt file reference according to the main README.
The exported `inference_accelerator: cpu` explicitly selects `cpu_host` and omits
NVIDIA driver/runtime installation and GPU device reservations. CPU inference
is slower than GPU inference and is intended for short functional tests. The
example limits Ollama to 4 CPUs, 6 GB RAM, one request at a time and a
2048-token context, leaving capacity for Ubuntu and the HTTPS gateway.

## Trust the SSH host before connecting

The Cloud server creation response does not supply an authenticated SSH host-key
fingerprint. Obtain the server's Ed25519 fingerprint through the authenticated
Hetzner console and compare it before populating `known_hosts`. Cloud-init prints
public host-key fingerprints to the console and places the Ed25519 fingerprint
in the login banner, so the authenticated VNC view can display it without a
root-password reset or guest login. Alternatively run
`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` in the provider console.
An unauthenticated `ssh-keyscan` by itself does not establish trust. Do not
disable strict host-key checking, embed a private host key in user data, or place
a Cloud token in the guest just to report its fingerprint.

See Hetzner's [SSH-key guide](https://community.hetzner.com/tutorials/add-ssh-key-to-your-hetzner-cloud/)
for the trusted console verification path.

## Cleanup

Stop charges by deleting the demo VM **and its managed primary IPv4** when the
test is complete. The primary IP intentionally has `auto_delete = false` to keep
Terraform's resource lifecycle explicit; deleting only the VM leaves the IPv4
billable. Delete the demo firewall and uploaded public key when no longer needed.
Deletion is separate from stopping or powering off the VM. No cleanup runs
automatically, and the deployment does not buy backups or extra volumes.
