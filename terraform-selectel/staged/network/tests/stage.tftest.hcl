mock_provider "openstack" {}
variables {
  name                = "otus-dz07-network-ru-3"
  region              = "ru-3"
  cidr                = "10.78.0.0/24"
  network_zone        = "ru-3a"
  router_zone         = "ru-3a"
  owner               = "otus-dz07-synthetic"
  admin_cidrs         = ["192.0.2.1/32"]
  peer_cidrs          = ["10.77.0.0/24"]
  kibana_port         = 5601
  cloud_gateway_host  = 1
  global_gateway_host = 2
  dhcp_first_host     = 200
  dhcp_last_host      = 249
}
run "initial" {
  command = apply
  assert {
    condition     = openstack_networking_subnet_v2.lab.gateway_ip == "10.78.0.1" && openstack_networking_subnet_route_v2.peer["10.77.0.0/24"].next_hop == "10.78.0.2" && toset(openstack_compute_servergroup_v2.es.policies) == toset(["anti-affinity"])
    error_message = "Stage must retain required network, storage and ownership properties."
  }
}
run "repeat" {
  command = apply
  assert {
    condition     = output.result.network_id == run.initial.result.network_id && output.result.server_group_id == run.initial.result.server_group_id
    error_message = "Repeat apply must preserve every stage identity."
  }
}
