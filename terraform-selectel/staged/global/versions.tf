terraform {
  required_version = ">= 1.6.0, < 2.0.0"
  required_providers { selectel = { source = "selectel/selectel", version = "= 8.6.0" } }
}
provider "selectel" {}
