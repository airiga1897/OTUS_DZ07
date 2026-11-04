"""Malformed provider DNS metadata must not authorize Terraform refresh."""

import pytest
from selectel_refresh import _dns


@pytest.mark.parametrize("address,name", [(None, "es1"), ([], "es1"), ("10.77.0.31", None), ("10.77.0.31", 42)])
def test_non_string_dns_identity_is_rejected(address: object, name: object) -> None:
    before = {"dns_name": name, "dns_assignment": []}
    after = {
        "dns_name": name,
        "dns_assignment": [{"ip_address": address, "hostname": name, "fqdn": "es1.example.invalid."}],
    }
    assert _dns(before, after, address, name) is False
