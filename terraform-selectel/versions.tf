terraform {
  required_version = ">= 1.6.0, < 2.0.0"
  required_providers {
    openstack = {
      source  = "terraform-provider-openstack/openstack"
      version = "= 3.4.0"
    }
  }
}
# OS_AUTH_URL, OS_PROJECT_ID, OS_USER_DOMAIN_NAME, OS_USERNAME, OS_PASSWORD
# задаются в окружении оператора. Учётные записи и проект создаются отдельно.
provider "openstack" {
  region = var.region
}
