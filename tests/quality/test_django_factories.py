"""Real Django ORM data and the once-only production fixture contract."""

import pytest
from django.test import Client
from factories import ArticleFactory
from knowledge.services.initial_data import load_initial_data


@pytest.mark.django_db
def test_initial_data_does_not_replace_factory_created_user_material() -> None:
    article = ArticleFactory.create(body="User controlled synthetic text")
    first = load_initial_data()
    second = load_initial_data()
    article.refresh_from_db()
    assert first.status == second.status == "preserved_existing"
    assert not first.loaded_now
    assert not second.loaded_now
    assert article.body == "User controlled synthetic text"


@pytest.mark.django_db
def test_factory_records_have_independent_identifiers_and_are_visible(client: Client) -> None:
    first, second = ArticleFactory.create_batch(2)
    assert first.pk != second.pk
    assert first.slug != second.slug
    assert client.get("/").status_code == 200
