"""Synthetic test-only data, independent of the bundled initial-data fixture."""

import factory
from knowledge.models import Article


class ArticleFactory(factory.django.DjangoModelFactory[Article]):
    class Meta:
        model = Article

    title = factory.Sequence(lambda number: f"Test article {number}")
    slug = factory.Sequence(lambda number: f"test-article-{number}")
    lesson = 7
    summary = "Synthetic test material"
    body = "Synthetic test body"
    published = True
