"""Exercise the real HTTPS client; only its transport is synthetic."""

import http.client
import io
import json
import ssl
import unittest
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from unittest.mock import Mock, patch

import pool_selection
import selectel_api as api
from selectel_global_router import GlobalRouter


class SyntheticResponse(io.BytesIO):
    def __init__(self, value: object) -> None:
        super().__init__(json.dumps(value).encode() if value is not None else b"")
        self.headers = {"X-Test": "present"}


def response(value: object) -> SyntheticResponse:
    return SyntheticResponse(value)


class SelectelHttpTests(unittest.TestCase):
    def test_invalid_transport_settings_fail_before_network(self) -> None:
        valid = {"read_attempts": 3, "timeout_seconds": 15, "retry_delay_seconds": 2}
        cases: list[tuple[str, Sequence[object]]] = [
            ("read_attempts", [0, 6, True]),
            ("timeout_seconds", [0, 61, "15"]),
            ("retry_delay_seconds", [-1, 11, None]),
        ]
        for key, values in cases:
            for value in values:
                document = {"api": {**valid, key: value}}
                with (
                    patch("pathlib.Path.read_text", return_value=json.dumps(document)),
                    self.assertRaises(ValueError),
                ):
                    api.transport_policy()

    def invoke(
        self, outcomes: Sequence[object], payload: object = None
    ) -> tuple[tuple[object, api.ResponseHeaders], Mock]:
        opener = Mock()
        opener.open.side_effect = outcomes
        with patch("urllib.request.build_opener", return_value=opener), patch("time.sleep"):
            result = api.request_json("https://example.invalid/resources", token="synthetic-token", payload=payload)
        return result, opener

    def test_successful_get_empty_response_and_post_contract(self) -> None:
        for payload, value in [(None, {"ok": True}), ({"action": None}, None)]:
            result, opener = self.invoke([response(value)], payload)
            self.assertEqual(result[0], value or {})
            self.assertEqual(result[1]["X-Test"], "present")
            request = opener.open.call_args.args[0]
            self.assertEqual(request.get_method(), "GET" if payload is None else "POST")
            self.assertEqual(request.get_header("X-auth-token"), "synthetic-token")
            if payload is not None:
                self.assertEqual(json.loads(request.data), payload)

    def test_invalid_or_credentialed_endpoints_never_reach_transport(self) -> None:
        for url in (
            "http://example.invalid",
            "https:///missing",
            "https://user:password@example.invalid",
            "file:///tmp/x",
        ):
            with self.subTest(url=url), patch("urllib.request.build_opener") as opener:
                with self.assertRaises(ValueError):
                    api.request_json(url)
                opener.assert_not_called()

    def test_read_retries_cover_connect_read_and_truncated_responses(self) -> None:
        errors = [
            TimeoutError(),
            ConnectionResetError(),
            urllib.error.URLError("temporary"),
            http.client.IncompleteRead(b"partial"),
            urllib.error.HTTPError("https://example.invalid", 503, "Unavailable", http.client.HTTPMessage(), None),
            urllib.error.HTTPError("https://example.invalid", 429, "Limited", http.client.HTTPMessage(), None),
        ]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                result, opener = self.invoke([error, response({"ok": True})])
                assert isinstance(result[0], dict)
                self.assertTrue(result[0]["ok"])
                self.assertEqual(opener.open.call_count, 2)
        failing = Mock()
        failing.__enter__ = Mock(return_value=failing)
        failing.__exit__ = Mock(return_value=False)
        failing.read.side_effect = TimeoutError("read timeout")
        result, opener = self.invoke([failing, response({"complete": True})])
        assert isinstance(result[0], dict)
        self.assertTrue(result[0]["complete"])
        failing.__exit__.assert_called_once()

    def test_exhausted_reads_stop_after_bounded_attempts(self) -> None:
        opener = Mock()
        opener.open.side_effect = TimeoutError()
        with (
            patch("urllib.request.build_opener", return_value=opener),
            patch("time.sleep") as sleep,
            self.assertRaises(TimeoutError) as caught,
        ):
            api.request_json("https://example.invalid")
        self.assertEqual(opener.open.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        context = getattr(caught.exception, "api_error_context", None)
        assert isinstance(context, dict)
        self.assertEqual(context["attempts"], 3)
        self.assertEqual(context["host"], "example.invalid")

    def test_post_permission_redirect_and_certificate_failures_are_not_retried(self) -> None:
        certificate = ssl.SSLCertVerificationError("synthetic certificate failure")
        errors: list[tuple[object, Exception]] = [(None, certificate), (None, urllib.error.URLError(certificate))]
        errors += [
            (
                None,
                urllib.error.HTTPError("https://example.invalid", status, "Rejected", http.client.HTTPMessage(), None),
            )
            for status in (302, 400, 401, 403, 404)
        ]
        errors += [
            ({"action": None}, error)
            for error in (
                TimeoutError(),
                http.client.IncompleteRead(b""),
                urllib.error.HTTPError("https://example.invalid", 503, "Unavailable", http.client.HTTPMessage(), None),
            )
        ]
        for payload, error in errors:
            with self.subTest(payload=payload, error=type(error).__name__):
                opener = Mock()
                opener.open.side_effect = error
                with (
                    patch("urllib.request.build_opener", return_value=opener),
                    patch("time.sleep") as sleep,
                    self.assertRaises(type(error)),
                ):
                    api.request_json("https://example.invalid", payload=payload)
                self.assertEqual(opener.open.call_count, 1)
                sleep.assert_not_called()

    def test_invalid_json_is_not_hidden_by_retry_or_partial_parsing(self) -> None:
        opener = Mock()
        opener.open.return_value = io.BytesIO(b'log\n{"ok":true}')
        with (
            patch("urllib.request.build_opener", return_value=opener),
            self.assertRaises(json.JSONDecodeError),
        ):
            api.request_json("https://example.invalid")
        self.assertEqual(opener.open.call_count, 1)
        handler: urllib.request.HTTPRedirectHandler = api.NoRedirect()
        self.assertIsNone(
            handler.redirect_request(
                urllib.request.Request("https://source.invalid"),
                io.BytesIO(),
                302,
                "Found",
                http.client.HTTPMessage(),
                "https://foreign.invalid",
            )
        )

    def test_catalog_and_global_router_use_same_retry_policy(self) -> None:
        consumers: list[tuple[Callable[[], object], object]] = [
            (lambda: pool_selection.image_catalog("https://example.invalid/v2", "synthetic"), {"images": []}),
            (self.global_read, []),
        ]
        for consumer, payload in consumers:
            opener = Mock()
            opener.open.side_effect = [TimeoutError(), response(payload)]
            with patch("urllib.request.build_opener", return_value=opener), patch("time.sleep"):
                self.assertEqual(consumer(), payload.get("images") if isinstance(payload, dict) else payload)
            self.assertEqual(opener.open.call_count, 2)

    def global_read(self) -> object:
        client = GlobalRouter.__new__(GlobalRouter)
        client.token = "synthetic"
        return client.get("/networks")

    def test_authentication_scope_and_nova_endpoint_are_validated(self) -> None:
        config = {
            "auth_url": "https://identity.invalid/v3",
            "username": "synthetic",
            "password": "synthetic",
            "domain_name": "synthetic",
            "project_id": "own-project",
            "region": "ru-3",
        }
        catalog = [
            {"type": "image", "endpoints": []},
            {
                "type": "compute",
                "endpoints": [
                    {"interface": "private", "region": "ru-3", "url": "https://private.invalid"},
                    {"interface": "public", "region": "ru-9", "url": "https://other.invalid"},
                    {"interface": "public", "region": "ru-3", "url": "http://insecure.invalid"},
                    {"interface": "public", "region": "ru-3", "url": "https://compute.invalid/"},
                ],
            },
        ]
        reply = (
            {"token": {"project": {"id": "own-project"}, "catalog": catalog}},
            {"X-Subject-Token": "synthetic-token"},
        )
        with (
            patch.object(api, "auth_schema", return_value={"auth_url": config["auth_url"]}),
            patch.object(api, "request_json", return_value=reply) as request,
        ):
            client = api.NovaClient(config)
            self.assertEqual(client.endpoint, "https://compute.invalid")
            self.assertEqual(request.call_args.kwargs["payload"]["auth"]["scope"], {"project": {"id": "own-project"}})
            with self.assertRaises(ValueError):
                api.authenticate({**config, "auth_url": "https://foreign.invalid"})
            request.return_value = ({"token": {"project": {"id": "foreign"}}}, {})
            with self.assertRaises(ValueError):
                api.authenticate(config)
        with patch.object(api, "authenticate", return_value=("synthetic", [])), self.assertRaises(ValueError):
            api.NovaClient(config)
        with patch.object(api, "request_json", return_value=({"ok": True}, {})) as request:
            self.assertTrue(client.request("/servers/owned")["ok"])
            for path in ("/volumes/x", "/servers/../foreign", "/servers/x?token=y"):
                with self.assertRaises(ValueError):
                    client.request(path)
            self.assertEqual(request.call_count, 1)
