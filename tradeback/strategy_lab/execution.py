from datetime import timedelta
from decimal import Decimal, ROUND_DOWN

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from exchanges.connection import build_binance_service, get_binance_account
from exchanges.models import ExchangeAccount, TradeLog
from exchanges.services import BinanceService, BinanceServiceError, get_bracket_for_notional

from .engine import build_series, evaluate_condition, parse_klines
from .models import StrategyPosition, StrategyRuntime


TIMEFRAME_MILLISECONDS = {
    "5m": 5 * 60_000, "15m": 15 * 60_000, "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000, "1d": 24 * 60 * 60_000,
}


def _floor_step(value, step):
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step if step else value


def _pnl(position, price):
    movement = price - position.entry_price
    if position.direction == "SHORT":
        movement = -movement
    return movement * position.quantity


def _close_paper(position, price, reason):
    with transaction.atomic():
        position = StrategyPosition.objects.select_for_update().select_related(
            "runtime__strategy", "runtime__user"
        ).get(pk=position.pk)
        if position.status != StrategyPosition.Status.OPEN:
            return position
        position.current_price = price
        position.realized_pnl = _pnl(position, price)
        position.unrealized_pnl = 0
        position.status = StrategyPosition.Status.CLOSED
        position.close_reason = reason
        position.closed_at = timezone.now()
        position.save(update_fields=(
            "current_price", "realized_pnl", "unrealized_pnl", "status",
            "close_reason", "closed_at", "updated_at",
        ))
        TradeLog.objects.create(
            user=position.runtime.user,
            account=get_binance_account(position.runtime.user),
            source=TradeLog.Source.DRAFT,
            status=TradeLog.Status.DRAFT,
            market=TradeLog.Market.FUTURES,
            symbol=position.symbol,
            side="BUY" if position.direction == "LONG" else "SELL",
            price=position.entry_price,
            quantity=position.quantity,
            quote_quantity=position.entry_price * position.quantity,
            realized_pnl=position.realized_pnl,
            stop_loss=position.stop_loss,
            take_profit=position.take_profit,
            leverage=position.leverage,
            note=f"Strategy paper: {position.runtime.strategy.name} ({position.timeframe})",
            executed_at=position.opened_at,
        )
        return position


def update_runtime_positions(runtime, client=None):
    open_positions = list(runtime.positions.filter(status=StrategyPosition.Status.OPEN))
    if not open_positions:
        return
    marks = (client or BinanceService()).get_futures_mark_prices(
        [item.symbol for item in open_positions]
    )
    live_amounts = {}
    if runtime.mode == StrategyRuntime.Mode.LIVE and client:
        live_amounts = {
            item["symbol"]: Decimal(str(item.get("positionAmt", "0")))
            for item in client.get_futures_positions()
        }
    for position in open_positions:
        price = marks.get(position.symbol, position.current_price)
        if runtime.mode == StrategyRuntime.Mode.PAPER:
            if position.direction == "LONG" and price <= position.stop_loss or (
                position.direction == "SHORT" and price >= position.stop_loss
            ):
                _close_paper(position, position.stop_loss, "STOP_LOSS")
            elif position.direction == "LONG" and price >= position.take_profit or (
                position.direction == "SHORT" and price <= position.take_profit
            ):
                _close_paper(position, position.take_profit, "TAKE_PROFIT")
            else:
                position.current_price = price
                position.unrealized_pnl = _pnl(position, price)
                position.save(update_fields=("current_price", "unrealized_pnl", "updated_at"))
        elif (
            timezone.now() - position.opened_at > timedelta(seconds=10)
            and live_amounts.get(position.symbol, Decimal("0")) == 0
        ):
            position.current_price = price
            position.realized_pnl = _pnl(position, price)
            position.status = StrategyPosition.Status.CLOSED
            position.close_reason = "BINANCE_SYNC"
            position.closed_at = timezone.now()
            position.save(update_fields=(
                "current_price", "realized_pnl", "status", "close_reason",
                "closed_at", "updated_at",
            ))
        else:
            position.current_price = price
            position.unrealized_pnl = _pnl(position, price)
            position.save(update_fields=("current_price", "unrealized_pnl", "updated_at"))


