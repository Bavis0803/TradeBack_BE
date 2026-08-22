from decimal import Decimal

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("copytrading", "0005_copystrategy_last_notified_message_id")]

    operations = [
        migrations.AddField(
            model_name="copystrategy",
            name="entry_tolerance_percent",
            field=models.DecimalField(
                decimal_places=3,
                default=Decimal("0.300"),
                max_digits=5,
                validators=[MinValueValidator(0), MaxValueValidator(2)],
            ),
        ),
    ]
