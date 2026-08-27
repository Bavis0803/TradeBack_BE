from decimal import Decimal
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from exchanges.services import BinanceService, BinanceServiceError, decimal_to_string

from .execution import _binance_for_user, _floor_step, protect_live_execution
from .models import CopyExecution, CopyStrategy


def _pnl(execution, price, quantity=None):
    multiplier = Decimal("1") if execution.direction == "LONG" else Decimal("-1")
    sized_quantity = quantity if quantity is not None else execution.quantity
    return (price - execution.entry_price) * sized_quantity * multiplier


def _position_direction(amount):
    return "LONG" if Decimal(str(amount)) > 0 else "SHORT"


def _live_position_map(client):
    return {
        (item["symbol"], _position_direction(item.get("positionAmt", "0"))): item
        for item in client.get_futures_positions()
        if Decimal(str(item.get("positionAmt", "0"))) != 0
    }


def _live_close_details(execution, client, fallback_price):
    """Resolve an actual Binance exit from fills instead of guessing from a mark snapshot."""
    closing_side = "SELL" if execution.direction == "LONG" else "BUY"
    cutoff = int(execution.created_at.timestamp() * 1000) - 1000
    try:
        trades = client.get_futures_user_trades(execution.symbol, limit=1000)
    except BinanceServiceError:
        trades = []
    remaining = execution.quantity
    closed_quantity = Decimal("0")
    exit_notional = Decimal("0")
    realized = Decimal("0")
    for trade in sorted(trades, key=lambda item: int(item.get("time") or 0)):
        if int(trade.get("time") or 0) < cutoff or trade.get("side") != closing_side:
            continue
        trade_quantity = Decimal(str(trade.get("qty") or "0"))
        if trade_quantity <= 0 or remaining <= 0:
            continue
        used_quantity = min(trade_quantity, remaining)
        ratio = used_quantity / trade_quantity
        exit_notional += Decimal(str(trade.get("price") or fallback_price)) * used_quantity
        realized += Decimal(str(trade.get("realizedPnl") or "0")) * ratio
        closed_quantity += used_quantity
        remaining -= used_quantity
    if closed_quantity:
        exit_price = exit_notional / closed_quantity
        target_near = abs(exit_price - execution.take_profit) / execution.take_profit <= Decimal("0.005")
        stop_near = abs(exit_price - execution.stop_loss) / execution.stop_loss <= Decimal("0.005")
        if execution.direction == "LONG" and (exit_price >= execution.take_profit or target_near):
            reason = "TAKE_PROFIT"
        elif execution.direction == "SHORT" and (exit_price <= execution.take_profit or target_near):
            reason = "TAKE_PROFIT"
        elif execution.direction == "LONG" and (exit_price <= execution.stop_loss or stop_near):
            reason = "STOP_LOSS"
        elif execution.direction == "SHORT" and (exit_price >= execution.stop_loss or stop_near):
            reason = "STOP_LOSS"
        else:
            reason = "BINANCE_SYNC"
        return exit_price, realized, reason
    return fallback_price, _pnl(execution, fallback_price), "BINANCE_SYNC"


@transaction.atomic
def _finalize_live_close(execution, exit_price, realized_pnl, reason):
    execution = CopyExecution.objects.select_for_update().get(pk=execution.pk)
    if execution.position_status == CopyExecution.PositionStatus.CLOSED:
        return execution
    execution.exit_price = exit_price
    execution.realized_pnl = realized_pnl
    execution.position_status = CopyExecution.PositionStatus.CLOSED
    execution.close_reason = reason
    execution.closed_at = timezone.now()
    execution.binance_missing_since = None
    execution.save(update_fields=(
        "exit_price", "realized_pnl", "position_status", "close_reason", "closed_at",
        "binance_missing_since", "updated_at",
    ))
    if execution.trade_log_id:
        execution.trade_log.realized_pnl = realized_pnl
        execution.trade_log.save(update_fields=("realized_pnl", "updated_at"))
    return execution


def _close_live_from_binance(execution, client, fallback_price):
    return _finalize_live_close(
        execution, *_live_close_details(execution, client, fallback_price)
    )


