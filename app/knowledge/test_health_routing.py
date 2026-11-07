"""Exercise the configured HAProxy readiness request against the Django app."""

import re
import shlex
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from django.db import DatabaseError
from django.test import TestCase, override_settings

if TYPE_CHECKING:
    from django.test.client import _MonkeyPatchedWSGIResponse

ROOT = Path(__file__).resolve().parents[2]


@override_settings(ALLOWED_HOSTS=["localhost", "127.0.0.1", "192.0.2.10"], REQUIRE_SHARED_MOUNT=False)
class HealthRoutingTests(TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.storage = Path(self.directory.name)
        self.settings_override = override_settings(STATIC_ROOT=self.storage / "static")
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        (self.storage / ".ready").write_text("otus-dz04\n")

    def health_request(self) -> "_MonkeyPatchedWSGIResponse":
        config = (ROOT / "ansible/roles/haproxy/templates/haproxy.cfg.j2").read_text()
        sends = [line.strip() for line in config.splitlines() if line.strip().startswith("http-check send ")]
        self.assertEqual(len(sends), 1, "HAProxy must send an explicit readiness request with Host")
        tokens = shlex.split(sends[0])
        self.assertEqual(tokens[tokens.index("ver") + 1], "HTTP/1.1")
        self.assertEqual(tokens[tokens.index("hdr") + 1].lower(), "host")
        return self.client.generic(
            tokens[tokens.index("meth") + 1], tokens[tokens.index("uri") + 1], HTTP_HOST=tokens[tokens.index("hdr") + 2]
        )

    def test_old_hostless_probe_is_rejected_after_nginx_server_name_fallback(self) -> None:
        config = (ROOT / "ansible/roles/portal/templates/nginx.conf.j2").read_text()
        self.assertIn("proxy_set_header Host $host;", config)
        match = re.search(r"server_name\s+([^;\s]+)", config)
        assert match is not None, "Nginx configuration must declare server_name"
        fallback = match.group(1)
        self.assertEqual(self.client.get("/health/ready", HTTP_HOST=fallback).status_code, 400)

    def test_configured_probe_returns_ready_with_healthy_database_and_storage(self) -> None:
        response = self.health_request()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ready")

    def test_configured_probe_keeps_storage_and_database_failures_unhealthy(self) -> None:
        (self.storage / ".ready").unlink()
        self.assertEqual(self.health_request().status_code, 503)
        (self.storage / ".ready").write_text("otus-dz04\n")
        with patch("knowledge.views.connection.cursor", side_effect=DatabaseError("synthetic outage")):
            self.assertEqual(self.health_request().status_code, 503)

    def test_public_host_is_supported_and_foreign_hosts_still_rejected(self) -> None:
        self.assertEqual(self.client.get("/health/ready", HTTP_HOST="192.0.2.10").status_code, 200)
        self.assertEqual(self.client.get("/health/ready", HTTP_HOST="foreign.invalid").status_code, 400)
