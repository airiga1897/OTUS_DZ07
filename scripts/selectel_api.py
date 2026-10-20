"""Small HTTPS-only Keystone/Nova client; credentials and tokens stay in memory."""
import json
import urllib.parse
import urllib.request

from credentials import settings
import privacy


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
    with urllib.request.build_opener(NoRedirect).open(req, timeout=15) as response:
        raw = response.read()
        return (json.loads(raw) if raw else {}), response.headers


def authenticate(config):
    auth_url = settings()['auth_url']
    if config.get("auth_url", auth_url).rstrip("/") != auth_url:
        raise ValueError("Unexpected identity endpoint")
    body = {"auth": {"identity": {"methods": ["password"], "password": {"user": {
        "name": config["username"], "domain": {"name": config["domain_name"]},
        "password": config["password"]}}}, "scope": {"project": {"id": config["project_id"]}}}}
    data, headers = request_json(auth_url + "/auth/tokens", payload=body)
    identity = data["token"]
    if identity.get("project", {}).get("id") != config["project_id"]:
        raise ValueError("Unexpected project")
    token = headers["X-Subject-Token"]
    privacy.remember([token])
    return token, identity["catalog"]


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
