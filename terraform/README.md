# Dedicated GPU infrastructure boundary

This configuration reads an **existing Hetzner GEX dedicated server**, manages
its Robot firewall, and produces Ansible inventory. Hardware ordering, billing,
Ubuntu 24.04 installation, and enrollment of an SSH public key happen separately.
It deliberately has no purchase, reset, rescue, or OS installation resources.
The `hcloud` provider manages Hetzner Cloud and cannot turn a CPU VM into a GEX.

The community [`futo-org/hetzner` provider](https://registry.terraform.io/providers/futo-org/hetzner/1.1.0/docs)
is pinned to 1.1.0. Its Robot API authentication is different from a Hetzner Cloud
API token. Future authenticated runs require `HETZNER_ROBOT_USERNAME` and
`HETZNER_ROBOT_PASSWORD` injected into the Terraform process by an approved
credential manager. Never put their values in `.tf`, `.tfvars`, shell history,
workflow YAML, or checked-in files. This submission requires no credentials and
does not perform `plan` or `apply`.

## Offline and public-download validation

From this directory:

```sh
terraform fmt -check -recursive
terraform init -backend=false -input=false
terraform validate
```

Initialization downloads the pinned public provider; validation makes no Robot
request. Commit `.terraform.lock.hcl` and keep `.terraform/` ignored. The CI
workflow runs these checks without infrastructure credentials. It does not run
`terraform plan`, `terraform apply`, or Ansible against a server.

## Future configuration

Copy `terraform.tfvars.example` to ignored `terraform.tfvars`, enter the actual
Robot server number, and replace the example admin CIDR with the controller's
public egress address. Up to three IPv4 CIDRs are supported in private mode;
public HTTPS mode supports exactly one because of Robot's ten-rule limit. There is no default
administrator network and `/0` is rejected. The selected product must start with
`GEX`; Ansible independently checks the physical NVIDIA GPU and its driver.

The default SSH account is `root`, matching the initial Hetzner installation.
If using another existing account, it must have passwordless sudo. SSH identity
files remain outside this repository; use the SSH agent and trusted `known_hosts`.

After a future, separately authorized infrastructure run, inventory can be
exported from the repository root without storing credentials:

```sh
umask 077
terraform -chdir=terraform output -raw ansible_inventory_yaml > inventory/local.auto.yml
```

Terraform state contains infrastructure metadata such as server IPs. Keep state
and saved plans out of Git and CI artifacts. Before operational use, configure
an access-controlled, encrypted remote state backend with locking and recovery.
The code-only submission uses no configured remote backend.

## Firewall behavior

The [Robot firewall](https://docs.hetzner.com/robot/dedicated-server/firewall/)
is stateless and has a ten-rule limit per direction. This configuration accepts
IPv4 SSH on port 22 only from the configured administrator CIDRs. It also permits
TCP ACK reply packets to ports 1024–65535 (including Docker NAT remapping), UDP DNS and NTP replies on IPv4
and IPv6, IPv4 ICMP, and outbound traffic. Hetzner infrastructure services are
allowlisted to retain rescue and provider networking support. IPv6 filtering is
explicitly enabled; Robot always permits ICMPv6.

By default there is no public API listener rule and no IPv6 SSH rule. Set
`public_api_enabled = true` to add separate IPv4 TCP 80 and 443 ingress rules for Caddy and
ACME certificate validation. This also enables the optional Ansible gateway in
the exported inventory and uses the server IPv4 as `public_api_address`; provide
the protected authentication-hash file reference as documented in the main
README. No DNS record is required. Port 80 is for ACME/HTTPS redirects, never
plaintext API requests. TLS private keys are created on the server by Caddy and
never pass through Terraform state.

Ollama always binds to `127.0.0.1:11434`; it is reached through the authenticated
gateway or SSH tunnel described in the main README. The host's stateful firewall is still required because a stateless
reply rule does not prove that a packet belongs to an established connection.
Review these rules if customizing connection tracking, source ports, or network services.

The resource replaces the server's **entire firewall configuration**. Review
existing rules and vSwitch dependencies before any future deployment. Import an
existing firewall before taking over its management. `prevent_destroy` is set
because provider deletion disables the firewall; intentionally removing or
replacing it requires a reviewed code change.
