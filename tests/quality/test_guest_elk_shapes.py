"""Guest Elasticsearch client diagnostics never raise an unset exception."""

from unittest.mock import patch

import pytest
from remote import elk_api


def test_no_endpoint_reports_configuration_problem_without_network() -> None:
    with (
        patch.object(elk_api, "C", {"ips": {}}),
        patch.object(elk_api, "S", {"elastic": "synthetic-only"}),
        patch.object(elk_api.urllib.request, "urlopen") as request,
        pytest.raises(RuntimeError, match="endpoint"),
    ):
        elk_api.api("/")
    request.assert_not_called()


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {1: "bad"},
        {"status": False},
        {"cluster_uuid": None},
        {"count": True},
        {"count": -1},
        {"number_of_nodes": "3"},
        {"found": 1},
        {"_source": None},
        {"hits": {"hits": None}},
        {"hits": {"hits": [None]}},
        {"nodes": {"id": {"name": None}}},
        {"aggregations": {"services": {"buckets": [{"key": 2}]}}},
    ],
)
def test_invalid_elasticsearch_response(value: object) -> None:
    with pytest.raises(ValueError, match="ELK"):
        elk_api._elasticsearch_response(value)


@pytest.mark.parametrize(
    "value", [None, {"status": None}, {"status": {"overall": {}}}, {"data_view": {"id": 2}}, {"id": False}]
)
def test_invalid_kibana_response(value: object) -> None:
    with pytest.raises(ValueError, match="ELK"):
        elk_api._kibana_response(value)


@pytest.mark.parametrize(
    "value",
    [
        None,
        {"ips": {"es1": None}},
        {"expected_services": None},
        {"log_hosts": [1]},
        {"log_expectations": {"app": "nginx"}},
        {"ip": None},
        {"kibana_port": True},
    ],
)
def test_invalid_guest_settings(value: object) -> None:
    with pytest.raises(ValueError, match="ELK"):
        elk_api._guest_settings(value)


def test_valid_elasticsearch_shapes_keep_unknown_fields() -> None:
    value = {
        "status": "green",
        "cluster_uuid": "local",
        "number_of_nodes": 3,
        "count": 0,
        "found": True,
        "_source": {"message": "synthetic"},
        "hits": {"hits": [{"_id": "1"}]},
        "aggregations": {"services": {"buckets": [{"key": "nginx", "doc_count": 3}]}},
        "nodes": {"id": {"name": "es1", "unknown": []}},
        "unknown": None,
    }
    assert elk_api._elasticsearch_response(value) is value
    assert elk_api._elasticsearch_response({}) == {}
    assert elk_api._rows([]) == []


def test_valid_guest_settings_and_kibana_shapes() -> None:
    settings = {
        "ips": {"es1": "10.1.0.31"},
        "secrets": {"elastic": "synthetic-only"},
        "ip": "10.1.0.10",
        "kibana_port": 5601,
        "expected_services": ["nginx"],
        "log_hosts": ["app"],
        "log_expectations": {"app": ["nginx"]},
        "unknown": None,
    }
    assert elk_api._guest_settings(settings) is settings
    assert elk_api._guest_settings({}) == {}
    response = {"status": {"overall": {"level": "available"}}, "data_view": {"id": "view"}, "id": "dashboard"}
    assert elk_api._kibana_response(response) is response
    assert elk_api._kibana_response({}) == {}
