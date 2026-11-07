mock_provider "selectel" {}
variables {
  region              = "ru-3"
  name                = "otus-dz07-connection-ru-3"
  owner               = "otus-dz07-synthetic"
  project_id          = "synthetic-project"
  router_id           = "synthetic-router"
  os_network_id       = "synthetic-network"
  os_subnet_id        = "synthetic-subnet"
  cidr                = "10.78.0.0/24"
  global_gateway_host = 2
  service_hosts       = [253, 254]
}
run "initial" {
  command = apply
  assert {
    condition     = selectel_global_router_vpc_subnet_v1.lab.gateway == "10.78.0.2" && toset(selectel_global_router_vpc_subnet_v1.lab.service_addresses) == toset(["10.78.0.253", "10.78.0.254"]) && selectel_global_router_vpc_network_v1.lab.project_id == var.project_id
    error_message = "Stage must retain required network, storage and ownership properties."
  }
}
run "repeat" {
  command = apply
  assert {
    condition     = output.result.network_id == run.initial.result.network_id && output.result.subnet_id == run.initial.result.subnet_id
    error_message = "Repeat apply must preserve every stage identity."
  }
}
