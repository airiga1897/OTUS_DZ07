variable "name" { type = string }
variable "owner" { type = string }
resource "selectel_global_router_router_v1" "lab" {
  name = var.name
  tags = [var.owner]
}
output "result" { value = { router_id = selectel_global_router_router_v1.lab.id } }
