"""Run the existing application write view against local PostgreSQL, with no public test API."""
import json
import os
from pathlib import Path
import sys

import uuid


def create_article(mode: str) -> dict:
    from django.contrib.auth import get_user_model
    from django.test import Client
    from knowledge.models import Article
    token = uuid.uuid4().hex
    user = get_user_model().objects.create_user(username='probe-' + token, is_staff=True, is_superuser=True)
    client = Client(HTTP_HOST='localhost')
    try:
        client.force_login(user)
        slug, body = mode + '-' + token, 'committed-' + uuid.uuid4().hex
        response = client.post('/admin/knowledge/article/add/', {
            'title': slug, 'slug': slug, 'lesson': 6, 'summary': 'proof',
            'body': body, '_save': 'Save'})
        if response.status_code != 302:
            raise AssertionError('Django admin write failed')
        article = Article.objects.get(slug=slug)
        return {'id': article.pk, 'slug': article.slug, 'body': article.body}
    finally:
        client.logout()
        user.delete()


def main() -> None:
    from initial_data import application_environment
    from runtime_config import BASE, APP
    passwords = json.loads((BASE / 'settings.json').read_text())['passwords']
    os.environ.update(application_environment(passwords))
    os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings'
    sys.path.insert(0, str(APP / 'app'))
    import django
    django.setup()
    print(json.dumps(create_article(sys.argv[1])))


if __name__ == '__main__':
    main()
