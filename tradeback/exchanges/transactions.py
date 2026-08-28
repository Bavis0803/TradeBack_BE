from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from .connection import build_binance_service, touch_exchange_sync
from .dashboard import as_decimal, as_utc_datetime
from .models import TradeLog, TradeSyncState
from .services import BinanceServiceError


MAX_SYMBOLS_PER_SYNC = 20
INITIAL_TRADES_PER_SYMBOL = 1000


def _discover_symbols(account, service):
    warnings = []
    existing_futures = list(
        account.trade_sync_states.filter(market=TradeLog.Market.FUTURES)
        .values_list("symbol", flat=True)
    )
    existing_spot = list(
        account.trade_sync_states.filter(market=TradeLog.Market.SPOT)
        .values_list("symbol", flat=True)
    )

    start_ms = int((timezone.now() - timedelta(days=90)).timestamp() * 1000)
    incomes = service.get_futures_income(start_time=start_ms, limit=1000)
    futures_account = service.get_futures_account()
    futures = existing_futures[:]
    for symbol in [
        item.get("symbol")
        for item in sorted(incomes, key=lambda value: int(value.get("time", 0)), reverse=True)
    ] + [
        item.get("symbol")
        for item in futures_account.get("positions", [])
        if as_decimal(item.get("positionAmt")) != 0
    ]:
        if symbol and symbol not in futures:
            futures.append(symbol)

    spot = existing_spot[:]
    try:
        spot_account = service.get_spot_account()
        exchange_info = service.get_spot_exchange_info()
        tradable_symbols = {
            item["symbol"]
            for item in exchange_info.get("symbols", [])
            if item.get("status") == "TRADING"
        }
        for balance in spot_account.get("balances", []):
            asset = balance.get("asset")
            amount = as_decimal(balance.get("free")) + as_decimal(balance.get("locked"))
            symbol = f"{asset}USDT"
            if amount > 0 and asset != "USDT" and symbol in tradable_symbols and symbol not in spot:
                spot.append(symbol)
    except BinanceServiceError as error:
        warnings.append(f"Spot trade discovery unavailable: {error}")
    return futures[:MAX_SYMBOLS_PER_SYNC], spot[:MAX_SYMBOLS_PER_SYNC], warnings


def _trade_defaults(account, market, item):
    is_futures = market == TradeLog.Market.FUTURES
    price = as_decimal(item.get("price"))
    quantity = as_decimal(item.get("qty"))
    return {
        "user": account.user,
        "source": TradeLog.Source.BINANCE,
        "status": TradeLog.Status.FILLED,
        "order_id": item.get("orderId"),
        "side": item.get("side") if is_futures else ("BUY" if item.get("isBuyer") else "SELL"),
        "price": price,
        "quantity": quantity,
        "quote_quantity": as_decimal(item.get("quoteQty"), price * quantity),
        "realized_pnl": as_decimal(item.get("realizedPnl")) if is_futures else Decimal("0"),
        "commission": as_decimal(item.get("commission")),
        "commission_asset": item.get("commissionAsset", ""),
        "executed_at": as_utc_datetime(item["time"]),
    }


def _sync_symbol(account, service, market, symbol):
    state, _ = TradeSyncState.objects.get_or_create(
        account=account,
        market=market,
        symbol=symbol,
    )
    if state.last_synced_at and timezone.now() - state.last_synced_at < timedelta(minutes=1):
        return 0
    from_id = state.last_trade_id + 1 if state.last_trade_id is not None else None
    if market == TradeLog.Market.FUTURES:
        payload = service.get_futures_user_trades(
            symbol, limit=INITIAL_TRADES_PER_SYMBOL, from_id=from_id
        )
    else:
        payload = service.get_spot_user_trades(
            symbol, limit=INITIAL_TRADES_PER_SYMBOL, from_id=from_id
        )
    if not payload:
        state.last_synced_at = timezone.now()
        state.last_error = ""
        state.save(update_fields=("last_synced_at", "last_error"))
        return 0

    rows = [
        TradeLog(
            account=account,
            market=market,
            symbol=item.get("symbol", symbol),
            trade_id=int(item["id"]),
            **_trade_defaults(account, market, item),
        )
        for item in payload
    ]
    with transaction.atomic():
        before = TradeLog.objects.filter(
            account=account,
            market=market,
            symbol=symbol,
            trade_id__in=[row.trade_id for row in rows],
        ).count()
        TradeLog.objects.bulk_create(rows, ignore_conflicts=True, batch_size=500)
        state.last_trade_id = max(row.trade_id for row in rows)
        state.last_synced_at = timezone.now()
        state.last_error = ""
        state.save(update_fields=("last_trade_id", "last_synced_at", "last_error"))
    return max(len(rows) - before, 0)


def sync_real_trades(account):
    lock_key = f"binance-trade-sync-lock:{account.pk}"
    if not cache.add(lock_key, "1", timeout=30):
        return {"created": 0, "symbols_synced": 0, "warnings": ["A sync is already running."]}
    try:
        service = build_binance_service(account)
        futures, spot, warnings = _discover_symbols(account, service)
        created = 0
        synced = 0
        for market, symbols in (
            (TradeLog.Market.FUTURES, futures),
            (TradeLog.Market.SPOT, spot),
        ):
            for symbol in symbols:
                try:
                    created += _sync_symbol(account, service, market, symbol)
                    synced += 1
                except BinanceServiceError as error:
                    TradeSyncState.objects.update_or_create(
                        account=account,
                        market=market,
                        symbol=symbol,
                        defaults={"last_synced_at": timezone.now(), "last_error": str(error)[:500]},
                    )
                    warnings.append(f"{market} {symbol}: {error}")
        touch_exchange_sync(account)
        cache.delete_many([
            f"binance-dashboard:v3:{account.pk}",
            f"binance-dashboard:v3:{account.pk}:stale",
        ])
        return {"created": created, "symbols_synced": synced, "warnings": warnings}
    finally:
        cache.delete(lock_key)
