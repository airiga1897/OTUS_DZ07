variable "region" { type = string }
variable "availability_zone" { type = string }
variable "image_id" { type = string }
variable "flavor_id" { type = string }
variable "volume_type" { type = string }
variable "admin_cidrs" {
  type = list(string)
  validation {
    condition     = length(var.admin_cidrs) > 0 && alltrue([for c in var.admin_cidrs : can(cidrnetmask(c)) && split("/", c)[1] == "32"])
    error_message = "Specify operator IPv4 /32 addresses."
  }
}
variable "ssh_public_key_path" {
  type    = string
  default = "../.local/otus_dz07.pub"
}