@transaction.atomic
def close_paper_position(execution, exit_price, reason):
    execution = CopyExecution.objects.select_for_update().get(pk=execution.pk)
    if execution.position_status != CopyExecution.PositionStatus.OPEN:
        return execution
    execution.exit_price = exit_price
    remaining = execution.remaining_quantity or execution.quantity
    execution.realized_pnl += _pnl(execution, exit_price, remaining)
    execution.remaining_quantity = Decimal("0")
    execution.position_status = CopyExecution.PositionStatus.CLOSED
    execution.close_reason = reason
    execution.closed_at = timezone.now()
    execution.save(update_fields=(
        "exit_price", "realized_pnl", "remaining_quantity", "position_status",
        "close_reason", "closed_at", "updated_at",
    ))
    if execution.trade_log_id:
        execution.trade_log.realized_pnl = execution.realized_pnl
        execution.trade_log.save(update_fields=("realized_pnl", "updated_at"))
    return execution


def _paper_trigger(execution, mark_price):
    if execution.break_even_activated_at:
        if execution.direction == "LONG" and mark_price <= execution.entry_price:
            return execution.entry_price, "BREAK_EVEN"
        if execution.direction == "SHORT" and mark_price >= execution.entry_price:
            return execution.entry_price, "BREAK_EVEN"
        return None
    if execution.direction == "LONG":
        if mark_price <= execution.stop_loss:
            return execution.stop_loss, "STOP_LOSS"
        if mark_price >= execution.take_profit:
            return execution.take_profit, "TP1_PARTIAL"
    else:
        if mark_price >= execution.stop_loss:
            return execution.stop_loss, "STOP_LOSS"
        if mark_price <= execution.take_profit:
            return execution.take_profit, "TP1_PARTIAL"
    return None


@transaction.atomic
def _take_paper_tp1(execution):
    execution = CopyExecution.objects.select_for_update().get(pk=execution.pk)
    if execution.break_even_activated_at or execution.position_status != CopyExecution.PositionStatus.OPEN:
        return execution
    remaining = execution.remaining_quantity or execution.quantity
    closed_quantity = min(execution.take_profit_quantity or remaining, remaining)
    execution.realized_pnl += _pnl(execution, execution.take_profit, closed_quantity)
    execution.remaining_quantity = remaining - closed_quantity
    if execution.remaining_quantity <= 0:
        execution.exit_price = execution.take_profit
        execution.position_status = CopyExecution.PositionStatus.CLOSED
        execution.close_reason = "TAKE_PROFIT"
        execution.closed_at = timezone.now()
    else:
        execution.break_even_activated_at = timezone.now()
        execution.close_reason = "TP1_PARTIAL"
    execution.save(update_fields=(
        "realized_pnl", "remaining_quantity", "exit_price", "position_status",
        "close_reason", "closed_at", "break_even_activated_at", "updated_at",
    ))
    if execution.trade_log_id:
        execution.trade_log.realized_pnl = execution.realized_pnl
        execution.trade_log.save(update_fields=("realized_pnl", "updated_at"))
    return execution


def _cancel_pending(execution, reason="LIMIT_EXPIRED"):
    execution.status = CopyExecution.Status.CANCELLED
    execution.position_status = CopyExecution.PositionStatus.NONE
    execution.close_reason = reason
    execution.closed_at = timezone.now()
    execution.save(update_fields=(
        "status", "position_status", "close_reason", "closed_at", "updated_at",
    ))
    return execution


def _fill_paper_limit(execution):
    execution.status = CopyExecution.Status.PAPER_FILLED
    execution.position_status = CopyExecution.PositionStatus.OPEN
    execution.entry_price = execution.limit_price
    execution.save(update_fields=(
        "status", "position_status", "entry_price", "updated_at",
    ))
    trade_log = execution.trade_log
    if trade_log is None:
        from exchanges.models import TradeLog
        trade_log = TradeLog.objects.create(
            user=execution.strategy.user,
            account=execution.strategy.user.exchange_accounts.filter(
                exchange="BINANCE", status="CONNECTED"
            ).first(),
            source=TradeLog.Source.DRAFT, status=TradeLog.Status.DRAFT,
            market=TradeLog.Market.FUTURES, symbol=execution.symbol,
            side="BUY" if execution.direction == "LONG" else "SELL",
            price=execution.entry_price, quantity=execution.quantity,
            quote_quantity=execution.entry_price * execution.quantity,
            stop_loss=execution.stop_loss, take_profit=execution.take_profit,
            leverage=execution.leverage,
            note=f"Telegram paper limit copy: {execution.strategy.chat_title}",
            executed_at=timezone.now(),
        )
        execution.trade_log = trade_log
        execution.save(update_fields=("trade_log", "updated_at"))
    return execution


