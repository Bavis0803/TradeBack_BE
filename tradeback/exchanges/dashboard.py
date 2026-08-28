from collections import defaultdict
from datetime import datetime, time, timedelta, timezone as datetime_timezone
from decimal import Decimal, InvalidOperation

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from .connection import build_binance_service, touch_exchange_sync
from .models import PortfolioSnapshot, TradeLog
from .services import BinanceServiceError, decimal_to_string


DASHBOARD_CACHE_VERSION = "v3"
DASHBOARD_CACHE_SECONDS = 60
DASHBOARD_STALE_CACHE_SECONDS = 300
DASHBOARD_BUILD_LOCK_SECONDS = 20
RECENT_TRADE_SYNC_SECONDS = 300
PNL_INCOME_TYPES = {"REALIZED_PNL", "COMMISSION", "FUNDING_FEE"}
STABLE_ASSETS = {"USDT", "USDC", "FDUSD", "BUSD"}
ASSET_NAMES = {
    "BTC": "Bitcoin",
    "ETH": "Ethereum",
    "BNB": "BNB",
    "SOL": "Solana",
    "USDT": "Tether",
    "USDC": "USD Coin",
    "FDUSD": "First Digital USD",
}


def as_decimal(value, default="0"):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def as_utc_datetime(timestamp_ms):
    return datetime.fromtimestamp(int(timestamp_ms) / 1000, tz=datetime_timezone.utc)


def ticker_maps(payload):
    prices = {}
    changes = {}
    for item in payload or []:
        symbol = item.get("symbol", "")
        if not symbol:
            continue
        prices[symbol] = as_decimal(item.get("lastPrice", item.get("closePrice")))
        changes[symbol] = as_decimal(item.get("priceChangePercent"))
    return prices, changes


def asset_quote(asset, spot_prices, futures_prices, spot_changes, futures_changes):
    if asset in STABLE_ASSETS:
        return Decimal("1"), Decimal("0")
    symbol = f"{asset}USDT"
    price = spot_prices.get(symbol) or futures_prices.get(symbol) or Decimal("0")
    change = spot_changes.get(symbol, futures_changes.get(symbol, Decimal("0")))
    return price, change


def sync_recent_futures_trades(account, service, candidate_symbols):
    for symbol in candidate_symbols[:4]:
        try:
            trades = service.get_futures_user_trades(symbol, limit=50)
        except BinanceServiceError:
            continue
        for item in trades:
            quantity = as_decimal(item.get("qty"))
            price = as_decimal(item.get("price"))
            quote_quantity = as_decimal(item.get("quoteQty"), price * quantity)
            TradeLog.objects.update_or_create(
                account=account,
                market=TradeLog.Market.FUTURES,
                symbol=item.get("symbol", symbol),
                trade_id=int(item["id"]),
                defaults={
                    "user": account.user,
                    "source": TradeLog.Source.BINANCE,
                    "status": TradeLog.Status.FILLED,
                    "order_id": item.get("orderId"),
                    "side": item.get("side", "BUY"),
                    "price": price,
                    "quantity": quantity,
                    "quote_quantity": quote_quantity,
                    "realized_pnl": as_decimal(item.get("realizedPnl")),
                    "commission": as_decimal(item.get("commission")),
                    "commission_asset": item.get("commissionAsset", ""),
                    "executed_at": as_utc_datetime(item["time"]),
                },
            )


def persist_portfolio_history(account, total_value, spot_value, futures_value, incomes):
    today = timezone.localdate()
    daily_income = defaultdict(lambda: Decimal("0"))
    for item in incomes:
        if item.get("incomeType") not in PNL_INCOME_TYPES:
            continue
        income_date = as_utc_datetime(item["time"]).date()
        daily_income[income_date] += as_decimal(item.get("income"))

    running_after_day = Decimal("0")
    estimates = {}
    for days_ago in range(0, 30):
        snapshot_date = today - timedelta(days=days_ago)
        if days_ago == 0:
            estimates[snapshot_date] = total_value
        else:
            running_after_day += daily_income[snapshot_date + timedelta(days=1)]
            estimates[snapshot_date] = total_value - running_after_day

    # Today's point is authoritative. Historical estimates only fill gaps and must
    # never overwrite a LIVE snapshot captured on an earlier day.
    with transaction.atomic():
        PortfolioSnapshot.objects.update_or_create(
            account=account,
            snapshot_date=today,
            defaults={
                "total_value_usdt": max(total_value, Decimal("0")),
                "spot_value_usdt": spot_value,
                "futures_value_usdt": futures_value,
                "source": PortfolioSnapshot.Source.LIVE,
            },
        )
        historical_estimates = {
            snapshot_date: max(value, Decimal("0"))
            for snapshot_date, value in estimates.items()
            if snapshot_date != today
        }
        existing = {
            item.snapshot_date: item
            for item in PortfolioSnapshot.objects.filter(
                account=account,
                snapshot_date__in=historical_estimates,
            )
        }
        to_create = []
        to_update = []
        for snapshot_date, value in historical_estimates.items():
            snapshot = existing.get(snapshot_date)
            if snapshot is None:
                to_create.append(PortfolioSnapshot(
                    account=account,
                    snapshot_date=snapshot_date,
                    total_value_usdt=value,
                    spot_value_usdt=Decimal("0"),
                    futures_value_usdt=value,
                    source=PortfolioSnapshot.Source.ESTIMATED,
                ))
            elif snapshot.source == PortfolioSnapshot.Source.ESTIMATED:
                snapshot.total_value_usdt = value
                snapshot.spot_value_usdt = Decimal("0")
                snapshot.futures_value_usdt = value
                to_update.append(snapshot)
        PortfolioSnapshot.objects.bulk_create(to_create, ignore_conflicts=True)
        PortfolioSnapshot.objects.bulk_update(
            to_update,
            ("total_value_usdt", "spot_value_usdt", "futures_value_usdt"),
        )


