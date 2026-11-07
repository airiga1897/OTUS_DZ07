variable "region" { type = string }
variable "name" { type = string }
variable "cidr" { type = string }
variable "network_zone" { type = string }
variable "router_zone" { type = string }
variable "admin_cidrs" { type = list(string) }
variable "peer_cidrs" { type = list(string) }
variable "kibana_port" { type = number }
variable "owner" { type = string }
variable "cloud_gateway_host" { type = number }
variable "global_gateway_host" { type = number }
variable "dhcp_first_host" { type = number }
variable "dhcp_last_host" { type = number }
data "openstack_networking_network_v2" "external" {
  name     = "external-network"
  external = true
}
resource "openstack_networking_network_v2" "lab" {
  name                    = var.name
  port_security_enabled   = true
  availability_zone_hints = [var.network_zone]
  tags                    = [var.owner]
}
resource "openstack_networking_subnet_v2" "lab" {
  name            = var.name
  network_id      = openstack_networking_network_v2.lab.id
  cidr            = var.cidr
  ip_version      = 4
  enable_dhcp     = true
  gateway_ip      = cidrhost(var.cidr, var.cloud_gateway_host)
  dns_nameservers = ["77.88.8.8", "1.1.1.1"]
  allocation_pool {
    start = cidrhost(var.cidr, var.dhcp_first_host)
    end   = cidrhost(var.cidr, var.dhcp_last_host)
  }
}
resource "openstack_networking_subnet_route_v2" "peer" {
  for_each         = toset(var.peer_cidrs)
  subnet_id        = openstack_networking_subnet_v2.lab.id
  destination_cidr = each.value
  next_hop         = cidrhost(var.cidr, var.global_gateway_host)
}
resource "openstack_networking_router_v2" "lab" {
  name                    = var.name
  availability_zone_hints = [var.router_zone]
  external_network_id     = data.openstack_networking_network_v2.external.id
  tags                    = [var.owner]
}
resource "openstack_networking_router_interface_v2" "lab" {
  router_id = openstack_networking_router_v2.lab.id
  subnet_id = openstack_networking_subnet_v2.lab.id
}
resource "openstack_networking_secgroup_v2" "lab" {
  name        = var.name
  description = var.owner
}
resource "openstack_networking_secgroup_rule_v2" "internal" {
  for_each          = toset(concat([var.cidr], var.peer_cidrs))
  direction         = "ingress"
  ethertype         = "IPv4"
  remote_ip_prefix  = each.value
  security_group_id = openstack_networking_secgroup_v2.lab.id
}
resource "openstack_networking_secgroup_rule_v2" "ssh" {
  for_each          = toset(var.admin_cidrs)
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 22
  port_range_max    = 22
  remote_ip_prefix  = each.value
  security_group_id = openstack_networking_secgroup_v2.lab.id
}
resource "openstack_networking_secgroup_rule_v2" "kibana" {
  for_each          = toset(var.admin_cidrs)
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = var.kibana_port
  port_range_max    = var.kibana_port
  remote_ip_prefix  = each.value
  security_group_id = openstack_networking_secgroup_v2.lab.id
}
resource "openstack_networking_secgroup_rule_v2" "http" {
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 80
  port_range_max    = 80
  remote_ip_prefix  = "0.0.0.0/0"
  security_group_id = openstack_networking_secgroup_v2.lab.id
}
resource "openstack_compute_servergroup_v2" "es" {
  name     = "${var.name}-es"
  policies = ["anti-affinity"]
}
output "result" {
  value = {
    gateway_ip            = openstack_networking_subnet_v2.lab.gateway_ip
    network_id            = openstack_networking_network_v2.lab.id
    subnet_id             = openstack_networking_subnet_v2.lab.id
    security_group_id     = openstack_networking_secgroup_v2.lab.id
    server_group_id       = openstack_compute_servergroup_v2.es.id
    external_network_name = data.openstack_networking_network_v2.external.name
    cidr                  = var.cidr
    region                = var.region
    network_zone          = var.network_zone
    router_zone           = var.router_zone
  }
}
