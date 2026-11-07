"""Application-write probe uses the real admin view on an isolated Django test DB."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.test import TestCase

from .models import Article


class ApplicationProbeTests(TestCase):
    def module(self) -> ModuleType:
        path = Path(__file__).resolve().parents[2] / "scripts/remote/portal_probe.py"
        spec = spec_from_file_location("portal_probe_test", path)
        assert spec is not None and spec.loader is not None, "Probe source must be importable"
        module = module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_write_through_admin_preserves_existing_user_and_removes_temporary_login(self) -> None:
        user = get_user_model().objects.create_user("existing", password=None)
        record = self.module().create_article("expanded")
        article = Article.objects.get(pk=record["id"])
        self.assertEqual(article.body, record["body"])
        self.assertFalse(article.published)
        response = self.client.get("/")
        self.assertNotContains(response, article.title)
        self.assertEqual(self.client.get("/articles/" + article.slug + "/").status_code, 404)
        self.assertEqual(list(get_user_model().objects.values_list("pk", flat=True)), [user.pk])
        self.assertEqual(Session.objects.count(), 0)

    def test_failed_write_also_removes_temporary_login(self) -> None:
        with patch("django.test.Client.post") as post:
            post.return_value.status_code = 500
            with self.assertRaisesRegex(AssertionError, "admin write failed"):
                self.module().create_article("baseline")
        self.assertEqual(get_user_model().objects.count(), 0)
        self.assertEqual(Session.objects.count(), 0)

    def test_legacy_probes_are_unpublished_without_changing_materials_or_user_rows(self) -> None:
        from importlib import import_module
        from types import SimpleNamespace

        from django.apps import apps
        from django.db import connection

        from .services.initial_data import load_initial_data

        load_initial_data()
        original = list(Article.objects.values())
        slug = "baseline-" + "a" * 32
        probe = Article.objects.create(
            title=slug, slug=slug, lesson=6, summary="proof", body="committed-" + "b" * 32, published=True
        )
        custom = Article.objects.create(
            title="User article", slug="user", lesson=6, summary="proof", body=probe.body, published=True
        )
        migrate = import_module("knowledge.migrations.0003_unpublish_verification_articles").unpublish_probes
        for _ in range(2):
            migrate(apps, SimpleNamespace(connection=connection))
        probe.refresh_from_db()
        self.assertFalse(probe.published)
        self.assertEqual(probe.body, "committed-" + "b" * 32)
        self.assertTrue(Article.objects.get(pk=custom.pk).published)
        self.assertEqual(list(Article.objects.filter(pk__in=[row["id"] for row in original]).values()), original)
