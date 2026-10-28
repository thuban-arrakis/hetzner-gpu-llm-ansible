terraform {
  required_version = ">= 1.10.0, < 2.0.0"

  required_providers {
    hetzner = {
      source  = "futo-org/hetzner"
      version = "1.1.0"
    }
  }
}

# The community Robot provider reads HETZNER_ROBOT_USERNAME and
# HETZNER_ROBOT_PASSWORD from the process environment. A Hetzner Cloud token
# cannot authenticate to Robot. Do not put credentials in Terraform variables.
provider "hetzner" {}
