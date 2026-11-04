"""Global Router boundaries validate consumed fields and retain unrelated metadata."""

from unittest.mock import patch

import pytest
from selectel_global_router import (
    GlobalRouter,
    group_ids,
    identifier,
    matches_namespace,
    quota_limit,
    response_rows,
    tags,
)


@pytest.mark.parametrize("value", [None, True, [], {}, ""])
def test_unknown_identifiers_are_not_auditable(value: object) -> None:
    with pytest.raises(ValueError, match="identifier"):
        identifier(value)


@pytest.mark.parametrize("value", [None, {}, [None], [{1: "bad"}]])
def test_bad_collection_shapes(value: object) -> None:
    with pytest.raises(ValueError, match="response"):
        response_rows(value)


@pytest.mark.parametrize("value", [None, "owner", [1]])
def test_invalid_tags_cannot_confirm_ownership(value: object) -> None:
    with pytest.raises(ValueError, match="ownership tags"):
        tags({"tags": value})


@pytest.mark.parametrize("value", [None, True, "10", -2])
def test_unknown_quota_is_not_capacity(value: object) -> None:
    with pytest.raises(ValueError, match="quota limit"):
        quota_limit({"limit": value})


def test_namespace_checks_only_expected_name_and_tags() -> None:
    assert matches_namespace({"name": "otus-dz07-router", "unused": None}, "otus-dz07")
    assert matches_namespace({"name": "other", "tags": ["otus-dz07-owner"]}, "otus-dz07")
    assert not matches_namespace({"name": "other"}, "otus-dz07")
    with pytest.raises(ValueError, match="resource name"):
        matches_namespace({"name": None}, "otus-dz07")


def test_known_identifier_and_quota_forms() -> None:
    assert identifier("local") == "local"
    assert identifier(2) == 2
    assert group_ids([{"id": "one"}, {"id": "two"}]) == {"one", "two"}
    assert tags({}) == []
    assert tags({"tags": ["owner"]}) == ["owner"]
    assert quota_limit({"limit": -1}) == -1
    assert quota_limit({"limit": 0}) == 0


def test_no_new_attachment_does_not_require_unused_limit() -> None:
    router = GlobalRouter({})
    quotas = [
        {"name": name, "scope": "router_id", "scope_value": "router", "limit": None} for name in ("networks", "subnets")
    ]
    router.check_attachment_quotas(
        quotas,
        [{"id": "network", "router_id": "router"}],
        [{"id": "subnet", "network_id": "network"}],
        ["ru-2"],
        "router",
    )


def test_cleanup_does_not_validate_unneeded_resource_name() -> None:
    router = GlobalRouter({})
    with patch.object(router, "listing", return_value=[{"id": "foreign", "name": None, "tags": []}]):
        router.assert_absent({}, "local-owner")


def test_mixed_group_identity_types_are_unknown_not_unsupported() -> None:
    router = GlobalRouter({})
    zones = [{"name": "ru-2", "service": "vpc", "enable": True, "groups": [{"id": "one"}, {"id": 2}]}]
    with patch.object(router, "listing", return_value=zones), pytest.raises(ValueError, match="mixed"):
        router.compatible_regions(["ru-2"])


def test_bad_record_cannot_become_successful_listing() -> None:
    router = GlobalRouter({})
    with patch.object(router, "get", return_value=[{"id": None}]), pytest.raises(ValueError, match="identifier"):
        router.listing("networks")
