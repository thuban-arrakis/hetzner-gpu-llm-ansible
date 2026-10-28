# GEX is a dedicated Robot product, not a Hetzner Cloud hcloud_server type.
# Ordering, payment, OS installation, and SSH key enrollment are prerequisites.
data "hetzner_server" "gpu" {
  server_number = var.server_number

  lifecycle {
    postcondition {
      condition     = startswith(upper(self.product), "GEX")
      error_message = "The selected Robot server is not a GEX product. Select the intended dedicated GPU server."
    }
  }
}

locals {
  ssh_rules = [for index, cidr in var.admin_ipv4_cidrs : {
    name       = "SSH admin ${index + 1}"
    ip_version = "ipv4"
    protocol   = "tcp"
    src_ip     = cidr
    dst_port   = "22"
    action     = "accept"
  }]

  # Robot is stateless. These rules admit replies to server-initiated package,
  # image, model, DNS, and time-sync requests. The stateful host firewall still
  # rejects unsolicited traffic; these are not application listener rules.
  tcp_reply_rules = [for version in ["ipv4", "ipv6"] : {
    name       = "TCP replies ${version}"
    ip_version = version
    protocol   = "tcp"
    dst_port   = "1024-65535"
    tcp_flags  = "ack"
    action     = "accept"
  }]

  dns_reply_rules = [for version in ["ipv4", "ipv6"] : {
    name       = "DNS replies ${version}"
    ip_version = version
    protocol   = "udp"
    src_port   = "53"
    dst_port   = "1024-65535"
    action     = "accept"
  }]

  ntp_reply_rules = [for version in ["ipv4", "ipv6"] : {
    name       = "NTP replies ${version}"
    ip_version = version
    protocol   = "udp"
    src_port   = "123"
    dst_port   = "1024-65535"
    action     = "accept"
  }]

  public_api_rules = var.public_api_enabled ? [for port in ["80", "443"] : {
    name       = "Public gateway ${port}"
    ip_version = "ipv4"
    protocol   = "tcp"
    dst_port   = port
    action     = "accept"
  }] : []

  input_rules = concat(
    local.ssh_rules,
    local.public_api_rules,
    local.tcp_reply_rules,
    local.dns_reply_rules,
    local.ntp_reply_rules,
    [{
      name       = "ICMP ipv4"
      ip_version = "ipv4"
      protocol   = "icmp"
      action     = "accept"
    }]
  )
}

resource "hetzner_firewall" "gpu" {
  server_number = tostring(data.hetzner_server.gpu.server_number)
  status        = "active"
  allowlist_hos = true
  filter_ipv6   = true

  input = local.input_rules
  output = [{
    name   = "Allow outbound"
    action = "accept"
  }]

  lifecycle {
    # Provider deletion disables this firewall. Require an explicit code change
    # before removing protection or replacing the target server.
    prevent_destroy = true

    precondition {
      condition     = length(local.input_rules) <= 10
      error_message = "Hetzner Robot permits at most ten rules per direction. Public HTTPS requires exactly one administrator CIDR with this policy."
    }
  }
}
