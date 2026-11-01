"""Initial fixture contracts; local test database only."""
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.core import serializers
from django.core.management import call_command
from django.test import TestCase, override_settings

from .models import Article, InitialDataState
from .services.initial_data import fixture_path, load_initial_data


class InitialDataTests(TestCase):
    def test_homework_seven_is_visible_on_site_with_previous_materials(self) -> None:
        load_initial_data()
        for lesson in (5, 6, 7):
            article = Article.objects.get(lesson=lesson)
            self.assertContains(self.client.get('/'), article.title)
            self.assertContains(self.client.get('/articles/' + article.slug + '/'), article.body.splitlines()[0])

    def test_fixture_fields_primary_keys_and_next_id(self) -> None:
        result = load_initial_data()
        expected = json.loads(fixture_path().read_text(encoding='utf-8'))
        actual = json.loads(serializers.serialize('json', Article.objects.order_by('pk')))
        self.assertEqual(actual, expected)
        self.assertEqual(result.status, 'loaded')
        self.assertEqual([r['pk'] for r in actual], [1, 2, 3, 4, 5, 6, 7])
        article = Article.objects.create(title='Next', slug='next-id', lesson=6)
        self.assertGreater(article.pk, max(r['pk'] for r in expected))
        self.assertEqual(get_user_model().objects.count(), 0)
        self.assertEqual(Group.objects.count(), 0)
        self.assertEqual({r['model'] for r in expected}, {'knowledge.article'})

    def test_repeated_deploy_preserves_edits_and_user_permission_relations(self) -> None:
        load_initial_data()
        Article.objects.filter(pk=1).update(title='Editor change', image='articles/kept.png')
        custom = Article.objects.create(title='Custom', slug='custom', lesson=6)
        user = get_user_model().objects.create_user('synthetic-editor', password=None)
        group = Group.objects.create(name='synthetic-editors')
        permission = Permission.objects.get(codename='change_article', content_type__app_label='knowledge')
        group.permissions.add(permission)
        user.groups.add(group)
        before = serializers.serialize('json', Article.objects.order_by('pk'))
        call_command('load_initial_data', verbosity=0)
        self.assertEqual(serializers.serialize('json', Article.objects.order_by('pk')), before)
        self.assertEqual(Article.objects.get(pk=1).title, 'Editor change')
        self.assertEqual(Article.objects.get(pk=1).image.name, 'articles/kept.png')
        self.assertTrue(Article.objects.filter(pk=custom.pk).exists())
        self.assertTrue(get_user_model().objects.get(pk=user.pk).has_perm('knowledge.change_article'))
        self.assertGreater(get_user_model().objects.create_user('next-synthetic-user', password=None).pk, user.pk)
        self.assertGreater(Group.objects.create(name='next-synthetic-group').pk, group.pk)

    def test_existing_database_without_marker_is_not_overwritten(self) -> None:
        existing = Article.objects.create(pk=1, title='Owned', slug='owned', lesson=6)
        result = load_initial_data()
        self.assertEqual(result.status, 'preserved_existing')
        self.assertEqual(list(Article.objects.values_list('pk', 'title')), [(existing.pk, 'Owned')])
        self.assertEqual(InitialDataState.objects.count(), 1)

    def test_user_only_database_does_not_receive_seed_records(self) -> None:
        user = get_user_model().objects.create_user('existing-synthetic-user', password=None)
        self.assertEqual(load_initial_data().status, 'preserved_existing')
        self.assertFalse(Article.objects.exists())
        self.assertTrue(get_user_model().objects.filter(pk=user.pk).exists())

    def test_failed_import_rolls_back_records_and_marker_then_can_retry(self) -> None:
        def fail(*args: object, **kwargs: object) -> None:
            Article.objects.create(pk=1, title='Partial', slug='partial', lesson=6)
            raise RuntimeError('Synthetic import failure')
        with patch('knowledge.services.initial_data.call_command', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, 'Synthetic import failure'):
                load_initial_data()
        self.assertFalse(Article.objects.exists())
        self.assertFalse(InitialDataState.objects.exists())
        self.assertEqual(load_initial_data().status, 'loaded')

    def test_marker_preserves_intentional_deletion_of_all_initial_articles(self) -> None:
        load_initial_data()
        Article.objects.all().delete()
        self.assertEqual(load_initial_data().status, 'loaded')
        self.assertFalse(Article.objects.exists())

    def test_contenttypes_permissions_and_generated_ids_remain_consistent(self) -> None:
        load_initial_data()
        contenttype = ContentType.objects.get_for_model(Article)
        permissions = Permission.objects.filter(content_type=contenttype)
        self.assertEqual(set(permissions.values_list('codename', flat=True)),
                         {'add_article', 'change_article', 'delete_article', 'view_article'})
        before = list(permissions.values_list('pk', flat=True))
        call_command('load_initial_data', verbosity=0)
        self.assertEqual(list(permissions.values_list('pk', flat=True)), before)
        self.assertEqual(ContentType.objects.filter(app_label='knowledge', model='article').count(), 1)
        maximum = Permission.objects.order_by('-pk').first().pk
        extra = Permission.objects.create(content_type=contenttype, codename='synthetic_extra', name='Synthetic')
        self.assertGreater(extra.pk, maximum)
        maximum_type = ContentType.objects.order_by('-pk').first().pk
        next_type = ContentType.objects.create(app_label='synthetic', model='synthetic')
        self.assertGreater(next_type.pk, maximum_type)

    def test_repeated_initialization_preserves_existing_media_file(self) -> None:
        temporary = Path(__file__).resolve().parents[2] / '.local/tmp'
        temporary.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temporary) as directory, override_settings(MEDIA_ROOT=directory):
            load_initial_data()
            media = Path(directory) / 'kept.png'
            media.write_bytes(b'synthetic-media-content')
            Article.objects.filter(pk=1).update(image='kept.png')
            load_initial_data()
            self.assertEqual(media.read_bytes(), b'synthetic-media-content')
            self.assertEqual(Article.objects.get(pk=1).image.name, 'kept.png')
