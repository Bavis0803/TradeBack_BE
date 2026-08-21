import django.db.models.deletion
from django.db import migrations, models

import exchanges.fields


def move_credentials_to_encrypted_table(apps, schema_editor):
    ExchangeAccount = apps.get_model("exchanges", "ExchangeAccount")
    ExchangeCredential = apps.get_model("exchanges", "ExchangeCredential")
    for account in ExchangeAccount.objects.all().iterator():
        api_key = account.api_key
        ExchangeCredential.objects.create(
            account_id=account.id,
            api_key=api_key,
            api_secret=account.api_secret,
        )
        account.api_key_hint = (
            f"{api_key[:4]}****{api_key[-4:]}" if len(api_key) > 8 else "****"
        )
        account.save(update_fields=("api_key_hint",))


class Migration(migrations.Migration):
    dependencies = [("exchanges", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="exchangeaccount",
            name="api_key_hint",
            field=models.CharField(blank=True, max_length=16),
        ),
        migrations.AddField(
            model_name="exchangeaccount",
            name="last_error",
            field=models.CharField(blank=True, max_length=500),
        ),
        migrations.AddField(
            model_name="exchangeaccount",
            name="last_synced_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="exchangeaccount",
            name="last_verified_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="exchangeaccount",
            name="status",
            field=models.CharField(
                choices=[
                    ("CONNECTED", "Connected"),
                    ("ERROR", "Error"),
                    ("DISABLED", "Disabled"),
                ],
                default="CONNECTED",
                max_length=20,
            ),
        ),
        migrations.CreateModel(
            name="ExchangeCredential",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("api_key", exchanges.fields.EncryptedTextField()),
                ("api_secret", exchanges.fields.EncryptedTextField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "account",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="credential",
                        to="exchanges.exchangeaccount",
                    ),
                ),
            ],
        ),
        migrations.RunPython(move_credentials_to_encrypted_table, migrations.RunPython.noop),
        migrations.RemoveField(model_name="exchangeaccount", name="api_key"),
        migrations.RemoveField(model_name="exchangeaccount", name="api_secret"),
        migrations.AlterUniqueTogether(name="exchangeaccount", unique_together=set()),
        migrations.AddConstraint(
            model_name="exchangeaccount",
            constraint=models.UniqueConstraint(
                fields=("user", "exchange"), name="unique_user_exchange"
            ),
        ),
    ]
