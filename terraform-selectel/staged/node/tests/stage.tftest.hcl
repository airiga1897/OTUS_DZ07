mock_provider "openstack" {}
variables {
  region                = "ru-3"
  node_name             = "es2"
  role                  = "es"
  owner                 = "otus-dz07-synthetic"
  name_prefix           = "otus-dz07"
  zone                  = "ru-3b"
  private_ip            = "10.78.0.32"
  subnet_cidr           = "10.78.0.0/24"
  image_id              = "synthetic-image"
  flavor_id             = "synthetic-flavor"
  volume_type           = "universal.ru-3b"
  boot_disk_gb          = 20
  network_id            = "synthetic-network"
  subnet_id             = "synthetic-subnet"
  security_group_id     = "synthetic-security"
  server_group_id       = "synthetic-group"
  external_network_name = "external-network"
  ssh_public_key_path   = "../../tests/mock-public-key.txt"
  route_probe_key       = "synthetic-route-proof"
  subnet_gateway        = "10.78.0.1"
}
run "initial" {
  command = apply
  assert {
    condition     = one(openstack_compute_instance_v2.node.scheduler_hints).group == "synthetic-group" && openstack_blockstorage_volume_v3.boot.availability_zone == "ru-3b" && openstack_compute_instance_v2.node.metadata["dz07_owner"] == var.owner
    error_message = "Stage must retain required network, storage and ownership properties."
  }
}
run "repeat" {
  command = apply
  assert {
    condition     = output.result.id == run.initial.result.id && output.result.port_id == run.initial.result.port_id && output.result.volume_id == run.initial.result.volume_id
    error_message = "Repeat apply must preserve every stage identity."
  }
}
run "diagnostic_without_host_requirement" {
  command = plan
  variables {
    require_distinct_hosts = false
  }
  assert {
    condition     = length(openstack_compute_instance_v2.node.scheduler_hints) == 0
    error_message = "Diagnostic mode must omit the scheduler_hints block."
  }
}
run "app_never_requests_host_separation" {
  command = plan
  variables {
    role                   = "app"
    require_distinct_hosts = true
  }
  assert {
    condition     = length(openstack_compute_instance_v2.node.scheduler_hints) == 0
    error_message = "The application VM must not request an Elasticsearch server group."
  }
}
