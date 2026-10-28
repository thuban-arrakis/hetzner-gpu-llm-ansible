output "server_id" {
  description = "Cloud server ID, useful for inspecting the authenticated provider console."
  value       = hcloud_server.inference.id
}

output "server_ipv4" {
  description = "Public IPv4 address of the CPU demo."
  value       = hcloud_primary_ip.inference.ip_address
}

output "public_api_url" {
  description = "HTTPS URL after Ansible installs Caddy and issues a trusted IP certificate."
  value       = "https://${hcloud_primary_ip.inference.ip_address}/api/chat"
}

output "ansible_inventory_yaml" {
  description = "Non-secret CPU-mode inventory; add a protected bcrypt file reference locally before deployment."
  value = yamlencode({
    gpu_servers = {
      hosts = {
        cpu01 = {
          ansible_host                = hcloud_primary_ip.inference.ip_address
          ansible_user                = "root"
          ansible_port                = 22
          inference_accelerator       = "cpu"
          ollama_memory_limit         = "6g"
          ollama_cpu_limit            = "4"
          ollama_context_length       = 2048
          ollama_parallel_requests    = 1
          server_security_admin_cidrs = var.admin_ipv4_cidrs
          public_api_enabled          = true
          public_api_address          = hcloud_primary_ip.inference.ip_address
        }
      }
    }
  })
}
