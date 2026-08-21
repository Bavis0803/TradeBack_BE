from decimal import Decimal
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from exchanges.services import BinanceService, BinanceServiceError, decimal_to_string

from .execution import _binance_for_user
from .models import CopyExecution, CopyStrategy


def _pnl(execution, price):
    multiplier = Decimal("1") if execution.direction == "LONG" else Decimal("-1")
    return (price - execution.entry_price) * execution.quantity * multiplier


@transaction.atomic
def close_paper_position(execution, exit_price, reason):
    execution = CopyExecution.objects.select_for_update().get(pk=execution.pk)
    if execution.position_status != CopyExecution.PositionStatus.OPEN:
        return execution
    execution.exit_price = exit_price
    execution.realized_pnl = _pnl(execution, exit_price)
    execution.position_status = CopyExecution.PositionStatus.CLOSED
    execution.close_reason = reason
    execution.closed_at = timezone.now()
    execution.save(update_fields=(
        "exit_price", "realized_pnl", "position_status", "close_reason", "closed_at", "updated_at",
    ))
    if execution.trade_log_id:
        execution.trade_log.realized_pnl = execution.realized_pnl
        execution.trade_log.save(update_fields=("realized_pnl", "updated_at"))
    return execution


def _paper_trigger(execution, mark_price):
    if execution.direction == "LONG":
        if mark_price <= execution.stop_loss:
            return execution.stop_loss, "STOP_LOSS"
        if mark_price >= execution.take_profit:
            return execution.take_profit, "TAKE_PROFIT"
    else:
        if mark_price >= execution.stop_loss:
            return execution.stop_loss, "STOP_LOSS"
        if mark_price <= execution.take_profit:
            return execution.take_profit, "TAKE_PROFIT"
    return None


def get_position_payload(user, strategy_id=None):
    rows = CopyExecution.objects.filter(strategy__user=user).select_related("strategy", "signal")
    if strategy_id:
        rows = rows.filter(strategy_id=strategy_id)
    open_rows = list(rows.filter(position_status=CopyExecution.PositionStatus.OPEN).order_by("-created_at"))
    recent_rows = list(rows.filter(position_status=CopyExecution.PositionStatus.CLOSED).order_by("-closed_at")[:10])
    symbols = {item.symbol for item in open_rows}
    try:
        marks = BinanceService().get_futures_mark_prices(symbols) if symbols else {}
    except BinanceServiceError:
        marks = {}

    live_positions = {}
    live_sync_ok = False
    if any(item.strategy.mode == CopyStrategy.Mode.LIVE for item in open_rows):
        try:
            _, client = _binance_for_user(user)
            live_positions = {
                item["symbol"]: item for item in client.get_futures_positions()
                if Decimal(str(item.get("positionAmt", "0"))) != 0
            }
            live_sync_ok = True
        except Exception:
            live_positions = {}

    snapshots = []
    for execution in open_rows:
        live = live_positions.get(execution.symbol) if execution.strategy.mode == CopyStrategy.Mode.LIVE else None
        mark = Decimal(str(live.get("markPrice"))) if live else marks.get(execution.symbol, execution.entry_price)
        if (
            execution.strategy.mode == CopyStrategy.Mode.LIVE
            and live_sync_ok
            and live is None
            and timezone.now() - execution.created_at > timedelta(seconds=10)
        ):
            execution = close_paper_position(execution, mark, "BINANCE_SYNC")
            recent_rows.insert(0, execution)
            continue
        if execution.strategy.mode == CopyStrategy.Mode.PAPER:
            trigger = _paper_trigger(execution, mark)
            if trigger:
                execution = close_paper_position(execution, *trigger)
                recent_rows.insert(0, execution)
                continue
        snapshots.append(_serialize(execution, mark, live))

    return {
        "open": snapshots,
        "recent": [_serialize(item, item.exit_price or item.entry_price) for item in recent_rows[:10]],
        "updated_at": timezone.now().isoformat(),
    }


def _serialize(execution, mark_price, live=None):
    entry = Decimal(str(live.get("entryPrice"))) if live else execution.entry_price
    quantity = abs(Decimal(str(live.get("positionAmt")))) if live else execution.quantity
    pnl = Decimal(str(live.get("unRealizedProfit", "0"))) if live else (
        _pnl(execution, mark_price) if execution.position_status == CopyExecution.PositionStatus.OPEN
        else execution.realized_pnl
    )
    margin = execution.margin_usdt
    roe = pnl / margin * Decimal("100") if margin else Decimal("0")
    leverage = int(live.get("leverage")) if live else execution.leverage
    return {
        "id": str(execution.id),
        "strategy_id": str(execution.strategy_id),
        "chat_title": execution.strategy.chat_title,
        "mode": execution.strategy.mode,
        "status": execution.position_status,
        "execution_status": execution.status,
        "symbol": execution.symbol,
        "direction": execution.direction,
        "entry_price": decimal_to_string(entry),
        "mark_price": decimal_to_string(mark_price),
        "exit_price": decimal_to_string(execution.exit_price) if execution.exit_price else None,
        "quantity": decimal_to_string(quantity),
        "leverage": leverage,
        "margin_usdt": decimal_to_string(margin),
        "notional_usdt": decimal_to_string(mark_price * quantity),
        "unrealized_pnl": decimal_to_string(pnl),
        "roe_percent": decimal_to_string(roe),
        "stop_loss": decimal_to_string(execution.stop_loss),
        "take_profit": decimal_to_string(execution.take_profit),
        "close_reason": execution.close_reason,
        "opened_at": execution.created_at.isoformat(),
        "closed_at": execution.closed_at.isoformat() if execution.closed_at else None,
        "price_source": "BINANCE_ACCOUNT" if live else "BINANCE_MARK_PRICE",
    }
