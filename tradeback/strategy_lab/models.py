import uuid
from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class StrategyDefinition(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        QUEUED = "QUEUED", "Queued"
        TRAINING = "TRAINING", "Training"
        TRAINED = "TRAINED", "Trained"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="strategy_definitions"
    )
    name = models.CharField(max_length=120)
    description = models.CharField(max_length=500, blank=True)
    indicator_code = models.TextField(max_length=20000)
    long_condition = models.CharField(max_length=1000)
    short_condition = models.CharField(max_length=1000, blank=True)
    parsed_spec = models.JSONField(default=dict, blank=True)
    risk_reward_ratio = models.DecimalField(
        max_digits=6, decimal_places=2, default=Decimal("2"),
        validators=[MinValueValidator(Decimal("0.5")), MaxValueValidator(Decimal("10"))],
    )
    stop_loss_percent = models.DecimalField(
        max_digits=6, decimal_places=3, default=Decimal("1"),
        validators=[MinValueValidator(Decimal("0.1")), MaxValueValidator(Decimal("20"))],
    )
    symbols = models.JSONField(default=list)
    timeframes = models.JSONField(default=list)
    history_days = models.PositiveSmallIntegerField(
        default=90, validators=[MinValueValidator(7), MaxValueValidator(365)]
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    version = models.PositiveIntegerField(default=1)
    last_error = models.CharField(max_length=500, blank=True)
    trained_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=("user", "-updated_at"), name="strategy_user_time_idx"),
            models.Index(fields=("status", "updated_at"), name="strategy_status_time_idx"),
        ]
        ordering = ("-updated_at",)


class StrategyTrainingRun(models.Model):
    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        RUNNING = "RUNNING", "Running"
        COMPLETED = "COMPLETED", "Completed"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    strategy = models.ForeignKey(
        StrategyDefinition, on_delete=models.CASCADE, related_name="training_runs"
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.QUEUED)
    progress_percent = models.PositiveSmallIntegerField(default=0)
    config_snapshot = models.JSONField(default=dict)
    summary = models.JSONField(default=dict)
    error = models.CharField(max_length=500, blank=True)
    queued_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=("status", "queued_at"), name="training_queue_idx"),
            models.Index(fields=("strategy", "-queued_at"), name="training_strategy_idx"),
        ]
        ordering = ("-queued_at",)


class StrategyBacktestResult(models.Model):
    training_run = models.ForeignKey(
        StrategyTrainingRun, on_delete=models.CASCADE, related_name="results"
    )
    symbol = models.CharField(max_length=32)
    timeframe = models.CharField(max_length=8)
    bars_tested = models.PositiveIntegerField(default=0)
    total_trades = models.PositiveIntegerField(default=0)
    winning_trades = models.PositiveIntegerField(default=0)
    losing_trades = models.PositiveIntegerField(default=0)
    win_rate = models.DecimalField(max_digits=7, decimal_places=3, default=0)
    net_return_percent = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    profit_factor = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    max_drawdown_percent = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    trades = models.JSONField(default=list)
    equity_curve = models.JSONField(default=list)
    period_start = models.DateTimeField(null=True, blank=True)
    period_end = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=("training_run", "symbol", "timeframe"), name="unique_training_market"
        )]
        indexes = [models.Index(
            fields=("training_run", "-win_rate"), name="backtest_run_win_idx"
        )]


class StrategyRuntime(models.Model):
    class Mode(models.TextChoices):
        PAPER = "PAPER", "Paper"
        LIVE = "LIVE", "Live Binance Futures"

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        PAUSED = "PAUSED", "Paused"
        ERROR = "ERROR", "Error"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="strategy_runtimes"
    )
    strategy = models.ForeignKey(
        StrategyDefinition, on_delete=models.CASCADE, related_name="runtimes"
    )
    training_run = models.ForeignKey(
        StrategyTrainingRun, on_delete=models.PROTECT, related_name="runtimes"
    )
    mode = models.CharField(max_length=8, choices=Mode.choices, default=Mode.PAPER)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE)
    symbols = models.JSONField(default=list)
    timeframes = models.JSONField(default=list)
    allocation_per_order = models.DecimalField(
        max_digits=20, decimal_places=8, validators=[MinValueValidator(Decimal("1"))]
    )
    total_budget = models.DecimalField(
        max_digits=20, decimal_places=8, validators=[MinValueValidator(Decimal("1"))]
    )
    max_daily_loss = models.DecimalField(
        max_digits=20, decimal_places=8, validators=[MinValueValidator(Decimal("1"))]
    )
    leverage = models.PositiveSmallIntegerField(
        default=1, validators=[MinValueValidator(1), MaxValueValidator(125)]
    )
    max_open_positions = models.PositiveSmallIntegerField(
        default=3, validators=[MinValueValidator(1), MaxValueValidator(20)]
    )
    last_candles = models.JSONField(default=dict, blank=True)
    last_error = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=("status", "updated_at"), name="runtime_worker_idx"),
            models.Index(fields=("user", "-created_at"), name="runtime_user_time_idx"),
        ]
        ordering = ("-created_at",)


class StrategyPosition(models.Model):
    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        CLOSED = "CLOSED", "Closed"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    runtime = models.ForeignKey(
        StrategyRuntime, on_delete=models.CASCADE, related_name="positions"
    )
    symbol = models.CharField(max_length=32)
    timeframe = models.CharField(max_length=8)
    direction = models.CharField(max_length=8, choices=(("LONG", "Long"), ("SHORT", "Short")))
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.OPEN)
    entry_price = models.DecimalField(max_digits=32, decimal_places=12)
    current_price = models.DecimalField(max_digits=32, decimal_places=12)
    quantity = models.DecimalField(max_digits=32, decimal_places=12)
    leverage = models.PositiveSmallIntegerField(default=1)
    margin_usdt = models.DecimalField(max_digits=20, decimal_places=8)
    stop_loss = models.DecimalField(max_digits=32, decimal_places=12)
    take_profit = models.DecimalField(max_digits=32, decimal_places=12)
    unrealized_pnl = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    realized_pnl = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    entry_order_id = models.CharField(max_length=64, blank=True)
    stop_order_id = models.CharField(max_length=64, blank=True)
    take_profit_order_id = models.CharField(max_length=64, blank=True)
    signal_candle_time = models.BigIntegerField()
    close_reason = models.CharField(max_length=32, blank=True)
    error = models.CharField(max_length=500, blank=True)
    opened_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=("runtime", "symbol", "timeframe", "signal_candle_time"),
            name="unique_runtime_signal_candle",
        )]
        indexes = [
            models.Index(fields=("runtime", "status", "-opened_at"), name="position_runtime_idx"),
            models.Index(fields=("runtime", "-opened_at"), name="position_history_idx"),
        ]
        ordering = ("-opened_at",)
