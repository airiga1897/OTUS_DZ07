"""Small HTTPS-only Keystone/Nova client; credentials and tokens stay in memory."""

import http.client
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import NotRequired, Protocol, TypedDict, cast

from credentials import schema as auth_schema


class TransportPolicy(TypedDict):
    read_attempts: int
    timeout_seconds: int
    retry_delay_seconds: int


class APIErrorContext(TypedDict):
    method: str
    host: str
    attempts: int
    http_status: int | None
    cause_type: str


class ResponseHeaders(Protocol):
    def __getitem__(self, name: str, /) -> str | None: ...


class CatalogEndpoint(TypedDict):
    interface: str
    url: str
    region: NotRequired[str | None]


class CatalogService(TypedDict):
    type: str
    endpoints: list[CatalogEndpoint]


def subject_token(headers: ResponseHeaders) -> str:
    try:
        token = headers["X-Subject-Token"]
    except KeyError:
        token = None
    if not isinstance(token, str) or not token:
        raise ValueError("Identity token header unavailable")
    return token


def identity_scope(response: object) -> dict[str, object]:
    token = response.get("token") if isinstance(response, dict) else None
    if not isinstance(token, dict) or any(not isinstance(key, str) for key in token):
        raise ValueError("Invalid identity response")
    return cast(dict[str, object], token)


def json_object(value: object, message: str = "Unknown API response object") -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(message)
    return cast(dict[str, object], value)


def json_rows(value: object, message: str = "Unknown API collection") -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise ValueError(message)
    return [json_object(row, message) for row in value]


def json_text(value: object, message: str = "Unknown API text field") -> str:
    if not isinstance(value, str):
        raise ValueError(message)
    return value


def json_int(value: object, message: str = "Unknown API integer field") -> int:
    if type(value) is not int:
        raise ValueError(message)
    return value


def json_texts(value: object, message: str = "Unknown API text collection") -> list[str]:
    if not isinstance(value, list):
        raise ValueError(message)
    return [json_text(item, message) for item in value]


def json_object_or_empty(value: object, message: str = "Unknown API response object") -> dict[str, object]:
    if value is None:
        return {}
    return json_object(value, message)


def identifier_set(value: object, message: str = "Unknown API text field") -> set[str]:
    if not value:
        return set()
    return {json_text(value, message)}


def text_mapping(value: object, message: str = "Unknown API response object") -> dict[str, str]:
    return {key: json_text(item, message) for key, item in json_object(value, message).items()}


def json_contains(value: object, key: str, message: str = "Unknown API response object") -> bool:
    if value is None:
        return False
    return key in json_object(value, message)


def checked_catalog(value: object) -> list[CatalogService]:
    if not isinstance(value, list):
        raise ValueError("Invalid identity service catalog")
    for service in value:
        if (
            not isinstance(service, dict)
            or not isinstance(service.get("type"), str)
            or not isinstance(service.get("endpoints"), list)
        ):
            raise ValueError("Invalid identity service catalog")
        for endpoint in service["endpoints"]:
            if (
                not isinstance(endpoint, dict)
                or not isinstance(endpoint.get("interface"), str)
                or not isinstance(endpoint.get("url"), str)
                or (endpoint.get("region") is not None and not isinstance(endpoint["region"], str))
            ):
                raise ValueError("Invalid identity catalog endpoint")
    return cast(list[CatalogService], value)


def transport_policy() -> TransportPolicy:
    document = json.loads((Path(__file__).resolve().parents[1] / "config/cloud.json").read_text())
    policy = document.get("api") if isinstance(document, dict) else None
    if not isinstance(policy, dict):
        raise ValueError("Invalid Selectel API transport policy")
    for key, minimum, maximum in [("read_attempts", 1, 5), ("timeout_seconds", 1, 60), ("retry_delay_seconds", 0, 10)]:
        if type(policy.get(key)) is not int or not minimum <= policy[key] <= maximum:
            raise ValueError("Invalid Selectel API transport setting: " + key)
    return cast(TransportPolicy, policy)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: object, fp: object, code: int, msg: object, headers: object, newurl: str) -> None:
        # Do not forward credentials or a token across redirects.
        return None


def request_json(url: str, *, token: str | None = None, payload: object = None) -> tuple[object, ResponseHeaders]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("HTTPS endpoint required")
    headers = {"Accept": "application/json"}
    if token:
        headers["X-Auth-Token"] = token
    if payload is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        url, headers=headers, data=json.dumps(payload).encode() if payload is not None else None
    )
    policy = transport_policy()
    attempts = policy["read_attempts"] if payload is None else 1
    attempt = 0
    while True:
        try:
            with urllib.request.build_opener(NoRedirect).open(req, timeout=policy["timeout_seconds"]) as response:
                raw = response.read()
                return (json.loads(raw) if raw else {}), response.headers
        except (OSError, http.client.IncompleteRead) as error:
            certificate_error = isinstance(error, ssl.SSLCertVerificationError) or isinstance(
                getattr(error, "reason", None), ssl.SSLCertVerificationError
            )
            permanent_http = isinstance(error, urllib.error.HTTPError) and error.code < 500 and error.code != 429
            if attempt + 1 == attempts or certificate_error or permanent_http:
                context: APIErrorContext = {
                    "method": req.get_method(),
                    "host": parsed.hostname,
                    "attempts": attempt + 1,
                    "http_status": error.code if isinstance(error, urllib.error.HTTPError) else None,
                    "cause_type": type(getattr(error, "reason", error)).__name__,
                }
                error.__dict__["api_error_context"] = context
                raise
            time.sleep(policy["retry_delay_seconds"])
            attempt += 1


def authenticate(config: Mapping[str, str]) -> tuple[str, list[CatalogService]]:
    auth_url = auth_schema()["auth_url"]
    if config.get("auth_url", auth_url).rstrip("/") != auth_url:
        raise ValueError("Unexpected identity endpoint")
    body = {
        "auth": {
            "identity": {
                "methods": ["password"],
                "password": {
                    "user": {
                        "name": config["username"],
                        "domain": {"name": config["domain_name"]},
                        "password": config["password"],
                    }
                },
            },
            "scope": {"project": {"id": config["project_id"]}},
        }
    }
    data, headers = request_json(auth_url + "/auth/tokens", payload=body)
    identity = identity_scope(data)
    project = identity.get("project")
    if not isinstance(project, dict) or project.get("id") != config["project_id"]:
        raise ValueError("Unexpected project")
    return subject_token(headers), checked_catalog(identity.get("catalog"))


class NovaClient:
    def __init__(self, config: Mapping[str, str]) -> None:
        self.token, catalog = authenticate(config)
        self.endpoint = ""
        for service in catalog:
            if service["type"] != "compute":
                continue
            for endpoint in service["endpoints"]:
                if endpoint.get("interface") == "public" and endpoint.get("region") == config["region"]:
                    url = endpoint["url"].rstrip("/")
                    if urllib.parse.urlsplit(url).scheme == "https":
                        self.endpoint = url
                        break
        if not self.endpoint:
            raise ValueError("No HTTPS Nova endpoint in selected region")

    def request(self, path: str, payload: object = None) -> dict[str, object]:
        if not path.startswith("/servers/") or ".." in path or "?" in path:
            raise ValueError("Unexpected Nova path")
        data, _ = request_json(self.endpoint + path, token=self.token, payload=payload)
        return json_object(data, "Invalid Nova response object")