def serialize_trade(trade):
    return {
        "id": f"{trade.market}-{trade.symbol}-{trade.trade_id}",
        "market": trade.market,
        "symbol": trade.symbol,
        "pair": trade.symbol.removesuffix("USDT") + "/USDT",
        "side": trade.side,
        "price": decimal_to_string(trade.price),
        "quantity": decimal_to_string(trade.quantity),
        "quote_quantity": decimal_to_string(trade.quote_quantity),
        "realized_pnl": decimal_to_string(trade.realized_pnl),
        "commission": decimal_to_string(trade.commission),
        "commission_asset": trade.commission_asset,
        "executed_at": trade.executed_at,
    }


def _dashboard_cache_keys(account):
    prefix = f"binance-dashboard:{DASHBOARD_CACHE_VERSION}:{account.pk}"
    return prefix, f"{prefix}:stale", f"{prefix}:build-lock"


def build_dashboard_payload(account, force_refresh=False):
    cache_key, stale_key, lock_key = _dashboard_cache_keys(account)
    if not force_refresh:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

    owns_lock = cache.add(lock_key, "1", timeout=DASHBOARD_BUILD_LOCK_SECONDS)
    if not owns_lock and not force_refresh:
        stale = cache.get(stale_key)
        if stale is not None:
            return stale

    try:
        payload = _build_dashboard_payload(account, force_refresh=force_refresh)
        cache.set(cache_key, payload, timeout=DASHBOARD_CACHE_SECONDS)
        cache.set(stale_key, payload, timeout=DASHBOARD_STALE_CACHE_SECONDS)
        return payload
    except BinanceServiceError:
        stale = cache.get(stale_key)
        if stale is not None and not force_refresh:
            return stale
        raise
    finally:
        if owns_lock:
            cache.delete(lock_key)


