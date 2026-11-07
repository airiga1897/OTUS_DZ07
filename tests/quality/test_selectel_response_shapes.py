"""Keystone and Glance data boundaries use synthetic responses only."""

import json
from collections.abc import Mapping
from pathlib import Path
from unittest.mock import patch

import pool_selection as pools
import pytest
import selectel_api as api
from credentials import schema
from placement_config import PlacementPolicy


@pytest.mark.parametrize("name", [None, False, 42, [], {"private": "PRIVATE"}])
def test_cleanup_audit_rejects_unknown_names_instead_of_confirming_absence(name: object) -> None:
    from unittest.mock import Mock

    from selectel_adapter import audit_cleanup

    with (
        patch("selectel_adapter.collection", return_value=[{"name": name}]),
        pytest.raises(ValueError, match="resource name") as error,
    ):
        audit_cleanup(Mock(), "otus-dz07", set())
    assert "PRIVATE" not in str(error.value)


def test_json_int_rejects_booleans() -> None:
    assert api.json_int(22) == 22
    with pytest.raises(ValueError, match="integer"):
        api.json_int(True)


def test_json_texts_keep_only_text_collections() -> None:
    assert api.json_texts(["a", "b"]) == ["a", "b"]
    with pytest.raises(ValueError, match="text collection"):
        api.json_texts("a")
    with pytest.raises(ValueError, match="text collection"):
        api.json_texts([None])


def test_json_text_keeps_only_text() -> None:
    assert api.json_text("public") == "public"
    with pytest.raises(ValueError, match="text field"):
        api.json_text(None)


def test_provider_and_profile_boundaries_reject_foreign_shapes() -> None:
    from selectel_adapter import openstack_type, vm_profile

    assert openstack_type("openstack_compute_instance_v2") == "openstack_compute_instance_v2"
    with pytest.raises(ValueError, match="Foreign provider"):
        openstack_type(None)
    with pytest.raises(ValueError, match="Foreign provider"):
        openstack_type("foreign_instance")
    assert vm_profile({"vcpus": 2, "ram_mb": 4096}) == {"vcpus": 2, "ram_mb": 4096}
    with pytest.raises(ValueError, match="VM profile"):
        vm_profile({"vcpus": True, "ram_mb": 4096})


def test_optional_documents_and_identifiers_keep_their_boundaries() -> None:
    assert api.json_object_or_empty(None) == {}
    assert api.json_object_or_empty({"region": "ru-9"}) == {"region": "ru-9"}
    with pytest.raises(ValueError, match="response object"):
        api.json_object_or_empty([])
    assert api.identifier_set(None) == set()
    assert api.identifier_set("fip") == {"fip"}
    with pytest.raises(ValueError, match="text field"):
        api.identifier_set(True)
    assert api.text_mapping({"app": "id"}) == {"app": "id"}
    with pytest.raises(ValueError, match="response object"):
        api.text_mapping({"app": None})
    assert api.json_contains(None, "kibana") is False
    assert api.json_contains({"kibana": "id"}, "kibana") is True
    assert api.json_contains({"app": "id"}, "kibana") is False
    with pytest.raises(ValueError, match="response object"):
        api.json_contains([], "kibana")


@pytest.mark.parametrize("value", [None, [], "PRIVATE", {}, {"token": None}, {"token": []}, {"token": {0: "PRIVATE"}}])
def test_identity_scope_rejects_non_objects_without_disclosing_body(value: object) -> None:
    with pytest.raises(ValueError, match="Invalid identity response") as error:
        api.identity_scope(value)
    assert "PRIVATE" not in str(error.value)


@pytest.mark.parametrize(
    "value", [None, {}, [None], [{}], [{"type": None, "endpoints": []}], [{"type": "compute", "endpoints": None}]]
)
def test_bad_service_catalog_is_not_reported_as_an_empty_catalog(value: object) -> None:
    with pytest.raises(ValueError, match="service catalog"):
        api.checked_catalog(value)


@pytest.mark.parametrize(
    "endpoint",
    [
        None,
        {},
        {"interface": None, "url": "https://host.invalid"},
        {"interface": "public", "url": None},
        {"interface": "public", "url": "https://host.invalid", "region": []},
    ],
)
def test_malformed_endpoint_is_an_api_schema_error(endpoint: object) -> None:
    with pytest.raises(ValueError, match="catalog endpoint"):
        api.checked_catalog([{"type": "compute", "endpoints": [endpoint]}])


def test_global_nullable_region_does_not_become_a_compute_pool(caplog: pytest.LogCaptureFixture) -> None:
    catalog = api.checked_catalog(
        [
            {
                "type": "identity",
                "endpoints": [{"interface": "public", "url": "https://identity.invalid", "region": None}],
            },
            {
                "type": "compute",
                "endpoints": [{"interface": "public", "url": "https://compute.invalid", "region": None}],
            },
            *[
                {
                    "type": service,
                    "endpoints": [{"interface": "public", "url": "https://host.invalid/" + service, "region": "ru-2"}],
                }
                for service in pools.SERVICES
            ],
        ]
    )
    assert pools.ordered_pools({"pools": ["ru-2"]}, catalog) == ["ru-2"]
    assert "без имени пула" in caplog.text
    assert api.checked_catalog([]) == []
    assert api.identity_scope({"token": {"domain": {"name": "синтетический"}}}) == {"domain": {"name": "синтетический"}}