def reconcile_pending_entries(user):
    """Advance pending LIMIT orders independently from any open browser tab."""
    pending = list(
        CopyExecution.objects.filter(
            strategy__user=user,
            status=CopyExecution.Status.PENDING_ENTRY,
            position_status=CopyExecution.PositionStatus.PENDING,
        ).select_related("strategy")
    )
    if not pending:
        return

    paper = [item for item in pending if item.strategy.mode == CopyStrategy.Mode.PAPER]
    if paper:
        try:
            marks = BinanceService().get_futures_mark_prices({item.symbol for item in paper})
        except BinanceServiceError:
            marks = {}
        for execution in paper:
            mark = marks.get(execution.symbol)
            fillable = mark is not None and (
                (execution.direction == "LONG" and mark <= execution.limit_price)
                or (execution.direction == "SHORT" and mark >= execution.limit_price)
            )
            if fillable:
                _fill_paper_limit(execution)
            elif execution.entry_expires_at and timezone.now() >= execution.entry_expires_at:
                _cancel_pending(execution)

    live = [item for item in pending if item.strategy.mode == CopyStrategy.Mode.LIVE]
    if not live:
        return
    try:
        _, client = _binance_for_user(user)
    except Exception:
        return
    terminal = {"CANCELED", "EXPIRED", "REJECTED"}
    try:
        live_positions = _live_position_map(client)
    except BinanceServiceError as exc:
        for execution in live:
            execution.error = f"Binance position sync pending: {exc}"[:500]
            execution.save(update_fields=("error", "updated_at"))
        return
    for execution in live:
        try:
            order = client.get_futures_order(execution.symbol, execution.entry_order_id)
            expired = execution.entry_expires_at and timezone.now() >= execution.entry_expires_at
            executed_quantity = Decimal(str(order.get("executedQty") or "0"))
            if (
                (expired or order.get("status") == "PARTIALLY_FILLED")
                and order.get("status") not in {"FILLED", *terminal}
            ):
                client.cancel_futures_order(execution.symbol, execution.entry_order_id)
                order = client.get_futures_order(execution.symbol, execution.entry_order_id)
                executed_quantity = Decimal(str(order.get("executedQty") or executed_quantity))
            if order.get("status") == "FILLED" or (
                order.get("status") in terminal and executed_quantity > 0
            ):
                average_price = Decimal(str(order.get("avgPrice") or "0"))
                if average_price <= 0:
                    average_price = execution.limit_price
                live_position = live_positions.get((execution.symbol, execution.direction))
                if live_position is None:
                    _close_live_from_binance(execution, client, average_price)
                    continue
                remaining_quantity = abs(Decimal(str(live_position.get("positionAmt") or "0")))
                if remaining_quantity <= 0:
                    _close_live_from_binance(execution, client, average_price)
                    continue
                protect_live_execution(
                    execution, client, executed_quantity, average_price,
                    protection_quantity=min(executed_quantity, remaining_quantity),
                )
            elif order.get("status") in terminal or expired:
                _cancel_pending(execution)
        except BinanceServiceError as exc:
            execution.error = str(exc)[:500]
            execution.save(update_fields=("error", "updated_at"))