def _build_dashboard_payload(account, force_refresh=False):

    service = build_binance_service(account)
    now = timezone.now()
    start_30d_ms = int((now - timedelta(days=30)).timestamp() * 1000)
    futures_account = service.get_futures_account()
    incomes = service.get_futures_income(start_time=start_30d_ms, limit=1000)
    futures_tickers = service.get_futures_tickers()

    warnings = []
    try:
        spot_account = service.get_spot_account()
        spot_tickers = service.get_spot_tickers()
    except BinanceServiceError as error:
        spot_account = {"balances": []}
        spot_tickers = []
        warnings.append(f"Spot wallet unavailable: {error}")

    futures_prices, futures_changes = ticker_maps(futures_tickers)
    spot_prices, spot_changes = ticker_maps(spot_tickers)
    balances = defaultdict(lambda: {"balance": Decimal("0"), "wallets": set()})

    for item in spot_account.get("balances", []):
        balance = as_decimal(item.get("free")) + as_decimal(item.get("locked"))
        if balance:
            balances[item["asset"]]["balance"] += balance
            balances[item["asset"]]["wallets"].add("SPOT")

    for item in futures_account.get("assets", []):
        balance = as_decimal(item.get("marginBalance", item.get("walletBalance")))
        if balance:
            balances[item["asset"]]["balance"] += balance
            balances[item["asset"]]["wallets"].add("FUTURES")

    holdings = []
    for asset, item in balances.items():
        price, change = asset_quote(
            asset, spot_prices, futures_prices, spot_changes, futures_changes
        )
        value = item["balance"] * price
        holdings.append({
            "symbol": asset,
            "name": ASSET_NAMES.get(asset, asset),
            "balance": decimal_to_string(item["balance"]),
            "value_usdt": decimal_to_string(value),
            "change_24h_percent": decimal_to_string(change),
            "wallets": sorted(item["wallets"]),
        })
    holdings.sort(key=lambda item: as_decimal(item["value_usdt"]), reverse=True)

    spot_value = sum(
        as_decimal(item["value_usdt"])
        for item in holdings
        if "SPOT" in item["wallets"]
    )
    futures_value = as_decimal(futures_account.get("totalMarginBalance"))
    # A mixed-wallet asset is counted proportionally above; total portfolio uses the
    # authoritative futures margin balance plus independently valued Spot balances.
    spot_value = Decimal("0")
    for item in spot_account.get("balances", []):
        balance = as_decimal(item.get("free")) + as_decimal(item.get("locked"))
        price, _ = asset_quote(
            item.get("asset", ""), spot_prices, futures_prices, spot_changes, futures_changes
        )
        spot_value += balance * price
    total_value = spot_value + futures_value

    positions = []
    for item in futures_account.get("positions", []):
        amount = as_decimal(item.get("positionAmt"))
        if not amount:
            continue
        positions.append({
            "symbol": item["symbol"],
            "side": "LONG" if amount > 0 else "SHORT",
            "quantity": decimal_to_string(abs(amount)),
            "entry_price": decimal_to_string(as_decimal(item.get("entryPrice"))),
            "unrealized_pnl": decimal_to_string(as_decimal(item.get("unrealizedProfit"))),
            "leverage": int(item.get("leverage", 1)),
        })

    income_sorted = sorted(incomes, key=lambda item: int(item.get("time", 0)), reverse=True)
    candidate_symbols = []
    for symbol in [item["symbol"] for item in positions] + [
        item.get("symbol") for item in income_sorted
    ]:
        if symbol and symbol not in candidate_symbols:
            candidate_symbols.append(symbol)
    trade_sync_key = f"binance-dashboard-trades:{account.pk}"
    if force_refresh or cache.add(trade_sync_key, "1", timeout=RECENT_TRADE_SYNC_SECONDS):
        sync_recent_futures_trades(account, service, candidate_symbols)

    start_24h = now - timedelta(hours=24)
    pnl_24h = sum(
        as_decimal(item.get("income"))
        for item in incomes
        if item.get("incomeType") in PNL_INCOME_TYPES
        and as_utc_datetime(item["time"]) >= start_24h
    )
    pnl_30d = sum(
        as_decimal(item.get("income"))
        for item in incomes
        if item.get("incomeType") in PNL_INCOME_TYPES
    )
    realized = [
        as_decimal(item.get("income"))
        for item in incomes
        if item.get("incomeType") == "REALIZED_PNL"
        and as_decimal(item.get("income")) != 0
    ]
    wins = sum(1 for value in realized if value > 0)
    win_rate = Decimal(wins * 100) / Decimal(len(realized)) if realized else Decimal("0")
    # The card is Futures PNL, so its denominator must be estimated opening
    # Futures equity rather than the current combined Spot + Futures portfolio.
    opening_futures_equity = futures_value - pnl_24h
    pnl_percent = (
        pnl_24h * Decimal("100") / opening_futures_equity
        if opening_futures_equity > 0
        else Decimal("0")
    )

    persist_portfolio_history(account, total_value, spot_value, futures_value, incomes)
    recent_snapshots = list(
        account.portfolio_snapshots.order_by("-snapshot_date")[:30]
    )
    history = [
        {
            "date": item.snapshot_date.isoformat(),
            "value_usdt": decimal_to_string(item.total_value_usdt),
            "source": item.source,
        }
        for item in reversed(recent_snapshots)
    ]

    allocations = []
    for item in holdings:
        value = as_decimal(item["value_usdt"])
        if value <= 0:
            continue
        allocations.append({
            "symbol": item["symbol"],
            "value_usdt": item["value_usdt"],
            "percent": decimal_to_string(value * Decimal("100") / total_value)
            if total_value
            else "0",
        })

    recent_trades = [
        serialize_trade(item)
        for item in account.trade_logs.filter(source=TradeLog.Source.BINANCE)
        .select_related(None)
        .order_by("-executed_at")[:5]
    ]
    touch_exchange_sync(account)
    payload = {
        "account_mode": "connected",
        "exchange": "BINANCE",
        "as_of": now,
        "summary": {
            "total_portfolio_usdt": decimal_to_string(total_value),
            "spot_value_usdt": decimal_to_string(spot_value),
            "futures_value_usdt": decimal_to_string(futures_value),
            "pnl_24h_usdt": decimal_to_string(pnl_24h),
            "pnl_24h_percent": decimal_to_string(pnl_percent),
            "pnl_30d_usdt": decimal_to_string(pnl_30d),
            "open_positions": len(positions),
            "win_rate_30d": decimal_to_string(win_rate),
            "winning_trades_30d": wins,
            "closed_trades_30d": len(realized),
        },
        "holdings": holdings,
        "allocations": allocations,
        "portfolio_history": history,
        "positions": positions,
        "recent_trades": recent_trades,
        "warnings": warnings,
    }
    return payload
