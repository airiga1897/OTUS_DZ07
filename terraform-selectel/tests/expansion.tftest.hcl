# Offline provider mock: no cloud credentials, API calls or real infrastructure.
mock_provider "openstack" {}

variables {
  name_prefix       = "otus-dz07"
  subnet_cidr       = "10.77.0.0/24"
  region            = "ru-9"
  availability_zone = "ru-9a"
  image_id          = "00000000-0000-4000-8000-000000000001"
  volume_type       = "synthetic-volume-type"
  flavor_ids = {
    app = "00000000-0000-4000-8000-000000000002"
    es  = "00000000-0000-4000-8000-000000000004"
  }
  admin_cidrs         = ["192.0.2.1/32"]
  ssh_public_key_path = "tests/mock-public-key.txt"
  route_probe_key     = "mock-test-token-with-no-real-access"
  nodes = {
    app = { host = 10, role = "app" }
    es1 = { host = 31, role = "es" }
    es2 = { host = 32, role = "es" }
    es3 = { host = 33, role = "es" }
  }
}

run "initial" {
  command = apply
  assert {
    condition = openstack_networking_subnet_v2.lab.enable_dhcp && alltrue([
      for name, port in openstack_networking_port_v2.node :
      one(port.fixed_ip).ip_address == cidrhost(var.subnet_cidr, var.nodes[name].host)
    ])
    error_message = "DHCP must provide the IPv4 reserved on each Neutron port."
  }
  assert {
    condition = alltrue([for name, node in openstack_compute_instance_v2.node :
      alltrue([for file in yamldecode(node.user_data).write_files : !startswith(file.path, "/etc/netplan/")]) &&
      alltrue([for command in yamldecode(node.user_data).runcmd : !strcontains(command, "netplan")]) &&
      yamldecode(node.user_data).runcmd[1] == "python3 /root/${var.name_prefix}-check-ipv4.py ${cidrhost(var.subnet_cidr, var.nodes[name].host)}/${split("/", var.subnet_cidr)[1]} ${openstack_networking_subnet_v2.lab.gateway_ip} || { python3 /root/${var.name_prefix}-network-diagnostics.py; exit 1; }" &&
      length([for file in yamldecode(node.user_data).write_files : file if file.path == "/root/${var.name_prefix}-network-diagnostics.py" && file.permissions == "0600"]) == 1
    ])
    error_message = "Cloud networking must be preserved and IPv4 checked before starting services."
  }
  assert {
    condition = alltrue([for node in openstack_compute_instance_v2.node :
      yamldecode(node.user_data).runcmd[0] == "set -eu" &&
      alltrue([for command in yamldecode(node.user_data).runcmd : !strcontains(command, "&&")])
    ])
    error_message = "Bootstrap must stop on a failed command instead of hiding it behind a successful probe start."
  }
  assert {
    condition = alltrue([for node in openstack_compute_instance_v2.node :
      alltrue([for file in yamldecode(node.user_data).write_files : !startswith(file.path, "/etc/ssh/")]) &&
      alltrue([for command in yamldecode(node.user_data).runcmd : !strcontains(command, "ssh.service") && !strcontains(command, "ssh.socket")])
    ])
    error_message = "The image must retain control of SSH configuration and activation."
  }
  assert {
    condition     = join(",", openstack_networking_network_v2.lab.availability_zone_hints) == var.availability_zone && join(",", openstack_networking_router_v2.lab.availability_zone_hints) == var.availability_zone
    error_message = "Network and router must use the chosen segment."
  }
  assert {
    condition     = length(output.lab.server_ids) == 4
    error_message = "Initial topology must include app and three Elasticsearch nodes."
  }
}

run "repeat" {
  command = apply
  assert {
    condition     = output.lab.server_ids == run.initial.lab.server_ids && output.lab.volume_ids == run.initial.lab.volume_ids && output.lab.port_ids == run.initial.lab.port_ids
    error_message = "Repeat apply must preserve all four nodes, disks and ports."
  }
}

run "ru3b_pool_network" {
  command = plan
  variables {
    region                    = "ru-3"
    availability_zone         = "ru-3b"
    network_availability_zone = "ru-3a"
    router_availability_zone  = "ru-3a"
    volume_type               = "universal.ru-3b"
  }
  assert {
    condition     = alltrue([for node in openstack_compute_instance_v2.node : node.availability_zone == "ru-3b"]) && alltrue([for volume in openstack_blockstorage_volume_v3.boot : volume.availability_zone == "ru-3b" && volume.volume_type == "universal.ru-3b"])
    error_message = "VMs and disks must retain their compute/storage segment ru-3b."
  }
  assert {
    condition     = toset(openstack_networking_network_v2.lab.availability_zone_hints) == toset(["ru-3a"]) && toset(openstack_networking_router_v2.lab.availability_zone_hints) == toset(["ru-3a"]) && output.infrastructure.placement.zone == "ru-3b" && output.infrastructure.placement.network_zone == "ru-3a" && output.infrastructure.placement.router_zone == "ru-3a"
    error_message = "Pool network scheduling domains must be separate from the VM segment and saved in the infrastructure contract."
  }
}
