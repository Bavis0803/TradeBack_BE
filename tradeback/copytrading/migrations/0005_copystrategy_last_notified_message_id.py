from django.db import migrations, models
from django.db.models import F


def initialize_notification_cursors(apps, schema_editor):
    CopyStrategy = apps.get_model("copytrading", "CopyStrategy")
    CopyStrategy.objects.update(last_notified_message_id=F("last_message_id"))


class Migration(migrations.Migration):
    dependencies = [("copytrading", "0004_copystrategy_use_binance_max_leverage")]

    operations = [
        migrations.AddField(
            model_name="copystrategy",
            name="last_notified_message_id",
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.RunPython(initialize_notification_cursors, migrations.RunPython.noop),
    ]
