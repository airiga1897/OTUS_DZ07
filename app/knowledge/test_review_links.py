from django.test import TestCase

from .models import Article


class ReviewLinksTests(TestCase):
    def test_existing_article_opens_develop_without_rewriting_database(self) -> None:
        url = "https://github.com/airiga1897/OTUS_DZ07"
        article = Article.objects.create(
            title="Material", slug="review", lesson=6, body="Sources: " + url, published=True
        )
        response = self.client.get("/articles/review/")
        self.assertContains(response, 'href="' + url + '/tree/elkcluster"')
        article.refresh_from_db()
        self.assertEqual(article.body, "Sources: " + url)

    def test_explicit_branch_pr_and_other_links_are_preserved_and_html_escaped(self) -> None:
        body = (
            "https://github.com/airiga1897/OTUS_DZ05/pull/1\n"
            "https://github.com/airiga1897/OTUS_DZ07/tree/custom\n"
            "https://example.org/project\n<script>unsafe</script>"
        )
        Article.objects.create(title="Material", slug="explicit-links", lesson=6, body=body, published=True)
        response = self.client.get("/articles/explicit-links/")
        self.assertContains(response, 'href="https://github.com/airiga1897/OTUS_DZ05/pull/1"')
        self.assertContains(response, 'href="https://github.com/airiga1897/OTUS_DZ07/tree/custom"')
        self.assertContains(response, 'href="https://example.org/project"')
        self.assertContains(response, "&lt;script&gt;unsafe&lt;/script&gt;")
