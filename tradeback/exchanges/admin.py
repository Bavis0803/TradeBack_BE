from django.contrib import admin

from .models import ExchangeAccount, TradeLog


@admin.register(ExchangeAccount)
class ExchangeAccountAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "exchange",
        "status",
        "api_key_hint",
        "is_testnet",
        "last_verified_at",
        "last_synced_at",
    )
    list_filter = ("exchange", "status", "is_testnet")
    search_fields = ("user__email", "api_key_hint")
    readonly_fields = (
        "api_key_hint",
        "last_verified_at",
        "last_synced_at",
        "last_error",
        "created_at",
        "updated_at",
    )


@admin.register(TradeLog)
class TradeLogAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "source",
        "market",
        "symbol",
        "side",
        "status",
        "realized_pnl",
        "executed_at",
    )
    list_filter = ("source", "market", "side", "status")
    search_fields = ("user__email", "symbol", "trade_id", "order_id")
    date_hierarchy = "executed_at"
    list_select_related = ("user", "account")
    readonly_fields = ("client_ref", "trade_id", "order_id", "created_at", "updated_at")
