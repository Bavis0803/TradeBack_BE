from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("copytrading", "0010_alter_telegrammessage_parse_status_signalcandidate"),
    ]

    operations = [
        migrations.AddField(
            model_name="copyexecution",
            name="binance_missing_since",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="copyexecution",
            name="last_binance_seen_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
