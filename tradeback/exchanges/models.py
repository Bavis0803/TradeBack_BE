import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from .fields import EncryptedTextField


class ExchangeAccount(models.Model):
    class Exchange(models.TextChoices):
        BINANCE = "BINANCE", "Binance"

    class Status(models.TextChoices):
        CONNECTED = "CONNECTED", "Connected"
        ERROR = "ERROR", "Error"
        DISABLED = "DISABLED", "Disabled"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="exchange_accounts",
    )
    exchange = models.CharField(max_length=20, choices=Exchange.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.CONNECTED)
    api_key_hint = models.CharField(max_length=16, blank=True)
    is_testnet = models.BooleanField(default=False)
    last_verified_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("user", "exchange"), name="unique_user_exchange")
        ]

    def __str__(self):
        return f"{self.user.email} - {self.exchange} ({self.status})"


class ExchangeCredential(models.Model):
    account = models.OneToOneField(
        ExchangeAccount,
        on_delete=models.CASCADE,
        related_name="credential",
    )
    api_key = EncryptedTextField()
    api_secret = EncryptedTextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Encrypted credentials for {self.account_id}"


class PortfolioSnapshot(models.Model):
    class Source(models.TextChoices):
        LIVE = "LIVE", "Live Binance account"
        ESTIMATED = "ESTIMATED", "Estimated from account income"

    account = models.ForeignKey(
        ExchangeAccount,
        on_delete=models.CASCADE,
        related_name="portfolio_snapshots",
    )
    snapshot_date = models.DateField()
    total_value_usdt = models.DecimalField(max_digits=32, decimal_places=12)
    spot_value_usdt = models.DecimalField(max_digits=32, decimal_places=12, default=0)
    futures_value_usdt = models.DecimalField(max_digits=32, decimal_places=12, default=0)
    source = models.CharField(max_length=16, choices=Source.choices, default=Source.LIVE)
    captured_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("account", "snapshot_date"),
                name="unique_daily_portfolio_snapshot",
            )
        ]
        ordering = ("snapshot_date",)


class TradeLog(models.Model):
    class Market(models.TextChoices):
        FUTURES = "FUTURES", "USD-M Futures"
        SPOT = "SPOT", "Spot"

    class Source(models.TextChoices):
        BINANCE = "BINANCE", "Binance"
        DRAFT = "DRAFT", "Draft / paper trade"

    class Status(models.TextChoices):
        FILLED = "FILLED", "Filled"
        DRAFT = "DRAFT", "Draft"
        CANCELLED = "CANCELLED", "Cancelled"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="trade_logs",
    )

    account = models.ForeignKey(
        ExchangeAccount,
        on_delete=models.SET_NULL,
        related_name="trade_logs",
        null=True,
        blank=True,
    )
    client_ref = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    source = models.CharField(max_length=16, choices=Source.choices)
    status = models.CharField(max_length=16, choices=Status.choices)
    market = models.CharField(max_length=16, choices=Market.choices)
    symbol = models.CharField(max_length=32)
    trade_id = models.BigIntegerField(null=True, blank=True)
    order_id = models.BigIntegerField(null=True, blank=True)
    side = models.CharField(max_length=8)
    price = models.DecimalField(max_digits=32, decimal_places=12)
    quantity = models.DecimalField(max_digits=32, decimal_places=12)
    quote_quantity = models.DecimalField(max_digits=32, decimal_places=12, default=0)
    realized_pnl = models.DecimalField(max_digits=32, decimal_places=12, default=0)
    commission = models.DecimalField(max_digits=32, decimal_places=12, default=0)
    commission_asset = models.CharField(max_length=16, blank=True)
    stop_loss = models.DecimalField(max_digits=32, decimal_places=12, null=True, blank=True)
    take_profit = models.DecimalField(max_digits=32, decimal_places=12, null=True, blank=True)
    leverage = models.PositiveSmallIntegerField(default=1)
    note = models.CharField(max_length=500, blank=True)
    executed_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("account", "market", "symbol", "trade_id"),
                condition=models.Q(source="BINANCE"),
                name="unique_real_binance_trade",
            )
        ]
        indexes = [
            models.Index(fields=("user", "-executed_at"), name="tradelog_user_time_idx"),
            models.Index(
                fields=("user", "source", "-executed_at"),
                name="tradelog_user_src_time_idx",
            ),
            models.Index(
                fields=("user", "market", "side", "-executed_at"),
                name="tradelog_user_filter_idx",
            ),
        ]
        ordering = ("-executed_at",)


class TradeSyncState(models.Model):
    account = models.ForeignKey(
        ExchangeAccount,
        on_delete=models.CASCADE,
        related_name="trade_sync_states",
    )
    market = models.CharField(max_length=16, choices=TradeLog.Market.choices)
    symbol = models.CharField(max_length=32)
    last_trade_id = models.BigIntegerField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("account", "market", "symbol"),
                name="unique_trade_sync_cursor",
            )
        ]
