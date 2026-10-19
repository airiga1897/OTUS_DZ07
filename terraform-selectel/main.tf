locals {
  nodes = {
    es1 = { ip = "10.77.1.10", disk = 20 }
    es2 = { ip = "10.77.2.10", disk = 40 }
    es3 = { ip = "10.77.3.10", disk = 20 }
  }
}
data "openstack_networking_network_v2" "external" {
  name     = "external-network"
  external = true
}
resource "openstack_networking_network_v2" "lab" { name = "otus-dz07" }
resource "openstack_networking_subnet_v2" "lab" {
  name            = "otus-dz07"
  network_id      = openstack_networking_network_v2.lab.id
  cidr            = "10.77.0.0/16"
  ip_version      = 4
  dns_nameservers = ["77.88.8.8", "1.1.1.1"]
}
resource "openstack_networking_router_v2" "lab" {
  name                = "otus-dz07"
  external_network_id = data.openstack_networking_network_v2.external.id
}
resource "openstack_networking_router_interface_v2" "lab" {
  router_id = openstack_networking_router_v2.lab.id
  subnet_id = openstack_networking_subnet_v2.lab.id
}
resource "openstack_networking_secgroup_v2" "lab" { name = "otus-dz07" }
resource "openstack_networking_secgroup_rule_v2" "internal" {
  direction         = "ingress"
  ethertype         = "IPv4"
  remote_group_id   = openstack_networking_secgroup_v2.lab.id
  security_group_id = openstack_networking_secgroup_v2.lab.id
}
resource "openstack_networking_secgroup_rule_v2" "ssh" {
  for_each          = toset(var.admin_cidrs)
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 443
  port_range_max    = 443
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
resource "openstack_networking_port_v2" "node" {
  for_each           = local.nodes
  name               = "otus-dz07-${each.key}"
  network_id         = openstack_networking_network_v2.lab.id
  security_group_ids = [openstack_networking_secgroup_v2.lab.id]
  fixed_ip {
    subnet_id  = openstack_networking_subnet_v2.lab.id
    ip_address = each.value.ip
  }
}
resource "openstack_compute_servergroup_v2" "lab" {
  name     = "otus-dz07"
  policies = ["anti-affinity"]
}
resource "openstack_blockstorage_volume_v3" "boot" {
  for_each          = local.nodes
  name              = "otus-dz07-${each.key}-boot"
  size              = each.value.disk
  image_id          = var.image_id
  volume_type       = var.volume_type
  availability_zone = var.availability_zone
}
resource "openstack_compute_instance_v2" "node" {
  for_each          = local.nodes
  name              = "otus-dz07-${each.key}"
  flavor_id         = var.flavor_id
  availability_zone = var.availability_zone
  block_device {
    uuid                  = openstack_blockstorage_volume_v3.boot[each.key].id
    source_type           = "volume"
    destination_type      = "volume"
    boot_index            = 0
    delete_on_termination = false
  }
  network { port = openstack_networking_port_v2.node[each.key].id }
  scheduler_hints { group = openstack_compute_servergroup_v2.lab.id }
  user_data = "#cloud-config\n${yamlencode({
    hostname         = "otus-dz07-${each.key}"
    manage_etc_hosts = true
    users            = [{ name = "otus", groups = ["sudo"], shell = "/bin/bash", sudo = "ALL=(ALL) NOPASSWD:ALL", lock_passwd = true, ssh_authorized_keys = [trimspace(file(var.ssh_public_key_path))] }]
    ssh_pwauth       = false
    write_files      = [{ path = "/etc/ssh/sshd_config.d/10-otus-port.conf", content = "Port 22\nPort 443\n", permissions = "0644" }]
    runcmd           = ["sshd -t && systemctl stop ssh.service ssh.socket && systemctl disable ssh.socket && systemctl enable --now ssh.service"]
  })}"
  depends_on = [openstack_networking_router_interface_v2.lab]
}
resource "openstack_networking_floatingip_v2" "node" {
  for_each = local.nodes
  pool     = data.openstack_networking_network_v2.external.name
}
resource "openstack_networking_floatingip_associate_v2" "node" {
  for_each    = local.nodes
  floating_ip = openstack_networking_floatingip_v2.node[each.key].address
  port_id     = openstack_networking_port_v2.node[each.key].id
  depends_on  = [openstack_networking_router_interface_v2.lab]
}
output "lab" {
  value = { for name, node in openstack_compute_instance_v2.node : name => {
    id        = node.id
    ip        = local.nodes[name].ip
    public_ip = openstack_networking_floatingip_v2.node[name].address
  } }
}
