variable "region" { type = string }
variable "name" { type = string }
variable "owner" { type = string }
variable "project_id" { type = string }
variable "router_id" { type = string }
variable "os_network_id" { type = string }
variable "os_subnet_id" { type = string }
variable "cidr" { type = string }
variable "global_gateway_host" { type = number }
variable "service_hosts" { type = list(number) }
data "selectel_global_router_zone_v1" "pool" {
  name    = var.region
  service = "vpc"
}
resource "selectel_global_router_vpc_network_v1" "lab" {
  name          = var.name
  router_id     = var.router_id
  zone_id       = data.selectel_global_router_zone_v1.pool.id
  os_network_id = var.os_network_id
  project_id    = var.project_id
  tags          = [var.owner]
}
resource "selectel_global_router_vpc_subnet_v1" "lab" {
  name              = var.name
  network_id        = selectel_global_router_vpc_network_v1.lab.id
  os_subnet_id      = var.os_subnet_id
  cidr              = var.cidr
  gateway           = cidrhost(var.cidr, var.global_gateway_host)
  service_addresses = [for host in var.service_hosts : cidrhost(var.cidr, host)]
  tags              = [var.owner]
}
output "result" {
  value = { network_id = selectel_global_router_vpc_network_v1.lab.id
  subnet_id = selectel_global_router_vpc_subnet_v1.lab.id }
}
