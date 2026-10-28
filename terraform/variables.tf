variable "server_number" {
  description = "Robot server number of an existing, provisioned Hetzner GEX dedicated GPU server. This configuration does not order hardware or install its OS."
  type        = number

  validation {
    condition     = var.server_number > 0 && floor(var.server_number) == var.server_number
    error_message = "server_number must be a positive integer from Hetzner Robot."
  }
}

variable "admin_ipv4_cidrs" {
  description = "One to three IPv4 CIDRs allowed to SSH to port 22. Include the Ansible controller's public egress address. The Robot firewall has ten ingress rules in total."
  type        = list(string)

  validation {
    condition = (
      length(var.admin_ipv4_cidrs) >= 1 &&
      length(var.admin_ipv4_cidrs) <= 3 &&
      length(distinct(var.admin_ipv4_cidrs)) == length(var.admin_ipv4_cidrs) &&
      alltrue([
        for cidr in var.admin_ipv4_cidrs :
        can(cidrnetmask(cidr)) && can(regex("/([1-9]|[12][0-9]|3[0-2])$", cidr))
      ])
    )
    error_message = "Provide one to three distinct IPv4 CIDRs with prefixes /1 through /32; unrestricted /0 and IPv6 are not accepted."
  }
}

variable "ansible_user" {
  description = "Existing SSH account with root privileges or passwordless sudo. Hetzner's initial Ubuntu installation normally uses root."
  type        = string
  default     = "root"

  validation {
    condition     = can(regex("^[a-z_][a-z0-9_-]{0,31}$", var.ansible_user))
    error_message = "ansible_user must be a valid lowercase Linux account name."
  }
}
