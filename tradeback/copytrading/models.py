import uuid
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models

from exchanges.fields import EncryptedTextField


class TelegramConnection(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Waiting for verification"
        CONNECTED = "CONNECTED", "Connected"
        ERROR = "ERROR", "Error"
        DISABLED = "DISABLED", "Disabled"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="telegram_connection"
    )
    api_id = models.PositiveBigIntegerField()
    api_hash = EncryptedTextField()
    session = EncryptedTextField(blank=True, default="")
    phone = EncryptedTextField(blank=True, default="")
    phone_hint = models.CharField(max_length=32, blank=True)
    phone_code_hash = EncryptedTextField(blank=True, default="")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    last_error = models.CharField(max_length=500, blank=True)
    challenge_expires_at = models.DateTimeField(null=True, blank=True)
    last_connected_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class CopyStrategy(models.Model):
    class Mode(models.TextChoices):
        PAPER = "PAPER", "Paper trading"
        LIVE = "LIVE", "Live Binance Futures"

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Listening"
        PAUSED = "PAUSED", "Paused"
        ERROR = "ERROR", "Error"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="copy_strategies"
    )
    telegram_connection = models.ForeignKey(
        TelegramConnection, on_delete=models.CASCADE, related_name="strategies"
    )
    chat_id = models.BigIntegerField()
    chat_title = models.CharField(max_length=255)
    chat_username = models.CharField(max_length=255, blank=True)
    mode = models.CharField(max_length=8, choices=Mode.choices, default=Mode.PAPER)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE)
    allocation_usdt = models.DecimalField(
        max_digits=20, decimal_places=8, validators=[MinValueValidator(1)]
    )
    max_leverage = models.PositiveSmallIntegerField(
        default=10, validators=[MinValueValidator(1), MaxValueValidator(125)]
    )
    use_binance_max_leverage = models.BooleanField(default=True)
    max_daily_loss_usdt = models.DecimalField(
        max_digits=20, decimal_places=8, default=50, validators=[MinValueValidator(1)]
    )
    entry_tolerance_percent = models.DecimalField(
        max_digits=5,
        decimal_places=3,
        default=Decimal("0.300"),
        validators=[MinValueValidator(0), MaxValueValidator(2)],
    )
    allowed_symbols = models.JSONField(default=list, blank=True)
    last_message_id = models.BigIntegerField(null=True, blank=True)
    last_notified_message_id = models.BigIntegerField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("user", "chat_id"), name="unique_user_copy_chat")
        ]
        indexes = [models.Index(fields=("status", "updated_at"), name="copy_strategy_worker_idx")]


class TelegramMessage(models.Model):
    class ParseStatus(models.TextChoices):
        SIGNAL = "SIGNAL", "Trading signal"
        IGNORED = "IGNORED", "Ignored"
        INVALID = "INVALID", "Invalid signal"

    strategy = models.ForeignKey(CopyStrategy, on_delete=models.CASCADE, related_name="messages")
    telegram_message_id = models.BigIntegerField()
    sender_name = models.CharField(max_length=255, blank=True)
    text = models.TextField(blank=True)
    media_file = models.FileField(upload_to="copy_trading_media/%Y/%m/%d/", blank=True)
    media_type = models.CharField(max_length=16, blank=True)
    media_mime_type = models.CharField(max_length=128, blank=True)
    media_size = models.PositiveBigIntegerField(default=0)
    sent_at = models.DateTimeField()
    parse_status = models.CharField(
        max_length=12, choices=ParseStatus.choices, default=ParseStatus.IGNORED
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("strategy", "telegram_message_id"), name="unique_telegram_message"
            )
        ]
        indexes = [
            models.Index(fields=("strategy", "-sent_at"), name="telegram_strategy_time_idx"),
            models.Index(fields=("strategy", "parse_status", "-sent_at"), name="telegram_parse_idx"),
        ]
        ordering = ("-sent_at",)


class TradeSignal(models.Model):
    class Direction(models.TextChoices):
        LONG = "LONG", "Long"
        SHORT = "SHORT", "Short"

    message = models.OneToOneField(
        TelegramMessage, on_delete=models.CASCADE, related_name="signal"
    )
    symbol = models.CharField(max_length=32)
    direction = models.CharField(max_length=8, choices=Direction.choices)
    entry_low = models.DecimalField(max_digits=32, decimal_places=12)
    entry_high = models.DecimalField(max_digits=32, decimal_places=12)
    stop_loss = models.DecimalField(max_digits=32, decimal_places=12)
    take_profits = models.JSONField(default=list)
    requested_leverage = models.PositiveSmallIntegerField(null=True, blank=True)
    parser_version = models.CharField(max_length=16, default="chn-v1")
    created_at = models.DateTimeField(auto_now_add=True)


class CopyExecution(models.Model):
    class Status(models.TextChoices):
        PAPER_FILLED = "PAPER_FILLED", "Paper filled"
        SUBMITTED = "SUBMITTED", "Submitted"
        PROTECTED = "PROTECTED", "TP/SL protected"
        SKIPPED = "SKIPPED", "Skipped by risk checks"
        FAILED = "FAILED", "Failed"

    class PositionStatus(models.TextChoices):
        NONE = "NONE", "No position"
        OPEN = "OPEN", "Open"
        CLOSED = "CLOSED", "Closed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    strategy = models.ForeignKey(CopyStrategy, on_delete=models.CASCADE, related_name="executions")
    signal = models.ForeignKey(TradeSignal, on_delete=models.CASCADE, related_name="executions")
    status = models.CharField(max_length=16, choices=Status.choices)
    symbol = models.CharField(max_length=32)
    direction = models.CharField(max_length=8)
    entry_price = models.DecimalField(max_digits=32, decimal_places=12)
    quantity = models.DecimalField(max_digits=32, decimal_places=12, default=0)
    leverage = models.PositiveSmallIntegerField(default=1)
    margin_usdt = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    stop_loss = models.DecimalField(max_digits=32, decimal_places=12)
    take_profit = models.DecimalField(max_digits=32, decimal_places=12)
    entry_order_id = models.CharField(max_length=64, blank=True)
    stop_order_id = models.CharField(max_length=64, blank=True)
    take_profit_order_id = models.CharField(max_length=64, blank=True)
    error = models.CharField(max_length=500, blank=True)
    position_status = models.CharField(
        max_length=8, choices=PositionStatus.choices, default=PositionStatus.NONE
    )
    exit_price = models.DecimalField(max_digits=32, decimal_places=12, null=True, blank=True)
    realized_pnl = models.DecimalField(max_digits=32, decimal_places=12, default=0)
    close_reason = models.CharField(max_length=32, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    trade_log = models.OneToOneField(
        "exchanges.TradeLog", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="copy_execution",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("strategy", "signal"), name="unique_copy_execution")
        ]
        indexes = [
            models.Index(fields=("strategy", "-created_at"), name="copy_execution_time_idx"),
            models.Index(fields=("strategy", "status", "-created_at"), name="copy_execution_status_idx"),
            models.Index(fields=("strategy", "position_status", "-created_at"), name="copy_position_status_idx"),
        ]
        ordering = ("-created_at",)
