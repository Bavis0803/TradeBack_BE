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


class AISignalAgent(models.Model):
    class Provider(models.TextChoices):
        OPENAI = "OPENAI", "OpenAI"

    class Status(models.TextChoices):
        CONNECTED = "CONNECTED", "Connected"
        ERROR = "ERROR", "Error"
        DISABLED = "DISABLED", "Disabled"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ai_signal_agent"
    )
    provider = models.CharField(max_length=16, choices=Provider.choices, default=Provider.OPENAI)
    api_key = EncryptedTextField()
    api_key_hint = models.CharField(max_length=16, blank=True)
    model = models.CharField(max_length=64, default="gpt-5-nano")
    enabled = models.BooleanField(default=True)
    min_confidence = models.DecimalField(
        max_digits=4, decimal_places=3, default=Decimal("0.900"),
        validators=[MinValueValidator(Decimal("0.500")), MaxValueValidator(Decimal("1.000"))],
    )
    daily_call_limit = models.PositiveSmallIntegerField(
        default=50, validators=[MinValueValidator(1), MaxValueValidator(1000)]
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.CONNECTED)
    last_error = models.CharField(max_length=500, blank=True)
    last_verified_at = models.DateTimeField(null=True, blank=True)
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

    class EntryOrderType(models.TextChoices):
        MARKET = "MARKET", "Market entry"
        LIMIT = "LIMIT", "Limit entry"
        SMART = "SMART", "Market with limit fallback"

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
    risk_percent_per_order = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("20.00"),
        validators=[MinValueValidator(Decimal("0.10")), MaxValueValidator(Decimal("100.00"))],
    )
    minimum_risk_reward = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("1.00"),
        validators=[MinValueValidator(Decimal("0.10")), MaxValueValidator(Decimal("20.00"))],
    )
    tp1_close_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("70.00"),
        validators=[MinValueValidator(Decimal("1.00")), MaxValueValidator(Decimal("100.00"))],
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
    entry_order_type = models.CharField(
        max_length=8, choices=EntryOrderType.choices, default=EntryOrderType.SMART
    )
    limit_expiry_minutes = models.PositiveSmallIntegerField(
        default=15, validators=[MinValueValidator(11), MaxValueValidator(120)]
    )
    ai_detection_enabled = models.BooleanField(default=False)
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
        REVIEW = "REVIEW", "Needs user review"
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


class SignalCandidate(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Waiting for review"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"

    message = models.OneToOneField(
        TelegramMessage, on_delete=models.CASCADE, related_name="signal_candidate"
    )
    symbol = models.CharField(max_length=32)
    direction = models.CharField(max_length=8, choices=TradeSignal.Direction.choices)
    target_hint = models.CharField(max_length=64, blank=True)
    reason = models.CharField(max_length=255)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=("status", "-created_at"), name="signal_candidate_status_idx"),
        ]


class AISignalAnalysis(models.Model):
    class Status(models.TextChoices):
        SIGNAL = "SIGNAL", "Signal"
        NOT_SIGNAL = "NOT_SIGNAL", "Not a signal"
        LOW_CONFIDENCE = "LOW_CONFIDENCE", "Low confidence"
        ERROR = "ERROR", "Error"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ai_signal_analyses"
    )
    content_hash = models.CharField(max_length=64)
    provider = models.CharField(max_length=16)
    model = models.CharField(max_length=64)
    prompt_version = models.CharField(max_length=16)
    status = models.CharField(max_length=16, choices=Status.choices)
    confidence = models.DecimalField(max_digits=4, decimal_places=3, default=0)
    result = models.JSONField(default=dict)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    error = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("user", "content_hash", "provider", "model", "prompt_version"),
                name="unique_ai_signal_analysis",
            )
        ]
        indexes = [
            models.Index(fields=("user", "-created_at"), name="ai_signal_user_time_idx"),
        ]


class CopyExecution(models.Model):
    class Status(models.TextChoices):
        PAPER_FILLED = "PAPER_FILLED", "Paper filled"
        SUBMITTED = "SUBMITTED", "Submitted"
        PROTECTED = "PROTECTED", "TP/SL protected"
        SKIPPED = "SKIPPED", "Skipped by risk checks"
        FAILED = "FAILED", "Failed"
        PENDING_ENTRY = "PENDING_ENTRY", "Waiting for limit entry"
        CANCELLED = "CANCELLED", "Entry cancelled"

    class PositionStatus(models.TextChoices):
        NONE = "NONE", "No position"
        OPEN = "OPEN", "Open"
        CLOSED = "CLOSED", "Closed"
        PENDING = "PENDING", "Pending entry"

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
    entry_order_type = models.CharField(max_length=8, default=CopyStrategy.EntryOrderType.MARKET)
    limit_price = models.DecimalField(max_digits=32, decimal_places=12, null=True, blank=True)
    entry_expires_at = models.DateTimeField(null=True, blank=True)
    stop_order_id = models.CharField(max_length=64, blank=True)
    take_profit_order_id = models.CharField(max_length=64, blank=True)
    runner_take_profit = models.DecimalField(
        max_digits=32, decimal_places=12, null=True, blank=True
    )
    runner_take_profit_order_id = models.CharField(max_length=64, blank=True)
    take_profit_quantity = models.DecimalField(max_digits=32, decimal_places=12, default=0)
    remaining_quantity = models.DecimalField(max_digits=32, decimal_places=12, default=0)
    tp1_close_percent = models.DecimalField(max_digits=5, decimal_places=2, default=100)
    break_even_stop_order_id = models.CharField(max_length=64, blank=True)
    break_even_activated_at = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=500, blank=True)
    position_status = models.CharField(
        max_length=8, choices=PositionStatus.choices, default=PositionStatus.NONE
    )
    exit_price = models.DecimalField(max_digits=32, decimal_places=12, null=True, blank=True)
    realized_pnl = models.DecimalField(max_digits=32, decimal_places=12, default=0)
    close_reason = models.CharField(max_length=32, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    last_binance_seen_at = models.DateTimeField(null=True, blank=True)
    binance_missing_since = models.DateTimeField(null=True, blank=True)
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
