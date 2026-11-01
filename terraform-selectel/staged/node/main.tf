variable "region" { type = string }
variable "owner" { type = string }
variable "node_name" { type = string }
variable "role" { type = string }
variable "name_prefix" { type = string }
variable "zone" { type = string }
variable "private_ip" { type = string }
variable "subnet_cidr" { type = string }
variable "subnet_gateway" { type = string }
variable "image_id" { type = string }
variable "flavor_id" { type = string }
variable "volume_type" { type = string }
variable "boot_disk_gb" { type = number }
variable "network_id" { type = string }
variable "subnet_id" { type = string }
variable "security_group_id" { type = string }
variable "server_group_id" { type = string }
variable "require_distinct_hosts" {
  type    = bool
  default = true
}
variable "external_network_name" { type = string }
variable "ssh_public_key_path" { type = string }
variable "route_probe_key" {
  type      = string
  sensitive = true
}
resource "openstack_networking_port_v2" "node" {
  name               = "${var.name_prefix}-${var.node_name}"
  network_id         = var.network_id
  security_group_ids = [var.security_group_id]
  fixed_ip {
    subnet_id  = var.subnet_id
    ip_address = var.private_ip
  }
}
resource "openstack_blockstorage_volume_v3" "boot" {
  name              = "${var.name_prefix}-${var.node_name}-boot"
  metadata          = { dz07_owner = var.owner }
  size              = var.boot_disk_gb
  image_id          = var.image_id
  volume_type       = var.volume_type
  availability_zone = var.zone
}
resource "openstack_compute_instance_v2" "node" {
  name              = "${var.name_prefix}-${var.node_name}"
  metadata          = { dz07_owner = var.owner }
  flavor_id         = var.flavor_id
  availability_zone = var.zone
  block_device {
    uuid                  = openstack_blockstorage_volume_v3.boot.id
    source_type           = "volume"
    destination_type      = "volume"
    boot_index            = 0
    delete_on_termination = false
  }
  network { port = openstack_networking_port_v2.node.id }
  dynamic "scheduler_hints" {
    for_each = var.role == "es" && var.require_distinct_hosts ? [1] : []
    content { group = var.server_group_id }
  }
  user_data = "#cloud-config\n${yamlencode({
    hostname         = "${var.name_prefix}-${var.node_name}"
    manage_etc_hosts = true
    users            = [{ name = "otus", groups = ["sudo"], shell = "/bin/bash", sudo = "ALL=(ALL) NOPASSWD:ALL", lock_passwd = true, ssh_authorized_keys = [trimspace(file(pathexpand(var.ssh_public_key_path)))] }]
    ssh_pwauth       = false
    write_files = concat([{
      path        = "/root/${var.name_prefix}-check-ipv4.py"
      permissions = "0600"
      content     = file("${path.module}/../../../scripts/remote/check_ipv4.py")
      }, {
      path        = "/root/${var.name_prefix}-network-diagnostics.py"
      permissions = "0600"
      content     = file("${path.module}/../../../scripts/remote/network_diagnostics.py")
      }], var.role == "app" ? [{
      path        = "/root/${var.name_prefix}-route.py"
      permissions = "0600"
      content     = templatefile("${path.module}/../../../config/route-probe.py.tftpl", { probe_key = var.route_probe_key })
      }, {
      path        = "/etc/systemd/system/${var.name_prefix}-route.service"
      permissions = "0644"
      content     = "[Unit]\nAfter=network.target\n[Service]\nExecStart=/usr/bin/python3 /root/${var.name_prefix}-route.py\nRestart=on-failure\n[Install]\nWantedBy=multi-user.target\n"
    }] : [])
    runcmd = concat([
      "set -eu",
      "python3 /root/${var.name_prefix}-check-ipv4.py ${var.private_ip}/${split("/", var.subnet_cidr)[1]} ${var.subnet_gateway} || { python3 /root/${var.name_prefix}-network-diagnostics.py; exit 1; }"
      ], var.role == "app" ? [
      "systemctl daemon-reload",
      "systemctl enable --now ${var.name_prefix}-route.service"
    ] : [])
  })}"
}
resource "openstack_networking_floatingip_v2" "app" {
  count       = var.role == "app" ? 1 : 0
  pool        = var.external_network_name
  description = "${var.name_prefix}-${var.node_name}:${var.owner}"
}
resource "openstack_networking_floatingip_associate_v2" "app" {
  count       = var.role == "app" ? 1 : 0
  floating_ip = openstack_networking_floatingip_v2.app[0].address
  port_id     = openstack_networking_port_v2.node.id
}
output "result" {
  value = {
    id          = openstack_compute_instance_v2.node.id
    volume_id   = openstack_blockstorage_volume_v3.boot.id
    port_id     = openstack_networking_port_v2.node.id
    private_ip  = var.private_ip
    public_ip   = var.role == "app" ? openstack_networking_floatingip_v2.app[0].address : null
    roles       = [var.role]
    region      = var.region
    zone        = var.zone
    subnet_cidr = var.subnet_cidr
  }
}
