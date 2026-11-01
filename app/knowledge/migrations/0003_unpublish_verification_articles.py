"""Keep committed verification rows without exposing them as course materials."""
import re

from django.db import migrations


def unpublish_probes(apps, schema_editor):
    article = apps.get_model('knowledge', 'Article')
    candidates = article.objects.using(schema_editor.connection.alias).filter(
        lesson=6, summary='proof', published=True, image='')
    for row in candidates.iterator():
        if (row.title == row.slug
                and re.fullmatch(r'(baseline|failover|recovered|expanded)-[0-9a-f]{32}', row.slug)
                and re.fullmatch(r'committed-[0-9a-f]{32}', row.body)):
            candidates.filter(pk=row.pk).update(published=False)


class Migration(migrations.Migration):
    dependencies = [('knowledge', '0002_initialdatastate')]
    operations = [migrations.RunPython(unpublish_probes, migrations.RunPython.noop)]