def reconcile_live_protections(user, client=None, live_positions=None):
    """Move the runner stop to break-even only after Binance confirms TP1 filled."""
    executions = list(
        CopyExecution.objects.filter(
            strategy__user=user,
            strategy__mode=CopyStrategy.Mode.LIVE,
            status=CopyExecution.Status.PROTECTED,
            position_status=CopyExecution.PositionStatus.OPEN,
            break_even_activated_at__isnull=True,
            tp1_close_percent__lt=Decimal("100"),
        ).exclude(take_profit_order_id="").select_related("strategy")
    )
    if not executions:
        return set()
    try:
        if client is None:
            _, client = _binance_for_user(user)
        if live_positions is None:
            live_positions = _live_position_map(client)
    except Exception:
        return set()

    changed = set()
    for execution in executions:
        live = live_positions.get((execution.symbol, execution.direction))
        if live is None:
            continue
        try:
            target = client.get_futures_algo_order(execution.take_profit_order_id)
        except BinanceServiceError as exc:
            message = f"TP1 status sync pending: {exc}"[:500]
            if execution.error != message:
                execution.error = message
                execution.save(update_fields=("error", "updated_at"))
            continue
        filled_quantity = Decimal(str(target.get("actualQty") or "0"))
        if target.get("algoStatus") != "FINISHED" or filled_quantity <= 0:
            continue
        remaining_quantity = abs(Decimal(str(live.get("positionAmt") or "0")))
        if remaining_quantity <= 0:
            continue
        _activate_live_break_even(execution, client, remaining_quantity)
        changed.add(execution.pk)
    return changed


@transaction.atomic
def _activate_live_break_even(execution, client, remaining_quantity):
    execution = CopyExecution.objects.select_for_update().select_related("strategy").get(
        pk=execution.pk
    )
    if execution.break_even_activated_at:
        return execution
    closing_side = "SELL" if execution.direction == "LONG" else "BUY"
    old_stop_order_id = execution.stop_order_id
    try:
        context = client.get_symbol_context(execution.symbol, include_symbols=False)
        break_even_price = _floor_step(execution.entry_price, context.get("price_step"))
        stop = client.place_futures_algo_order(
            algoType="CONDITIONAL", symbol=execution.symbol, side=closing_side,
            type="STOP_MARKET", triggerPrice=decimal_to_string(break_even_price),
            quantity=decimal_to_string(remaining_quantity), reduceOnly="true",
            workingType="MARK_PRICE",
        )
    except BinanceServiceError as exc:
        execution.error = f"Could not move SL to break-even: {exc}"[:500]
        try:
            client.place_futures_order(
                symbol=execution.symbol, side=closing_side, type="MARKET",
                quantity=decimal_to_string(remaining_quantity), reduceOnly="true",
            )
            execution.error = (
                execution.error + " Runner was emergency-closed to preserve capital."
            )[:500]
        except BinanceServiceError:
            execution.error = (
                execution.error + " Original SL remains active; check Binance immediately."
            )[:500]
        execution.save(update_fields=("error", "updated_at"))
        return execution

    execution.break_even_stop_order_id = str(stop.get("algoId", ""))
    execution.break_even_activated_at = timezone.now()
    execution.remaining_quantity = remaining_quantity
    execution.error = ""
    execution.save(update_fields=(
        "break_even_stop_order_id", "break_even_activated_at", "remaining_quantity",
        "error", "updated_at",
    ))
    if old_stop_order_id:
        try:
            client.cancel_futures_algo_order(old_stop_order_id)
        except BinanceServiceError as exc:
            execution.error = (
                f"Break-even SL is active, but the original SL could not be cancelled: {exc}"
            )[:500]
            execution.save(update_fields=("error", "updated_at"))
    return execution


def cancel_pending_entry(execution):
    execution.entry_expires_at = timezone.now()
    execution.save(update_fields=("entry_expires_at", "updated_at"))
    reconcile_pending_entries(execution.strategy.user)
    execution.refresh_from_db()
    return execution


