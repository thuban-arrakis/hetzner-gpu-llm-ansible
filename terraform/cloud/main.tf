locals {
  labels = {
    application = "ollama-demo"
    accelerator = "cpu"
    managed_by  = "terraform"
  }
}

resource "hcloud_ssh_key" "operator" {
  name       = "${var.server_name}-operator"
  public_key = trimspace(file(var.ssh_public_key_file))
  labels     = local.labels
}

resource "hcloud_firewall" "inference" {
  name   = "${var.server_name}-ingress"
  labels = local.labels

  rule {
    description = "SSH from operator networks"
    direction   = "in"
    protocol    = "tcp"
    port        = "22"
    source_ips  = var.admin_ipv4_cidrs
  }

  rule {
    description = "ACME HTTP validation"
    direction   = "in"
    protocol    = "tcp"
    port        = "80"
    source_ips  = ["0.0.0.0/0"]
  }

  rule {
    description = "Authenticated HTTPS inference"
    direction   = "in"
    protocol    = "tcp"
    port        = "443"
    source_ips  = ["0.0.0.0/0"]
  }

  rule {
    description = "IPv4 diagnostics and path MTU"
    direction   = "in"
    protocol    = "icmp"
    source_ips  = ["0.0.0.0/0"]
  }
  # Stateful replies are automatic. Unlisted ingress (including 11434) is denied.
  # With no egress rules, package/model/ACME traffic can leave the server.
}

resource "hcloud_primary_ip" "inference" {
  name        = "${var.server_name}-ipv4"
  type        = "ipv4"
  location    = var.location
  auto_delete = false
  labels      = local.labels
}

resource "hcloud_server" "inference" {
  name         = var.server_name
  server_type  = var.server_type
  image        = "ubuntu-24.04"
  location     = var.location
  ssh_keys     = [hcloud_ssh_key.operator.id]
  firewall_ids = [hcloud_firewall.inference.id]
  labels       = local.labels
  backups      = false

  public_net {
    ipv4_enabled = true
    ipv4         = hcloud_primary_ip.inference.id
    ipv6_enabled = false
  }

  # Public configuration only. SSH host keys are generated inside the VM.
  # The provider injects the PUBLIC authorized key; no private keys or API
  # passwords ever belong in cloud-init or Terraform state.
  user_data = "#cloud-config\n${yamlencode({
    ssh_pwauth      = false
    disable_root    = false
    ssh_deletekeys  = true
    ssh_genkeytypes = ["ed25519"]
    ssh             = { emit_keys_to_console = true }
    package_update  = false
    package_upgrade = false
    write_files = [{
      path        = "/etc/ssh/sshd_config.d/00-cloud-demo.conf"
      owner       = "root:root"
      permissions = "0644"
      content     = "PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin prohibit-password\n"
    }]
    runcmd = [
      ["systemctl", "reload", "ssh"],
      ["sh", "-c", join("; ", [
        "printf '\\nLLM DEMO - TRUSTED SSH HOST FINGERPRINT\\n' > /etc/issue",
        "ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub >> /etc/issue",
        "printf '\\n' >> /etc/issue",
        "systemctl restart getty@tty1.service"
      ])]
    ]
    ssh_key_console_blacklist = []
    ssh_fp_console_blacklist  = []
  })}"
}
