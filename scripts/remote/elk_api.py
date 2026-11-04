"""ES API setup and evidence collection over verified TLS."""

import base64
import datetime
import json
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import TypedDict, cast


class GuestSettings(TypedDict, total=False):
    ips: dict[str, str]
    secrets: dict[str, str]
    ip: str
    kibana_port: int
    expected_services: list[str]
    log_hosts: list[str]
    log_expectations: dict[str, list[str]]


class Bucket(TypedDict):
    key: str


class Aggregation(TypedDict):
    buckets: list[Bucket]


class SearchHits(TypedDict):
    hits: list[dict[str, object]]


class ElasticsearchNode(TypedDict):
    name: str


class ElasticsearchResponse(TypedDict, total=False):
    status: str
    number_of_nodes: int
    cluster_uuid: str
    count: int
    found: bool
    aggregations: dict[str, Aggregation]
    hits: SearchHits
    _source: dict[str, object]
    nodes: dict[str, ElasticsearchNode]


class KibanaOverall(TypedDict):
    level: str


class KibanaStatus(TypedDict):
    overall: KibanaOverall


class KibanaDataView(TypedDict):
    id: str


class KibanaResponse(TypedDict, total=False):
    status: KibanaStatus
    data_view: KibanaDataView
    id: str


def _document(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("Invalid ELK response object")
    return cast(dict[str, object], value)


def _rows(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise ValueError("Invalid ELK response collection")
    return [_document(item) for item in value]


def _strings(value: object) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("Invalid ELK string collection")


def _text(value: object) -> None:
    if not isinstance(value, str):
        raise ValueError("Invalid ELK text field")


def _aggregations(value: object) -> None:
    for aggregation in _document(value).values():
        for bucket in _rows(_document(aggregation).get("buckets")):
            _text(bucket.get("key"))


def _elasticsearch_response(value: object) -> ElasticsearchResponse:
    result = _document(value)
    for key in ("status", "cluster_uuid"):
        if key in result:
            _text(result[key])
    for key in ("count", "number_of_nodes"):
        counter = result.get(key)
        if key in result and (type(counter) is not int or counter < 0):
            raise ValueError("Invalid ELK counter")
    if "found" in result and type(result["found"]) is not bool:
        raise ValueError("Invalid ELK document flag")
    _elasticsearch_nested(result)
    return cast(ElasticsearchResponse, result)


def _elasticsearch_nested(result: dict[str, object]) -> None:
    if "aggregations" in result:
        _aggregations(result["aggregations"])
    if "hits" in result:
        _rows(_document(result["hits"]).get("hits"))
    if "_source" in result:
        _document(result["_source"])
    if "nodes" in result:
        for node in _document(result["nodes"]).values():
            _text(_document(node).get("name"))


def _kibana_response(value: object) -> KibanaResponse:
    result = _document(value)
    if "status" in result:
        overall = _document(_document(result["status"]).get("overall"))
        _text(overall.get("level"))
    if "data_view" in result:
        _text(_document(result["data_view"]).get("id"))
    if "id" in result:
        _text(result["id"])
    return cast(KibanaResponse, result)


def _guest_settings(value: object) -> GuestSettings:
    result = _document(value)
    for key in ("ips", "secrets"):
        if key in result:
            for item in _document(result[key]).values():
                _text(item)
    for key in ("expected_services", "log_hosts"):
        if key in result:
            _strings(result[key])
    if "log_expectations" in result:
        for sources in _document(result["log_expectations"]).values():
            _strings(sources)
    if "ip" in result:
        _text(result["ip"])
    if "kibana_port" in result and type(result["kibana_port"]) is not int:
        raise ValueError("Invalid ELK port")
    return cast(GuestSettings, result)


BASE = Path("/home/otus/dz07")
C: GuestSettings = {}
S: dict[str, str] = {}
CTX: ssl.SSLContext | None = None


def api(path: str, payload: object = None, method: str | None = None) -> object:
    auth = base64.b64encode(("elastic:" + S["elastic"]).encode()).decode()
    last: OSError | None = None
    for ip in C["ips"].values():
        request = urllib.request.Request(
            "https://" + ip + ":9200" + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Authorization": "Basic " + auth, "Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, context=CTX, timeout=8) as response:
                result: object = json.load(response)
                return result
        except urllib.error.HTTPError:
            raise
        except OSError as error:
            last = error
    if last is None:
        raise RuntimeError("No Elasticsearch endpoint available")
    raise last


def _data(path: str, payload: object = None, method: str | None = None) -> ElasticsearchResponse:
    return _elasticsearch_response(api(path, payload, method))


def _master_record() -> dict[str, object]:
    return _rows(api("/_cat/master?format=json"))[0]


def wait_health(nodes: int | None = None, green: bool = True) -> ElasticsearchResponse:
    if nodes is None:
        nodes = len(C["ips"])
    for _ in range(120):
        try:
            health = _data("/_cluster/health")
            if health["number_of_nodes"] == nodes and (not green or health["status"] == "green"):
                return health
        except OSError:
            pass
        time.sleep(3)
    raise RuntimeError("Cluster health timeout")


def setup() -> None:
    wait_health()
    _data("/_security/user/elastic/_password", {"password": S["elastic"]}, "POST")
    _data("/_security/user/kibana_system/_password", {"password": S["kibana"]}, "POST")
    _data(
        "/_security/role/otus_shipper",
        {
            "cluster": ["monitor"],
            "indices": [{"names": ["logs-otus-*"], "privileges": ["auto_configure", "create_doc"]}],
        },
        "PUT",
    )
    _data("/_security/user/otus_shipper", {"password": S["shipper"], "roles": ["otus_shipper"]}, "PUT")
    _data(
        "/_ingest/pipeline/otus-logs",
        {
            "description": "OTUS service classification and nginx parsing",
            "processors": [
                {"set": {"field": "event.dataset", "value": "{{{service.name}}}"}},
                {
                    "grok": {
                        "if": "ctx.service?.name == 'nginx' && ctx.log?.file?.path?.endsWith('access.log') == true",
                        "field": "message",
                        "patterns": [
                            '%{IPORHOST:source.address} - %{DATA:user.name} \\[%{HTTPDATE:nginx.time}\\] "%{WORD:http.request.method} %{DATA:url.original} HTTP/%{NUMBER:http.version}" %{INT:http.response.status_code:int} %{INT:http.response.body.bytes:long} %{GREEDYDATA:nginx.tail}'
                        ],
                        "on_failure": [
                            {"set": {"field": "error.message", "value": "{{{_ingest.on_failure_message}}}"}}
                        ],
                    }
                },
            ],
        },
        "PUT",
    )
    _data(
        "/_ilm/policy/otus-logs",
        {"policy": {"phases": {"hot": {"actions": {}}, "delete": {"min_age": "7d", "actions": {"delete": {}}}}}},
        "PUT",
    )
    _data(
        "/_index_template/otus-logs",
        {
            "index_patterns": ["logs-otus-*"],
            "priority": 500,
            "template": {
                "settings": {"number_of_shards": 1, "number_of_replicas": 1, "index.lifecycle.name": "otus-logs"},
                "mappings": {
                    "properties": {
                        "@timestamp": {"type": "date"},
                        "service": {"properties": {"name": {"type": "keyword"}}},
                        "event": {"properties": {"dataset": {"type": "keyword"}}},
                        "host": {"properties": {"name": {"type": "keyword"}}},
                    }
                },
            },
        },
        "PUT",
    )
    print(json.dumps({"cluster_uuid": _data("/")["cluster_uuid"], "health": wait_health()}))


def source_counts() -> dict[str, int]:
    result = {}
    for node, sources in C["log_expectations"].items():
        for service in sources:
            query = {
                "bool": {"filter": [{"term": {"host.name": "otus-dz07-" + node}}, {"term": {"service.name": service}}]}
            }
            result[node + "/" + service] = _data("/logs-otus-*/_count", {"query": query}, "POST")["count"]
    return result


def verify() -> None:
    for _ in range(90):
        try:
            result = _data(
                "/logs-otus-*/_search",
                {
                    "size": 0,
                    "aggs": {
                        "services": {"terms": {"field": "service.name", "size": 20}},
                        "hosts": {"terms": {"field": "host.name", "size": max(20, len(C["ips"]))}},
                    },
                },
                "POST",
            )
            services = {x["key"] for x in result["aggregations"]["services"]["buckets"]}
            hosts = {x["key"] for x in result["aggregations"]["hosts"]["buckets"]}
            sources = source_counts()
            if (
                set(C["expected_services"]) <= services
                and {"otus-dz07-" + name for name in C["log_hosts"]} <= hosts
                and all(sources.values())
            ):
                break
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
        time.sleep(3)
    else:
        raise RuntimeError("Missing log sources")
    samples = {}
    for service in sorted(services):
        samples[service] = _data(
            "/logs-otus-*/_search",
            {"size": 2, "query": {"term": {"service.name": service}}, "sort": [{"@timestamp": "desc"}]},
            "POST",
        )["hits"]["hits"]
    parsed = _data("/logs-otus-*/_count", {"query": {"exists": {"field": "http.response.status_code"}}}, "POST")
    if not parsed["count"]:
        raise RuntimeError("nginx pipeline has not produced parsed HTTP status")
    print(
        json.dumps(
            {
                "checked_at_utc": datetime.datetime.now(datetime.UTC).isoformat(),
                "health": wait_health(),
                "cluster_uuid": _data("/")["cluster_uuid"],
                "counts": result,
                "source_counts": sources,
                "parsed_nginx_count": parsed["count"],
                "samples": samples,
            },
            ensure_ascii=False,
        )
    )


def master() -> None:
    _data(
        "/logs-otus-failover/_doc/before?refresh=wait_for&wait_for_active_shards=all",
        {
            "@timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
            "message": "OTUS_DZ07 committed before master shutdown",
            "service": {"name": "verification"},
        },
        "PUT",
    )
    print(json.dumps(_master_record()))


def failure() -> None:
    health = wait_health(nodes=len(C["ips"]) - 1, green=False)
    before = _data("/logs-otus-failover/_doc/before")
    if not before["found"] or before["_source"]["message"] != "OTUS_DZ07 committed before master shutdown":
        raise RuntimeError("Committed pre-failure document lost")
    _data(
        "/logs-otus-failover/_doc/probe?refresh=wait_for",
        {
            "@timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
            "message": "OTUS_DZ07 write with one ES node offline",
            "service": {"name": "verification"},
        },
        "PUT",
    )
    document = _data("/logs-otus-failover/_doc/probe")
    if not document["found"]:
        raise RuntimeError("Failover probe lost")
    print(json.dumps({"health": health, "before": before, "probe": document, "new_master": _master_record()}))


def recovery() -> None:
    print(
        json.dumps(
            {
                "health": wait_health(),
                "before": _data("/logs-otus-failover/_doc/before"),
                "probe": _data("/logs-otus-failover/_doc/probe"),
            }
        )
    )


def kibana() -> None:
    auth = base64.b64encode(("elastic:" + S["elastic"]).encode()).decode()

    def call(path: str, payload: object = None, method: str | None = None) -> object:
        request = urllib.request.Request(
            "http://" + C["ip"] + ":" + str(C["kibana_port"]) + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Authorization": "Basic " + auth, "Content-Type": "application/json", "kbn-xsrf": "true"},
            method=method,
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            result: object = json.load(response)
            return result

    for _ in range(120):
        try:
            status = _kibana_response(call("/api/status"))
            if status["status"]["overall"]["level"] == "available":
                break
        except OSError:
            pass
        time.sleep(3)
    else:
        raise RuntimeError("Kibana not ready")
    view = _kibana_response(
        call(
            "/api/data_views/data_view",
            {"data_view": {"id": "otus-logs", "title": "logs-otus-*", "timeFieldName": "@timestamp"}, "override": True},
            "POST",
        )
    )
    _kibana_response(call("/api/kibana/settings", {"changes": {"defaultIndex": "otus-logs"}}, "POST"))
    _kibana_response(
        call(
            "/api/saved_objects/search/otus-logs?overwrite=true",
            {
                "attributes": {
                    "title": "OTUS — централизованные логи",
                    "columns": ["host.name", "service.name", "message"],
                    "sort": [["@timestamp", "desc"]],
                    "kibanaSavedObjectMeta": {
                        "searchSourceJSON": json.dumps(
                            {
                                "indexRefName": "kibanaSavedObjectMeta.searchSourceJSON.index",
                                "query": {"language": "kuery", "query": ""},
                                "filter": [],
                            }
                        )
                    },
                },
                "references": [
                    {"name": "kibanaSavedObjectMeta.searchSourceJSON.index", "type": "index-pattern", "id": "otus-logs"}
                ],
            },
            "POST",
        )
    )
    dashboard = _kibana_response(
        call(
            "/api/saved_objects/dashboard/otus-logs?overwrite=true",
            {
                "attributes": {
                    "title": "OTUS DZ07 — журналы серверов",
                    "timeRestore": False,
                    "optionsJSON": json.dumps({"useMargins": True}),
                    "panelsJSON": json.dumps(
                        [
                            {
                                "panelIndex": "1",
                                "type": "search",
                                "panelRefName": "panel_1",
                                "embeddableConfig": {},
                                "gridData": {"x": 0, "y": 0, "w": 48, "h": 20, "i": "1"},
                            }
                        ]
                    ),
                },
                "references": [{"name": "panel_1", "type": "search", "id": "otus-logs"}],
            },
            "POST",
        )
    )
    print(
        json.dumps(
            {
                "status": status["status"]["overall"],
                "data_view_id": view["data_view"]["id"],
                "dashboard_id": dashboard["id"],
            }
        )
    )


def health() -> None:
    print(json.dumps(wait_health()))


def kibana_check() -> None:
    auth = base64.b64encode(("elastic:" + S["elastic"]).encode()).decode()
    results: dict[str, KibanaResponse] = {}
    for name, path in [
        ("status", "/api/status"),
        ("data_view", "/api/data_views/data_view/otus-logs"),
        ("dashboard", "/api/saved_objects/dashboard/otus-logs"),
    ]:
        request = urllib.request.Request(
            "http://" + C["ip"] + ":" + str(C["kibana_port"]) + path,
            headers={"Authorization": "Basic " + auth, "kbn-xsrf": "true"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            results[name] = _kibana_response(json.load(response))
    if results["status"]["status"]["overall"]["level"] != "available":
        raise RuntimeError("Kibana is not available")
    print(
        json.dumps(
            {
                "status": "available",
                "data_view": results["data_view"]["data_view"]["id"],
                "dashboard": results["dashboard"]["id"],
            }
        )
    )


def checkpoint() -> None:
    import secrets

    path = BASE / "scaling-checkpoint.json"
    if path.exists():
        before = json.loads(path.read_text())
        if (
            _data("/")["cluster_uuid"] != before["cluster_uuid"]
            or _data("/otus-scaling-check/_doc/probe")["_source"]["nonce"] != before["nonce"]
        ):
            raise RuntimeError("Saved Elasticsearch checkpoint no longer matches live data")
        print(json.dumps({"checkpoint_reused": True, "nodes_before": len(before["nodes"])}))
        return
    nonce = secrets.token_hex(24)
    _data("/otus-scaling-check/_doc/probe?refresh=wait_for&wait_for_active_shards=all", {"nonce": nonce}, "PUT")
    result = {
        "cluster_uuid": _data("/")["cluster_uuid"],
        "nodes": {key: node["name"] for key, node in _data("/_nodes")["nodes"].items()},
        "nonce": nonce,
    }
    path.write_text(json.dumps(result))
    print(json.dumps({"checkpoint_written": True, "nodes_before": len(result["nodes"])}))


def preservation() -> None:
    before = json.loads((BASE / "scaling-checkpoint.json").read_text())
    current = _data("/_nodes")["nodes"]
    if _data("/")["cluster_uuid"] != before["cluster_uuid"]:
        raise RuntimeError("Cluster UUID changed")
    if any(key not in current or current[key]["name"] != name for key, name in before["nodes"].items()):
        raise RuntimeError("An existing Elasticsearch node was replaced")
    if {node["name"] for node in current.values()} != set(C["ips"]):
        raise RuntimeError("Unexpected cluster membership")
    document = _data("/otus-scaling-check/_doc/probe")
    if not document.get("found") or document["_source"]["nonce"] != before["nonce"]:
        raise RuntimeError("Committed document was lost")
    _data(
        "/otus-scaling-check/_doc/after?refresh=wait_for&wait_for_active_shards=all", {"nonce": before["nonce"]}, "PUT"
    )
    if _data("/otus-scaling-check/_doc/after")["_source"]["nonce"] != before["nonce"]:
        raise RuntimeError("Write/read after configuration failed")
    print(
        json.dumps(
            {
                "cluster_uuid_preserved": True,
                "existing_node_ids_preserved": True,
                "committed_document_preserved": True,
                "health": wait_health(),
            }
        )
    )


if __name__ == "__main__":
    C = _guest_settings(json.loads((BASE / "elk-settings.json").read_text()))
    S = C["secrets"]
    CTX = ssl.create_default_context(cafile=str(BASE / "certs/ca.crt"))
    {
        "setup": setup,
        "verify": verify,
        "master": master,
        "failure": failure,
        "recovery": recovery,
        "kibana": kibana,
        "health": health,
        "checkpoint": checkpoint,
        "preservation": preservation,
        "kibana-check": kibana_check,
    }[sys.argv[1]]()