def get_position_payload(user, strategy_id=None):
    rows = CopyExecution.objects.filter(strategy__user=user).select_related("strategy", "signal")
    if strategy_id:
        rows = rows.filter(strategy_id=strategy_id)
    pending_rows = list(rows.filter(position_status=CopyExecution.PositionStatus.PENDING).order_by("-created_at"))
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
            live_positions = _live_position_map(client)
            live_sync_ok = True
            changed = reconcile_live_protections(user, client, live_positions)
            for execution in open_rows:
                if execution.pk in changed:
                    execution.refresh_from_db()
        except Exception:
            live_positions = {}

    snapshots = []
    now = timezone.now()
    missing_grace = timedelta(
        seconds=max(
            int(getattr(settings, "COPY_TRADING_POSITION_MISSING_GRACE_SECONDS", 45)),
            10,
        )
    )
    for execution in open_rows:
        live = (
            live_positions.get((execution.symbol, execution.direction))
            if execution.strategy.mode == CopyStrategy.Mode.LIVE else None
        )
        mark = Decimal(str(live.get("markPrice"))) if live else marks.get(execution.symbol, execution.entry_price)
        if execution.strategy.mode == CopyStrategy.Mode.LIVE and live_sync_ok:
            if live is not None:
                if execution.binance_missing_since is not None or execution.last_binance_seen_at is None:
                    execution.binance_missing_since = None
                    execution.last_binance_seen_at = now
                    execution.save(update_fields=(
                        "binance_missing_since", "last_binance_seen_at", "updated_at",
                    ))
            else:
                if execution.binance_missing_since is None:
                    execution.binance_missing_since = now
                    execution.save(update_fields=("binance_missing_since", "updated_at"))
                elif now - execution.binance_missing_since >= missing_grace:
                    execution = _close_live_from_binance(execution, client, mark)
                    recent_rows.insert(0, execution)
                    continue
        if execution.strategy.mode == CopyStrategy.Mode.PAPER:
            trigger = _paper_trigger(execution, mark)
            if trigger:
                if trigger[1] == "TP1_PARTIAL":
                    execution = _take_paper_tp1(execution)
                    if execution.position_status == CopyExecution.PositionStatus.CLOSED:
                        recent_rows.insert(0, execution)
                        continue
                else:
                    execution = close_paper_position(execution, *trigger)
                    recent_rows.insert(0, execution)
                    continue
        snapshots.append(_serialize(execution, mark, live))

    return {
        "pending": [_serialize(item, item.limit_price or item.entry_price) for item in pending_rows],
        "open": snapshots,
        "recent": [_serialize(item, item.exit_price or item.entry_price) for item in recent_rows[:10]],
        "updated_at": timezone.now().isoformat(),
    }


def _serialize(execution, mark_price, live=None):
    entry = Decimal(str(live.get("entryPrice"))) if live else execution.entry_price
    quantity = abs(Decimal(str(live.get("positionAmt")))) if live else (
        execution.remaining_quantity or execution.quantity
    )
    pnl = Decimal(str(live.get("unRealizedProfit", "0"))) if live else (
        _pnl(execution, mark_price, quantity)
        if execution.position_status == CopyExecution.PositionStatus.OPEN
        else execution.realized_pnl
    )
    margin = execution.margin_usdt
    roe = pnl / margin * Decimal("100") if margin else Decimal("0")
    leverage = int(live.get("leverage")) if live else execution.leverage
    active_stop_loss = entry if execution.break_even_activated_at else execution.stop_loss
    risk_amount = abs(entry - active_stop_loss) * quantity
    potential_profit = abs(execution.take_profit - entry) * quantity
    risk_reward = potential_profit / risk_amount if risk_amount else Decimal("0")
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
        "risk_amount": decimal_to_string(risk_amount),
        "potential_profit": decimal_to_string(potential_profit),
        "risk_reward_ratio": decimal_to_string(risk_reward),
        "stop_loss": decimal_to_string(active_stop_loss),
        "initial_stop_loss": decimal_to_string(execution.stop_loss),
        "take_profit": decimal_to_string(execution.take_profit),
        "take_profit_quantity": decimal_to_string(execution.take_profit_quantity),
        "tp1_close_percent": decimal_to_string(execution.tp1_close_percent),
        "break_even_active": bool(execution.break_even_activated_at),
        "close_reason": execution.close_reason,
        "opened_at": execution.created_at.isoformat(),
        "closed_at": execution.closed_at.isoformat() if execution.closed_at else None,
        "price_source": "BINANCE_ACCOUNT" if live else "BINANCE_MARK_PRICE",
        "sync_status": (
            "CONFIRMED" if live else
            "RECONNECTING" if (
                execution.strategy.mode == CopyStrategy.Mode.LIVE
                and execution.binance_missing_since is not None
            ) else
            "MARK_PRICE"
        ),
        "binance_missing_since": (
            execution.binance_missing_since.isoformat()
            if execution.binance_missing_since else None
        ),
        "entry_order_type": execution.entry_order_type,
        "limit_price": decimal_to_string(execution.limit_price) if execution.limit_price else None,
        "entry_expires_at": execution.entry_expires_at.isoformat() if execution.entry_expires_at else None,
    }