def _open_position(
    runtime, symbol, timeframe, candle_time, direction, price, client=None,
    signal_stop=None,
):
    strategy = runtime.strategy
    if runtime.positions.filter(status=StrategyPosition.Status.OPEN).count() >= runtime.max_open_positions:
        return None
    if StrategyPosition.objects.filter(
        runtime__user=runtime.user, symbol=symbol, status=StrategyPosition.Status.OPEN
    ).exists():
        return None
    used_budget = runtime.positions.filter(status=StrategyPosition.Status.OPEN).aggregate(
        total=Sum("margin_usdt")
    )["total"] or Decimal("0")
    if used_budget + runtime.allocation_per_order > runtime.total_budget:
        return None
    daily_loss = runtime.positions.filter(
        status=StrategyPosition.Status.CLOSED,
        closed_at__date=timezone.localdate(),
        realized_pnl__lt=0,
    ).aggregate(total=Sum("realized_pnl"))["total"] or Decimal("0")
    if abs(daily_loss) >= runtime.max_daily_loss:
        runtime.status = StrategyRuntime.Status.PAUSED
        runtime.last_error = "Daily strategy loss guard reached; runtime paused."
        runtime.save(update_fields=("status", "last_error", "updated_at"))
        return None

    market = client or BinanceService()
    context = market.get_symbol_context(symbol, include_symbols=False)
    leverage = runtime.leverage
    if client:
        balance = client.get_usdt_balance()
        if runtime.allocation_per_order > balance:
            raise BinanceServiceError("Strategy allocation exceeds available Futures balance.")
        brackets = client.get_leverage_brackets(symbol)
        leverage = min(
            leverage,
            get_bracket_for_notional(brackets, runtime.allocation_per_order * leverage)["initial_leverage"],
        )
    quantity = _floor_step(
        runtime.allocation_per_order * leverage / price,
        context["volume_step"],
    )
    if quantity < context["min_volume"] or quantity * price < context["min_notional"]:
        raise BinanceServiceError("Strategy allocation is below Binance minimum order size.")
    stop = Decimal(str(signal_stop)) if signal_stop is not None else None
    valid_signal_stop = stop is not None and (
        (direction == "LONG" and stop < price) or (direction == "SHORT" and stop > price)
    )
    risk = abs(price - stop) if valid_signal_stop else price * strategy.stop_loss_percent / Decimal("100")
    stop = stop if valid_signal_stop else (price - risk if direction == "LONG" else price + risk)
    target = (
        price + risk * strategy.risk_reward_ratio
        if direction == "LONG" else price - risk * strategy.risk_reward_ratio
    )
    position = StrategyPosition.objects.create(
        runtime=runtime, symbol=symbol, timeframe=timeframe, direction=direction,
        entry_price=price, current_price=price, quantity=quantity, leverage=leverage,
        margin_usdt=runtime.allocation_per_order, stop_loss=stop, take_profit=target,
        signal_candle_time=candle_time,
    )
    if runtime.mode == StrategyRuntime.Mode.PAPER:
        return position
    if not settings.STRATEGY_LIVE_ENABLED:
        position.status = StrategyPosition.Status.FAILED
        position.error = "Live strategy execution is disabled by the server kill-switch."
        position.save(update_fields=("status", "error", "updated_at"))
        return position
    side = "BUY" if direction == "LONG" else "SELL"
    close_side = "SELL" if direction == "LONG" else "BUY"
    try:
        client.change_initial_leverage(symbol, leverage)
        entry = client.place_futures_order(
            symbol=symbol, side=side, type="MARKET", quantity=str(quantity),
            newOrderRespType="RESULT", newClientOrderId=f"st-{position.id.hex[:28]}",
        )
        position.entry_order_id = str(entry.get("orderId", ""))
        fill_price = Decimal(str(entry.get("avgPrice") or entry.get("price") or price))
        if fill_price > 0:
            live_risk = risk if valid_signal_stop else fill_price * strategy.stop_loss_percent / Decimal("100")
            stop = fill_price - live_risk if direction == "LONG" else fill_price + live_risk
            target = (
                fill_price + live_risk * strategy.risk_reward_ratio
                if direction == "LONG" else fill_price - live_risk * strategy.risk_reward_ratio
            )
            position.entry_price = fill_price
            position.current_price = fill_price
            position.stop_loss = stop
            position.take_profit = target
            position.margin_usdt = quantity * fill_price / leverage
        position.stop_order_id = str(client.place_futures_algo_order(
            symbol=symbol, side=close_side, type="STOP_MARKET", triggerPrice=str(stop),
            closePosition="true", workingType="MARK_PRICE",
        ).get("algoId", ""))
        position.take_profit_order_id = str(client.place_futures_algo_order(
            symbol=symbol, side=close_side, type="TAKE_PROFIT_MARKET", triggerPrice=str(target),
            closePosition="true", workingType="MARK_PRICE",
        ).get("algoId", ""))
        position.save(update_fields=(
            "entry_price", "current_price", "margin_usdt", "stop_loss", "take_profit",
            "entry_order_id", "stop_order_id", "take_profit_order_id", "updated_at"
        ))
    except Exception as exc:
        try:
            if position.entry_order_id:
                client.place_futures_order(
                    symbol=symbol, side=close_side, type="MARKET", quantity=str(quantity),
                    reduceOnly="true",
                )
        except Exception:
            pass
        position.status = StrategyPosition.Status.FAILED
        position.error = str(exc)[:500]
        position.save(update_fields=("status", "error", "updated_at"))
    return position


