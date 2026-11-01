mock_provider "selectel" {}
variables {
  name  = "otus-dz07-global"
  owner = "otus-dz07-synthetic"
}
run "initial" {
  command = apply
  assert {
    condition     = toset(selectel_global_router_router_v1.lab.tags) == toset([var.owner])
    error_message = "Stage must retain required network, storage and ownership properties."
  }
}
run "repeat" {
  command = apply
  assert {
    condition     = output.result.router_id == run.initial.result.router_id
    error_message = "Repeat apply must preserve every stage identity."
  }
}
