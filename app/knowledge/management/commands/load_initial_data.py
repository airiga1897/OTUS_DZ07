from dataclasses import asdict
import json
from typing import Any

from django.core.management.base import BaseCommand

from knowledge.services.initial_data import load_initial_data


class Command(BaseCommand):
    help = 'Загрузить начальные материалы из фикстуры только в пустую базу.'

    def handle(self, *args: Any, **options: Any) -> str:
        return json.dumps(asdict(load_initial_data()))