def process_runtime(runtime):
    if runtime.status != StrategyRuntime.Status.ACTIVE:
        return
    client = None
    if runtime.mode == StrategyRuntime.Mode.LIVE:
        account = get_binance_account(runtime.user)
        if not account or account.status != ExchangeAccount.Status.CONNECTED:
            runtime.status = StrategyRuntime.Status.ERROR
            runtime.last_error = "Connect and verify Binance before running a LIVE strategy."
            runtime.save(update_fields=("status", "last_error", "updated_at"))
            return
        client = build_binance_service(account)
    try:
        update_runtime_positions(runtime, client)
        market = client or BinanceService()
        cursors = dict(runtime.last_candles or {})
        for symbol in runtime.symbols:
            for timeframe in runtime.timeframes:
                key = f"{symbol}:{timeframe}"
                previous_close = int(cursors.get(key, 0))
                next_due = previous_close + TIMEFRAME_MILLISECONDS.get(timeframe, 0) + 1_000
                if previous_close and int(timezone.now().timestamp() * 1000) < next_due:
                    continue
                rows = market.get_futures_klines(symbol, timeframe, limit=600)
                candles = parse_klines(rows)
                if len(candles) < 50:
                    continue
                index = len(candles) - 2
                candle = candles[index]
                if int(cursors.get(key, 0)) >= candle["close_time"]:
                    continue
                series = build_series(candles, runtime.strategy.parsed_spec)
                long_signal = evaluate_condition(
                    runtime.strategy.parsed_spec["long_condition"], series, index
                )
                short_signal = evaluate_condition(
                    runtime.strategy.parsed_spec["short_condition"], series, index
                )
                cursors[key] = candle["close_time"]
                if long_signal != short_signal:
                    signal_stop = None
                    if runtime.strategy.parsed_spec.get("engine") == "supertrend_bos_v1":
                        band_name = "upBand" if long_signal else "downBand"
                        signal_stop = series[band_name][index]
                    _open_position(
                        runtime, symbol, timeframe, candle["close_time"],
                        "LONG" if long_signal else "SHORT",
                        Decimal(str(candle["close"])), client, signal_stop,
                    )
        runtime.last_candles = cursors
        runtime.last_error = ""
        runtime.save(update_fields=("last_candles", "last_error", "updated_at"))
    except Exception as exc:
        runtime.last_error = str(exc)[:500]
        runtime.save(update_fields=("last_error", "updated_at"))
