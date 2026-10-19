import tempfile
from pathlib import Path
from django.core.management import call_command
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from .models import Article

class KnowledgeTests(TestCase):
    def setUp(self) -> None:
        Article.objects.create(title="ДЗ", slug="published", lesson=1, summary="Тест", body="<script>x</script>", published=True)
        Article.objects.create(title="Черновик", slug="draft", lesson=2, summary="Скрыто", body="Тест")

    def test_public_pages_hide_drafts_and_escape_content(self) -> None:
        response = self.client.get("/")
        self.assertContains(response, "ДЗ")
        self.assertNotContains(response, "Черновик")
        self.assertEqual(self.client.get("/articles/draft/").status_code, 404)
        self.assertContains(self.client.get("/articles/published/"), "&lt;script&gt;")

    def test_seed_preserves_editor_changes(self) -> None:
        call_command("seed_lessons", verbosity=0)
        Article.objects.filter(slug="high-load").update(title="Правка редактора")
        call_command("seed_lessons", verbosity=0)
        self.assertEqual(Article.objects.get(slug="high-load").title, "Правка редактора")
        self.assertEqual(Article.objects.count(), 6)

    def test_admin_requires_login(self) -> None:
        self.assertEqual(self.client.get("/admin/knowledge/article/add/").status_code, 302)

    def test_admin_can_upload_image(self) -> None:
        import io
        from PIL import Image
        user = get_user_model().objects.create_superuser("editor", "editor@example.invalid", "test-password")
        self.assertTrue(self.client.login(username=user.username, password="test-password"))
        buffer = io.BytesIO()
        Image.new("RGB", (4, 4), "blue").save(buffer, format="PNG")
        with tempfile.TemporaryDirectory() as root:
            with override_settings(MEDIA_ROOT=root):
                response = self.client.post("/admin/knowledge/article/add/", {
                    "title": "Файл", "slug": "uploaded", "lesson": 4, "summary": "Картинка", "body": "Тест",
                    "published": "on", "image": SimpleUploadedFile("test.png", buffer.getvalue(), content_type="image/png"), "_save": "Сохранить"})
                self.assertEqual(response.status_code, 302)
                article = Article.objects.get(slug="uploaded")
                self.assertTrue(Path(article.image.path).exists())

    def test_readiness_requires_storage_and_live_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            with override_settings(STATIC_ROOT=Path(root)/"static", REQUIRE_SHARED_MOUNT=False):
                self.assertEqual(self.client.get("/health/live").status_code, 200)
                self.assertEqual(self.client.get("/health/ready").status_code, 503)
                (Path(root)/".ready").write_text("otus-dz04")
                self.assertEqual(self.client.get("/health/ready").status_code, 200)
            with override_settings(STATIC_ROOT=Path(root)/"static", REQUIRE_SHARED_MOUNT=True):
                self.assertEqual(self.client.get("/health/ready").status_code, 503)
