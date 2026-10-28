terraform {
  required_version = ">= 1.10.0, < 2.0.0"

  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "1.70.1"
    }
  }
}

# Supply HCLOUD_TOKEN only through an approved secret-store process integration.
# Never put tokens in Terraform variables, files, user_data, or shell arguments.
provider "hcloud" {}
