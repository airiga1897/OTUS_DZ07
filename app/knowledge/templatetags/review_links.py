"""Resolve bare course repositories to their review branch without DB writes."""

import re

from django import template

register = template.Library()
REPOSITORY = re.compile(r"https://github\.com/airiga1897/OTUS_DZ0[1-7](?=$|\s)")


@register.filter
def review_sources(value: str) -> str:
    return REPOSITORY.sub(
        lambda match: match.group() + ("/tree/elkcluster" if match.group().endswith("07") else "/tree/develop"), value
    )
