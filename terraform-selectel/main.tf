locals {
  nodes     = var.nodes
  addresses = { for name, n in local.nodes : name => cidrhost(var.subnet_cidr, n.host) }
}
data "openstack_networking_network_v2" "external" {
  name     = var.external_network_name
  external = true
}
resource "openstack_networking_network_v2" "lab" {
  name                    = var.name_prefix
  port_security_enabled   = true
  availability_zone_hints = [coalesce(var.network_availability_zone, var.availability_zone)]
}
resource "openstack_networking_subnet_v2" "lab" {
  name            = var.name_prefix
  network_id      = openstack_networking_network_v2.lab.id
  cidr            = var.subnet_cidr
  ip_version      = 4
  enable_dhcp     = true
  dns_nameservers = ["77.88.8.8", "1.1.1.1"]
}
resource "openstack_networking_router_v2" "lab" {
  name                    = var.name_prefix
  availability_zone_hints = [coalesce(var.router_availability_zone, var.availability_zone)]
  external_network_id     = data.openstack_networking_network_v2.external.id
}
resource "openstack_networking_router_interface_v2" "lab" {
  router_id = openstack_networking_router_v2.lab.id
  subnet_id = openstack_networking_subnet_v2.lab.id
}
resource "openstack_networking_secgroup_v2" "lab" {
  name        = var.name_prefix
  description = "DZ07 private cluster; public SSH restricted to operator"
}
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
  port_range_min    = 22
  port_range_max    = 22
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
  name               = "${var.name_prefix}-${each.key}"
  network_id         = openstack_networking_network_v2.lab.id
  security_group_ids = [openstack_networking_secgroup_v2.lab.id]
  fixed_ip {
    subnet_id  = openstack_networking_subnet_v2.lab.id
    ip_address = local.addresses[each.key]
  }
}
resource "openstack_compute_servergroup_v2" "es" {
  name     = "${var.name_prefix}-es"
  policies = ["anti-affinity"]
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
resource "openstack_blockstorage_volume_v3" "boot" {
  for_each          = local.nodes
  name              = "${var.name_prefix}-${each.key}-boot"
  size              = var.boot_disk_gb
  image_id          = var.image_id
  volume_type       = var.volume_type
  availability_zone = var.availability_zone
}
resource "openstack_compute_instance_v2" "node" {
  for_each          = local.nodes
  name              = "${var.name_prefix}-${each.key}"
  flavor_id         = var.flavor_ids[each.value.role]
  availability_zone = var.availability_zone
  block_device {
    uuid                  = openstack_blockstorage_volume_v3.boot[each.key].id
    source_type           = "volume"
    destination_type      = "volume"
    boot_index            = 0
    delete_on_termination = false
  }
  network { port = openstack_networking_port_v2.node[each.key].id }
  dynamic "scheduler_hints" {
    for_each = each.value.role == "es" ? [1] : []
    content { group = openstack_compute_servergroup_v2.es.id }
  }
  user_data = "#cloud-config\n${yamlencode({
    hostname         = "${var.name_prefix}-${each.key}"
    manage_etc_hosts = true
    users            = [{ name = "otus", groups = ["sudo"], shell = "/bin/bash", sudo = "ALL=(ALL) NOPASSWD:ALL", lock_passwd = true, ssh_authorized_keys = [trimspace(file(pathexpand(var.ssh_public_key_path)))] }]
    ssh_pwauth       = false
    write_files = concat([{
      path        = "/root/${var.name_prefix}-check-ipv4.py"
      permissions = "0600"
      content     = file("${path.module}/../scripts/remote/check_ipv4.py")
      }, {
      path        = "/root/${var.name_prefix}-network-diagnostics.py"
      permissions = "0600"
      content     = file("${path.module}/../scripts/remote/network_diagnostics.py")
      }], each.value.role == "app" ? [{
      path        = "/root/${var.name_prefix}-route.py"
      permissions = "0600"
      content     = templatefile("${path.module}/../config/route-probe.py.tftpl", { probe_key = var.route_probe_key })
      }, {
      path        = "/etc/systemd/system/${var.name_prefix}-route.service"
      permissions = "0644"
      content     = "[Unit]\nAfter=network.target\n[Service]\nExecStart=/usr/bin/python3 /root/${var.name_prefix}-route.py\nRestart=on-failure\n[Install]\nWantedBy=multi-user.target\n"
    }] : [])
    runcmd = concat([
      "set -eu",
      "python3 /root/${var.name_prefix}-check-ipv4.py ${local.addresses[each.key]}/${split("/", var.subnet_cidr)[1]} ${openstack_networking_subnet_v2.lab.gateway_ip} || { python3 /root/${var.name_prefix}-network-diagnostics.py; exit 1; }"
      ], each.value.role == "app" ? [
      "systemctl daemon-reload",
      "systemctl enable --now ${var.name_prefix}-route.service"
    ] : [])
  })}"
  depends_on = [openstack_networking_router_interface_v2.lab]
}
resource "openstack_networking_floatingip_v2" "app" { pool = data.openstack_networking_network_v2.external.name }
resource "openstack_networking_floatingip_associate_v2" "app" {
  floating_ip = openstack_networking_floatingip_v2.app.address
  port_id     = openstack_networking_port_v2.node["app"].id
  depends_on  = [openstack_networking_router_interface_v2.lab]
}
output "lab" {
  value = {
    site_url      = "http://${openstack_networking_floatingip_v2.app.address}"
    app_public_ip = openstack_networking_floatingip_v2.app.address
    private_ips   = local.addresses
    subnet_cidr   = var.subnet_cidr
    server_ids    = { for name, n in openstack_compute_instance_v2.node : name => n.id }
    volume_ids    = { for name, n in openstack_blockstorage_volume_v3.boot : name => n.id }
    port_ids      = { for name, n in openstack_networking_port_v2.node : name => n.id }
  }
}

output "infrastructure" {
  value = {
    provider  = "selectel"
    scope     = { project_id = openstack_networking_network_v2.lab.tenant_id }
    namespace = var.name_prefix
    placement = merge({ region = var.region, zone = var.availability_zone },
      coalesce(var.network_availability_zone, var.availability_zone) == var.availability_zone && coalesce(var.router_availability_zone, var.availability_zone) == var.availability_zone ? {} : {
        network_zone = coalesce(var.network_availability_zone, var.availability_zone)
        router_zone  = coalesce(var.router_availability_zone, var.availability_zone)
    })
    subnet_cidr = var.subnet_cidr
    nodes = { for name, node in var.nodes : name => {
      roles      = [node.role]
      id         = openstack_compute_instance_v2.node[name].id
      volume_id  = openstack_blockstorage_volume_v3.boot[name].id
      port_id    = openstack_networking_port_v2.node[name].id
      private_ip = local.addresses[name]
      public_ip  = name == "app" ? openstack_networking_floatingip_v2.app.address : null
    } }
  }
}
