variable "server_name" {
  description = "Unique name of the ordinary CPU-only Cloud demo."
  type        = string
  default     = "llm-cpu-demo"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,61}[a-z0-9]$", var.server_name))
    error_message = "Use a lowercase hostname of 2-63 letters, numbers, and internal hyphens."
  }
}

variable "server_type" {
  description = "Explicit CPU-only size. CAX21 has 4 shared ARM vCPUs and 8 GB RAM; verify current stock and price before provisioning."
  type        = string
  default     = "cax21"

  validation {
    condition     = contains(["cax21", "cax31", "cx43", "cx53", "cpx32", "ccx23"], var.server_type)
    error_message = "Select a reviewed ARM or x86 CPU demo size; do not silently substitute an unreviewed product."
  }
}

variable "location" {
  description = "European location with current availability for the chosen size."
  type        = string
  default     = "fsn1"

  validation {
    condition     = contains(["hel1", "fsn1", "nbg1"], var.location)
    error_message = "Use hel1, fsn1, or nbg1."
  }
}

variable "admin_ipv4_cidrs" {
  description = "Explicit administrator IPv4 CIDRs permitted to SSH, including the Ansible controller egress."
  type        = list(string)

  validation {
    condition = (
      length(var.admin_ipv4_cidrs) >= 1 && length(var.admin_ipv4_cidrs) <= 10 &&
      length(distinct(var.admin_ipv4_cidrs)) == length(var.admin_ipv4_cidrs) &&
      alltrue([for cidr in var.admin_ipv4_cidrs :
        can(cidrnetmask(cidr)) && can(regex("/([1-9]|[12][0-9]|3[0-2])$", cidr))
      ])
    )
    error_message = "Provide distinct IPv4 CIDRs with prefixes /1 through /32; unrestricted SSH is forbidden."
  }
}

variable "ssh_public_key_file" {
  description = "Absolute local path to an existing OpenSSH Ed25519 PUBLIC key. Never a private key."
  type        = string

  validation {
    condition = (
      startswith(var.ssh_public_key_file, "/") &&
      can(regex("^ssh-ed25519 [A-Za-z0-9+/]+=*( [^\r\n]*)?$", trimspace(file(var.ssh_public_key_file))))
    )
    error_message = "Reference an absolute existing file containing one OpenSSH Ed25519 public key."
  }
}
