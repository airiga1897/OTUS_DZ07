"""Load the bundled fixture once, atomically, without replacing user data."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core import serializers
from django.core.management import call_command
from django.db import transaction

from knowledge.models import Article, InitialDataState


@dataclass(frozen=True)
class InitialDataResult:
    status: str
    loaded_now: bool
    articles: int
    fixture_sha256: str
    source: str = 'bundled_DZ04_materials_fixture'


def fixture_path() -> Path:
    return Path(__file__).resolve().parents[1] / 'fixtures/initial_data.json'


def has_existing_data() -> bool:
    return Article.objects.exists() or get_user_model().objects.exists() or Group.objects.exists()


def _internal_import(path: Path) -> None:
    call_command('loaddata', str(path), verbosity=0)
    expected = json.loads(path.read_text(encoding='utf-8'))
    actual = json.loads(serializers.serialize('json', Article.objects.order_by('pk')))
    if actual != expected:
        raise ValueError('Initial article fields or identifiers do not match the fixture')


@transaction.atomic
def load_initial_data() -> InitialDataResult:
    path = fixture_path()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    state, created = InitialDataState.objects.get_or_create(
        key='portal-initial-v1', defaults={'status': 'pending', 'fixture_sha256': digest})
    if created:
        state.status = 'preserved_existing' if has_existing_data() else 'loaded'
        if state.status == 'loaded':
            _internal_import(path)
        state.save(update_fields=['status'])
    if state.status not in {'loaded', 'preserved_existing'}:
        raise ValueError('Initial-data state is incomplete')
    return InitialDataResult(state.status, created and state.status == 'loaded',
                             Article.objects.count(), state.fixture_sha256)
