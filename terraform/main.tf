locals {
  nodes = {
    es1 = { zone = "ru-central1-a", cidr = "10.77.1.0/24", ip = "10.77.1.10", disk = 20 }
    es2 = { zone = "ru-central1-d", cidr = "10.77.2.0/24", ip = "10.77.2.10", disk = 40 }
    es3 = { zone = "ru-central1-e", cidr = "10.77.3.0/24", ip = "10.77.3.10", disk = 20 }
  }
}
data "yandex_compute_image" "ubuntu" { family = "ubuntu-2404-lts" }
resource "yandex_vpc_network" "lab" { name = "otus-dz07" }
resource "yandex_vpc_subnet" "node" {
  for_each       = local.nodes
  name           = "otus-dz07-${each.key}"
  zone           = each.value.zone
  network_id     = yandex_vpc_network.lab.id
  v4_cidr_blocks = [each.value.cidr]
}
resource "yandex_vpc_security_group" "lab" {
  name       = "otus-dz07"
  network_id = yandex_vpc_network.lab.id
  ingress {
    protocol          = "ANY"
    predefined_target = "self_security_group"
  }
  ingress {
    protocol       = "TCP"
    port           = 443
    v4_cidr_blocks = var.admin_cidrs
  }
  ingress {
    protocol       = "TCP"
    port           = 80
    v4_cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    protocol       = "ANY"
    v4_cidr_blocks = ["0.0.0.0/0"]
  }
}
resource "yandex_compute_instance" "node" {
  for_each    = local.nodes
  name        = "otus-dz07-${each.key}"
  hostname    = "otus-dz07-${each.key}"
  platform_id = "standard-v3"
  zone        = each.value.zone
  labels      = { project = "otus-dz07" }
  resources {
    cores         = 2
    core_fraction = 100
    memory        = 8
  }
  boot_disk {
    auto_delete = true
    initialize_params {
      image_id = data.yandex_compute_image.ubuntu.id
      size     = each.value.disk
      type     = "network-hdd"
    }
  }
  network_interface {
    subnet_id          = yandex_vpc_subnet.node[each.key].id
    ip_address         = each.value.ip
    nat                = true
    security_group_ids = [yandex_vpc_security_group.lab.id]
  }
  metadata = {
    serial-port-enable = "1"
    user-data = "#cloud-config\n${yamlencode({
      users       = [{ name = "otus", groups = ["sudo"], shell = "/bin/bash", sudo = ["ALL=(ALL) NOPASSWD:ALL"], lock_passwd = true, ssh_authorized_keys = [trimspace(file(var.ssh_public_key_path))] }]
      ssh_pwauth  = false
      write_files = [{ path = "/etc/ssh/sshd_config.d/10-otus-port.conf", content = "Port 22\nPort 443\n", permissions = "0644" }]
      runcmd      = ["sshd -t && systemctl stop ssh.service ssh.socket && systemctl disable ssh.socket && systemctl enable --now ssh.service"]
    })}"
  }
}
output "lab" {
  value = { for name, node in yandex_compute_instance.node : name => {
    id        = node.id
    ip        = node.network_interface[0].ip_address
    public_ip = node.network_interface[0].nat_ip_address
  } }
}
