from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('knowledge', '0001_initial')]
    operations = [
        migrations.CreateModel(
            name='InitialDataState',
            fields=[
                ('key', models.CharField(max_length=80, primary_key=True, serialize=False)),
                ('status', models.CharField(max_length=32)),
                ('fixture_sha256', models.CharField(max_length=64)),
            ],
            options={'default_permissions': ()},
        ),
    ]
