"""Small HTTPS-only Keystone/Nova client; credentials and tokens stay in memory."""
import json
import os
import urllib.parse
import urllib.request
import urllib.error
import http.client
from pathlib import Path
import ssl
import time

from credentials import auth_settings, config_from_env


def transport_policy():
    policy = json.loads((Path(__file__).resolve().parents[1] / 'config/cloud.json').read_text())['api']
    for key, minimum, maximum in [('read_attempts', 1, 5), ('timeout_seconds', 1, 60), ('retry_delay_seconds', 0, 10)]:
        if type(policy.get(key)) is not int or not minimum <= policy[key] <= maximum:
            raise ValueError('Invalid Selectel API transport setting: ' + key)
    return policy


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward credentials or a token across redirects.
        return None


def request_json(url, *, token=None, payload=None):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("HTTPS endpoint required")
    headers = {"Accept": "application/json"}
    if token:
        headers["X-Auth-Token"] = token
    if payload is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, headers=headers,
        data=json.dumps(payload).encode() if payload is not None else None)
    policy = transport_policy()
    attempts = policy['read_attempts'] if payload is None else 1
    for attempt in range(attempts):
        try:
            with urllib.request.build_opener(NoRedirect).open(req, timeout=policy['timeout_seconds']) as response:
                raw = response.read()
                return (json.loads(raw) if raw else {}), response.headers
        except (OSError, http.client.IncompleteRead) as error:
            certificate_error = isinstance(error, ssl.SSLCertVerificationError) or isinstance(
                getattr(error, 'reason', None), ssl.SSLCertVerificationError)
            permanent_http = isinstance(error, urllib.error.HTTPError) and error.code < 500 and error.code != 429
            if attempt + 1 == attempts or certificate_error or permanent_http:
                error.api_error_context = {'method': req.get_method(), 'host': parsed.hostname,
                    'attempts': attempt + 1, 'http_status': getattr(error, 'code', None),
                    'cause_type': type(getattr(error, 'reason', error)).__name__}
                raise
            time.sleep(policy['retry_delay_seconds'])


def authenticate(config):
    auth_url = auth_settings()["auth_url"]
    if config.get("auth_url", auth_url).rstrip("/") != auth_url:
        raise ValueError("Unexpected identity endpoint")
    body = {"auth": {"identity": {"methods": ["password"], "password": {"user": {
        "name": config["username"], "domain": {"name": config["domain_name"]},
        "password": config["password"]}}}, "scope": {"project": {"id": config["project_id"]}}}}
    data, headers = request_json(auth_url + "/auth/tokens", payload=body)
    identity = data["token"]
    if identity.get("project", {}).get("id") != config["project_id"]:
        raise ValueError("Unexpected project")
    return headers["X-Subject-Token"], identity["catalog"]


class NovaClient:
    def __init__(self, config):
        self.token, catalog = authenticate(config)
        self.endpoint = None
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

    def request(self, path, payload=None):
        if not path.startswith("/servers/") or ".." in path or "?" in path:
            raise ValueError("Unexpected Nova path")
        data, _ = request_json(self.endpoint + path, token=self.token, payload=payload)
        return data
