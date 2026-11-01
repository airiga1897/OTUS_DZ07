variable "region" { type = string }
variable "nodes" {
  type = map(object({ host = number, role = string }))
}
variable "etcd_members" {
  type = list(string)
}
variable "availability_zone" { type = string }
variable "network_availability_zone" {
  type    = string
  default = null
}
variable "router_availability_zone" {
  type    = string
  default = null
}
variable "image_id" { type = string }
variable "volume_type" { type = string }
variable "flavor_ids" { type = map(string) }
variable "external_network_name" {
  type    = string
  default = "external-network"
}
variable "name_prefix" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]+$", var.name_prefix))
    error_message = "Use a lowercase resource namespace."
  }
}
variable "subnet_cidr" {
  type = string
}
variable "admin_cidrs" {
  type = list(string)
  validation {
    condition     = length(var.admin_cidrs) > 0 && alltrue([for c in var.admin_cidrs : can(cidrnetmask(c)) && split("/", c)[1] == "32"])
    error_message = "Provide operator IPv4 /32 addresses."
  }
}
variable "ssh_public_key_path" {
  type    = string
  default = "../.local/otus_dz07.pub"
}
variable "boot_disk_gb" {
  type    = number
  default = 20
}
variable "route_probe_key" {
  type      = string
  sensitive = true
}
variable "kibana_port" {
  type    = number
  default = 5601
}
