output "server_ipv4" {
  description = "Public IPv4 of the existing dedicated GPU server."
  value       = data.hetzner_server.gpu.server_ip
}

output "server_product" {
  description = "Robot product name; the playbook separately verifies that an NVIDIA GPU is present and functional."
  value       = data.hetzner_server.gpu.product
}

output "ansible_inventory_yaml" {
  description = "After a separately authorized infrastructure run, write this output to inventory/local.auto.yml. It contains connection metadata, not secrets."
  value = yamlencode({
    gpu_servers = {
      hosts = {
        gpu01 = {
          ansible_host                = data.hetzner_server.gpu.server_ip
          ansible_user                = var.ansible_user
          ansible_port                = 22
          server_security_admin_cidrs = var.admin_ipv4_cidrs
          public_api_enabled          = var.public_api_enabled
          public_api_address          = data.hetzner_server.gpu.server_ip
        }
      }
    }
  })
}