@pytest.mark.parametrize("headers", [{}, {"X-Subject-Token": None}, {"X-Subject-Token": ""}])
def test_missing_subject_token_is_rejected(headers: Mapping[str, str | None]) -> None:
    with pytest.raises(ValueError, match="token header unavailable"):
        api.subject_token(headers)


def test_authentication_checks_scope_and_catalog_before_returning_token() -> None:
    config = {
        "username": "synthetic",
        "password": "PRIVATE",
        "domain_name": "synthetic",
        "project_id": "own",
        "auth_url": schema()["auth_url"],
    }
    result = {"token": {"project": {"id": "own"}, "catalog": []}}
    with patch.object(api, "request_json", return_value=(result, {"X-Subject-Token": "token"})) as request:
        assert api.authenticate(config) == ("token", [])
    request.assert_called_once()
    with (
        patch.object(api, "request_json", return_value=({"token": {"project": None}}, {})),
        pytest.raises(ValueError, match="Unexpected project"),
    ):
        api.authenticate(config)
    assert api.subject_token({"X-Subject-Token": "token"}) == "token"


@pytest.mark.parametrize("document", [None, [], {}, {"api": None}])
def test_transport_configuration_requires_an_object(document: object) -> None:
    with (
        patch.object(Path, "read_text", return_value=json.dumps(document)),
        pytest.raises(ValueError, match="transport policy"),
    ):
        api.transport_policy()


@pytest.mark.parametrize(
    "page",
    [None, [], {}, {"images": None}, {"images": [None]}, {"images": [{0: "PRIVATE"}]}, {"images": [], "next": []}],
)
def test_glance_pages_fail_closed_without_silently_skipping_a_bad_response(page: object) -> None:
    with (
        patch.object(pools, "request_json", return_value=(page, {})),
        pytest.raises(ValueError, match="Glance image catalog"),
    ):
        pools.image_catalog("https://host.invalid/v2", "synthetic")


@pytest.mark.parametrize("image", [{}, {"id": None}, {"id": ""}, {"id": []}, {"id": "own", "created_at": None}])
def test_verified_image_order_requires_string_identity_and_timestamp(image: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="Glance image identity"):
        pools._image_order(image)


def test_image_order_accepts_missing_creation_time_without_inventing_one() -> None:
    assert pools._image_order({"id": "own"}) == ("", "own")
    assert pools._image_page({"images": [], "next": ""}) == ([], "")


@pytest.mark.parametrize("field", ["min_disk", "min_ram"])
@pytest.mark.parametrize("value", [None, "512", True, []])
def test_non_integer_glance_requirements_are_rejected_instead_of_raising_type_error(field: str, value: object) -> None:
    config: PlacementPolicy = {
        "image_name": "Ubuntu",
        "boot_disk_gb": 20,
        "profiles": {"app": {"ram_mb": 8192}},
        "image_checksums": ["a" * 32],
    }
    image = {
        "name": "Ubuntu",
        "visibility": "public",
        "status": "active",
        "os_distro": "ubuntu",
        "checksum": "a" * 32,
        field: value,
    }
    assert pools.compatible_images([image], config) == []
    assert field in pools.image_rejections(image, config)


@pytest.mark.parametrize("body", [None, [], {0: "PRIVATE"}])
def test_nova_requires_an_object_response_for_server_operations(body: object) -> None:
    client = api.NovaClient.__new__(api.NovaClient)
    client.endpoint, client.token = "https://host.invalid", "synthetic"
    with (
        patch.object(api, "request_json", return_value=(body, {})),
        pytest.raises(ValueError, match="Nova response object"),
    ):
        client.request("/servers/own")


@pytest.mark.parametrize("region", [None, ""])
def test_unknown_resource_pool_makes_audit_incomplete_instead_of_skipping_it(region: str | None) -> None:
    from selectel_adapter import SelectelAdapter

    cloud = SelectelAdapter.__new__(SelectelAdapter)
    cloud.catalog = api.checked_catalog(
        [{"type": "compute", "endpoints": [{"interface": "public", "url": "https://host.invalid", "region": region}]}]
    )
    with pytest.raises(ValueError, match="audit pool identity is unconfirmed"):
        cloud.resource_regions()


def test_read_only_pool_view_preserves_project_token_catalog_and_original_region() -> None:
    from selectel_adapter import SelectelAdapter

    cloud = SelectelAdapter.__new__(SelectelAdapter)
    cloud.config = {"region": "ru-2", "project_id": "own"}
    cloud.token = "synthetic-token"
    cloud.catalog = api.checked_catalog(
        [
            {
                "type": "identity",
                "endpoints": [{"interface": "public", "url": "https://identity.invalid", "region": None}],
            },
            {
                "type": "compute",
                "endpoints": [
                    {"interface": "internal", "url": "https://internal.invalid", "region": "ru-9"},
                    {"interface": "public", "url": "https://compute-2.invalid", "region": "ru-2"},
                    {"interface": "public", "url": "https://compute-3.invalid", "region": "ru-3"},
                ],
            },
        ]
    )
    view = cloud.read_only_pool("ru-3")
    assert cloud.config["region"] == "ru-2"
    assert view.config == {"region": "ru-3", "project_id": "own"}
    assert view.token == cloud.token
    assert view.catalog is cloud.catalog
    assert view.endpoints == {"compute": "https://compute-3.invalid"}
    assert cloud.resource_regions() == ["ru-2", "ru-3"]
