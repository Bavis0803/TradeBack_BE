from datetime import timedelta
from decimal import Decimal, ROUND_DOWN

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from exchanges.models import ExchangeAccount, TradeLog
from exchanges.services import (
    BinanceService, BinanceServiceError, calculate_risk_sized_order, decimal_to_string,
)

from .models import CopyExecution, CopyStrategy, SignalCandidate, TelegramMessage, TradeSignal
from .ai_detection import analyze_signal_candidate
from .parser import SignalParseError, parse_signal, parse_signal_candidate, signal_symbol_hint


MULTIPART_SIGNAL_WINDOW = timedelta(minutes=5)


def _binance_for_user(user):
    account = ExchangeAccount.objects.select_related("credential").get(
        user=user, exchange=ExchangeAccount.Exchange.BINANCE, status=ExchangeAccount.Status.CONNECTED
    )
    return account, BinanceService(
        account.credential.api_key, account.credential.api_secret, account.is_testnet
    )


def _floor_step(value, step):
    if not step:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _limit_price(signal, context):
    # Use the edge nearest the breakout so a retrace has the highest safe fill chance.
    raw = signal.entry_high if signal.direction == "LONG" else signal.entry_low
    return _floor_step(raw, context.get("price_step"))


def _accepted_entry_range(strategy, signal):
    tolerance = Decimal(str(strategy.entry_tolerance_percent)) / Decimal("100")
    return (
        signal.entry_low * (Decimal("1") - tolerance),
        signal.entry_high * (Decimal("1") + tolerance),
    )


def _resolve_entry_order_type(strategy, signal, price, accepted_low, accepted_high, paper_replay):
    if paper_replay:
        return CopyStrategy.EntryOrderType.MARKET
    if strategy.entry_order_type != CopyStrategy.EntryOrderType.SMART:
        return strategy.entry_order_type
    slipped_against_trade = (
        signal.direction == "LONG" and price > accepted_high
    ) or (
        signal.direction == "SHORT" and price < accepted_low
    )
    return (
        CopyStrategy.EntryOrderType.LIMIT
        if slipped_against_trade else CopyStrategy.EntryOrderType.MARKET
    )


def _skip(strategy, signal, entry, message):
    return CopyExecution.objects.create(
        strategy=strategy,
        signal=signal,
        status=CopyExecution.Status.SKIPPED,
        symbol=signal.symbol,
        direction=signal.direction,
        entry_price=entry,
        stop_loss=signal.stop_loss,
        take_profit=Decimal(str(signal.take_profits[0])),
        error=message[:500],
    )


def process_telegram_message(
    strategy, telegram_message_id, text, sent_at, sender_name="", execute=True
):
    message, created = TelegramMessage.objects.get_or_create(
        strategy=strategy,
        telegram_message_id=telegram_message_id,
        defaults={"text": text or "", "sent_at": sent_at, "sender_name": sender_name},
    )
    if not created:
        return message, getattr(message, "signal", None), None

    return _process_saved_message(strategy, message, execute)


def _multipart_signal(strategy, message):
    symbol = signal_symbol_hint(message.text)
    if not symbol:
        return None

    candidates = TelegramMessage.objects.filter(
        strategy=strategy,
        telegram_message_id__lt=message.telegram_message_id,
        sent_at__gte=message.sent_at - MULTIPART_SIGNAL_WINDOW,
        sent_at__lte=message.sent_at,
        parse_status__in=(
            TelegramMessage.ParseStatus.IGNORED,
            TelegramMessage.ParseStatus.INVALID,
        ),
    ).order_by("-telegram_message_id")[:5]
    for candidate in candidates:
        if signal_symbol_hint(candidate.text) != symbol:
            continue
        if (
            message.sender_name
            and candidate.sender_name
            and message.sender_name != candidate.sender_name
        ):
            continue
        try:
            parsed = parse_signal(f"{candidate.text}\n{message.text}")
        except SignalParseError:
            continue
        if parsed and parsed.symbol == symbol:
            return parsed
    return None


def _process_saved_message(strategy, message, execute):
    direct_parse_failed = False
    multipart = False
    ai_parsed = False

    try:
        parsed = parse_signal(message.text)
    except SignalParseError:
        direct_parse_failed = True
        parsed = None
    if parsed is None:
        parsed = _multipart_signal(strategy, message)
        multipart = parsed is not None
    if parsed is None and execute:
        parsed = analyze_signal_candidate(strategy, message.text)
        ai_parsed = parsed is not None
    if parsed is None:
        candidate = parse_signal_candidate(message.text) if execute else None
        if candidate:
            message.parse_status = TelegramMessage.ParseStatus.REVIEW
            message.save(update_fields=("parse_status",))
            SignalCandidate.objects.create(
                message=message,
                symbol=candidate.symbol,
                direction=candidate.direction,
                target_hint=candidate.target_hint,
                reason=candidate.reason,
            )
            return message, None, None
        if direct_parse_failed:
            message.parse_status = TelegramMessage.ParseStatus.INVALID
            message.save(update_fields=("parse_status",))
        return message, None, None

    message.parse_status = TelegramMessage.ParseStatus.SIGNAL
    message.save(update_fields=("parse_status",))
    signal = TradeSignal.objects.create(
        message=message,
        symbol=parsed.symbol,
        direction=parsed.direction,
        entry_low=parsed.entry_low,
        entry_high=parsed.entry_high,
        stop_loss=parsed.stop_loss,
        take_profits=[decimal_to_string(value) for value in parsed.take_profits],
        requested_leverage=parsed.leverage,
        parser_version=("ai-signal-v1" if ai_parsed else "chn-v2-multi" if multipart else "chn-v2"),
    )
    execution = execute_signal(strategy, signal) if execute else None
    return message, signal, execution


@transaction.atomic
def reprocess_saved_message(message, execute=False):
    """Parse an already persisted message after parser upgrades without duplicating it."""
    locked = TelegramMessage.objects.select_for_update().select_related("strategy").get(pk=message.pk)
    existing = getattr(locked, "signal", None)
    if existing:
        return locked, existing, None
    return _process_saved_message(locked.strategy, locked, execute)


def execute_signal(strategy, signal, paper_replay=False):
    allocation = Decimal(str(strategy.allocation_usdt))
    daily_loss_limit = Decimal(str(strategy.max_daily_loss_usdt))
    existing = CopyExecution.objects.filter(strategy=strategy, signal=signal).first()
    if existing:
        if (
            paper_replay
            and strategy.mode == CopyStrategy.Mode.PAPER
            and existing.status == CopyExecution.Status.SKIPPED
            and existing.position_status == CopyExecution.PositionStatus.NONE
        ):
            existing.delete()
        else:
            return existing
    if strategy.status != CopyStrategy.Status.ACTIVE:
        return _skip(strategy, signal, signal.entry_low, "Strategy is paused.")
    if strategy.allowed_symbols and signal.symbol not in strategy.allowed_symbols:
        return _skip(strategy, signal, signal.entry_low, "Symbol is not in the strategy allowlist.")
    if CopyExecution.objects.filter(
        strategy__user=strategy.user,
        symbol=signal.symbol,
        position_status__in=(
            CopyExecution.PositionStatus.OPEN, CopyExecution.PositionStatus.PENDING,
        ),
    ).exists():
        return _skip(strategy, signal, signal.entry_low, "An open copy position already exists for this symbol.")

    if strategy.mode == CopyStrategy.Mode.PAPER:
        account = ExchangeAccount.objects.filter(
            user=strategy.user,
            exchange=ExchangeAccount.Exchange.BINANCE,
            status=ExchangeAccount.Status.CONNECTED,
        ).first()
        balance = allocation
        brackets = [{
            "initial_leverage": strategy.max_leverage,
            "notional_floor": Decimal("0"),
            "notional_cap": Decimal("1E50"),
            "maint_margin_ratio": Decimal("0.004"),
        }]
        if account:
            try:
                _, signed_client = _binance_for_user(strategy.user)
                context = signed_client.get_symbol_context(signal.symbol, include_symbols=False)
                brackets = signed_client.get_leverage_brackets(signal.symbol)
            except BinanceServiceError as exc:
                return _skip(strategy, signal, signal.entry_low, str(exc))
        else:
            try:
                context = BinanceService().get_symbol_context(signal.symbol, include_symbols=False)
            except BinanceServiceError as exc:
                return _skip(strategy, signal, signal.entry_low, str(exc))
    else:
        try:
            account, client = _binance_for_user(strategy.user)
            context = client.get_symbol_context(signal.symbol, include_symbols=False)
            brackets = client.get_leverage_brackets(signal.symbol)
            balance = client.get_usdt_balance()
        except (ExchangeAccount.DoesNotExist, BinanceServiceError) as exc:
            return _skip(strategy, signal, signal.entry_low, str(exc))

    market_price = context["current_price"]
    price = signal.entry_low if paper_replay and strategy.mode == CopyStrategy.Mode.PAPER else market_price
    accepted_low, accepted_high = _accepted_entry_range(strategy, signal)
    if (
        strategy.entry_order_type == CopyStrategy.EntryOrderType.MARKET
        and not paper_replay and not accepted_low <= price <= accepted_high
    ):
        return _skip(
            strategy,
            signal,
            price,
            (
                f"Current Binance price {decimal_to_string(price)} is outside entry "
                f"{decimal_to_string(signal.entry_low)}-{decimal_to_string(signal.entry_high)} "
                f"with {decimal_to_string(strategy.entry_tolerance_percent)}% tolerance "
                f"(accepted {decimal_to_string(accepted_low)}-{decimal_to_string(accepted_high)})."
            ),
        )
    first_target = Decimal(str(signal.take_profits[0]))
    risk_structure_valid = (
        signal.stop_loss < price < first_target
        if signal.direction == "LONG"
        else first_target < price < signal.stop_loss
    )
    if not paper_replay and not risk_structure_valid:
        return _skip(
            strategy,
            signal,
            price,
            "Current price has crossed the signal stop-loss or first target.",
        )
    if allocation > balance:
        return _skip(strategy, signal, price, "Per-order allocation exceeds available Futures balance.")
    if allocation > Decimal(str(settings.COPY_TRADING_MAX_ALLOCATION_USDT)):
        return _skip(strategy, signal, price, "Per-order allocation exceeds the server safety limit.")

    order_type = _resolve_entry_order_type(
        strategy, signal, price, accepted_low, accepted_high, paper_replay
    )
    order_price = price if order_type == CopyStrategy.EntryOrderType.MARKET else _limit_price(signal, context)
    take_profit = Decimal(str(signal.take_profits[0]))
    leverage_cap = (
        int(settings.COPY_TRADING_AUTO_LEVERAGE_CAP)
        if strategy.use_binance_max_leverage else strategy.max_leverage
    )
    try:
        sizing = calculate_risk_sized_order(
            {
                "direction": signal.direction, "entry_price": order_price,
                "stop_loss": signal.stop_loss, "take_profit": take_profit,
            },
            allocation, context, brackets, leverage_cap=leverage_cap,
            requested_leverage=signal.requested_leverage,
        )
    except ValueError as exc:
        return _skip(strategy, signal, price, str(exc))
    leverage = sizing["leverage"]
    quantity = Decimal(sizing["volume"])

    limit_marketable = (
        signal.direction == "LONG" and price <= order_price
    ) or (
        signal.direction == "SHORT" and price >= order_price
    )
    if strategy.mode == CopyStrategy.Mode.PAPER and (
        order_type == CopyStrategy.EntryOrderType.MARKET or limit_marketable
    ):
        execution = CopyExecution.objects.create(
            strategy=strategy, signal=signal, status=CopyExecution.Status.PAPER_FILLED,
            position_status=CopyExecution.PositionStatus.OPEN,
            symbol=signal.symbol, direction=signal.direction, entry_price=price,
            quantity=quantity, leverage=leverage, margin_usdt=allocation,
            stop_loss=signal.stop_loss, take_profit=take_profit,
            entry_order_type=order_type,
        )
        trade_log = TradeLog.objects.create(
            user=strategy.user, account=account, source=TradeLog.Source.DRAFT,
            status=TradeLog.Status.DRAFT, market=TradeLog.Market.FUTURES,
            symbol=signal.symbol, side="BUY" if signal.direction == "LONG" else "SELL",
            price=price, quantity=quantity, quote_quantity=price * quantity,
            stop_loss=signal.stop_loss, take_profit=take_profit, leverage=leverage,
            note=f"Telegram paper {'replay' if paper_replay else 'copy'}: {strategy.chat_title}",
            executed_at=timezone.now(),
        )
        execution.trade_log = trade_log
        execution.save(update_fields=("trade_log", "updated_at"))
        return execution

    if strategy.mode == CopyStrategy.Mode.PAPER:
        return CopyExecution.objects.create(
            strategy=strategy, signal=signal, status=CopyExecution.Status.PENDING_ENTRY,
            position_status=CopyExecution.PositionStatus.PENDING,
            symbol=signal.symbol, direction=signal.direction, entry_price=order_price,
            limit_price=order_price, quantity=quantity, leverage=leverage,
            margin_usdt=allocation, stop_loss=signal.stop_loss, take_profit=take_profit,
            entry_order_type=CopyStrategy.EntryOrderType.LIMIT,
            entry_expires_at=timezone.now() + timedelta(minutes=strategy.limit_expiry_minutes),
        )

    if not settings.COPY_TRADING_LIVE_ENABLED:
        return _skip(strategy, signal, price, "Live copy trading is disabled by the server kill-switch.")
    daily_pnl = TradeLog.objects.filter(
        user=strategy.user, source=TradeLog.Source.BINANCE,
        executed_at__date=timezone.localdate(), realized_pnl__lt=0,
    ).aggregate(total=Sum("realized_pnl"))["total"] or Decimal("0")
    if abs(daily_pnl) >= daily_loss_limit:
        return _skip(strategy, signal, price, "Daily loss limit has been reached.")

    side = "BUY" if signal.direction == "LONG" else "SELL"
    closing_side = "SELL" if side == "BUY" else "BUY"
    execution = CopyExecution.objects.create(
        strategy=strategy, signal=signal, status=CopyExecution.Status.SUBMITTED,
        symbol=signal.symbol, direction=signal.direction, entry_price=price,
        quantity=quantity, leverage=leverage, margin_usdt=allocation,
        stop_loss=signal.stop_loss, take_profit=take_profit,
        entry_order_type=order_type,
        limit_price=order_price if order_type == CopyStrategy.EntryOrderType.LIMIT else None,
        entry_expires_at=(
            timezone.now() + timedelta(minutes=strategy.limit_expiry_minutes)
            if order_type == CopyStrategy.EntryOrderType.LIMIT else None
        ),
    )
    try:
        client.change_initial_leverage(signal.symbol, leverage)
        if order_type == CopyStrategy.EntryOrderType.LIMIT:
            entry = client.place_futures_order(
                symbol=signal.symbol, side=side, type="LIMIT", timeInForce="GTD",
                price=decimal_to_string(order_price), quantity=decimal_to_string(quantity),
                goodTillDate=int(execution.entry_expires_at.timestamp() * 1000),
                newOrderRespType="RESULT", newClientOrderId=f"tb-{execution.id.hex[:28]}",
            )
            execution.entry_order_id = str(entry.get("orderId", ""))
            execution.status = CopyExecution.Status.PENDING_ENTRY
            execution.position_status = CopyExecution.PositionStatus.PENDING
            execution.save()
            if entry.get("status") != "FILLED":
                try:
                    entry = client.get_futures_order(signal.symbol, execution.entry_order_id)
                except BinanceServiceError:
                    pass
            filled_quantity = Decimal(str(entry.get("executedQty") or "0"))
            if entry.get("status") == "PARTIALLY_FILLED" and filled_quantity > 0:
                client.cancel_futures_order(signal.symbol, execution.entry_order_id)
                entry = client.get_futures_order(signal.symbol, execution.entry_order_id)
                filled_quantity = Decimal(str(entry.get("executedQty") or filled_quantity))
            if entry.get("status") == "FILLED" or filled_quantity > 0:
                average_price = Decimal(str(entry.get("avgPrice") or "0"))
                if average_price <= 0:
                    average_price = order_price
                return protect_live_execution(execution, client, filled_quantity, average_price)
            if entry.get("status") in {"CANCELED", "EXPIRED", "REJECTED"}:
                execution.status = CopyExecution.Status.CANCELLED
                execution.position_status = CopyExecution.PositionStatus.NONE
                execution.close_reason = "BINANCE_CANCELLED"
                execution.closed_at = timezone.now()
                execution.save()
            return execution
        entry = client.place_futures_order(
            symbol=signal.symbol, side=side, type="MARKET",
            quantity=decimal_to_string(quantity), newClientOrderId=f"tb-{execution.id.hex[:28]}",
        )
        execution.entry_order_id = str(entry.get("orderId", ""))
        execution.position_status = CopyExecution.PositionStatus.OPEN
        stop = client.place_futures_algo_order(
            algoType="CONDITIONAL", symbol=signal.symbol, side=closing_side,
            type="STOP_MARKET", triggerPrice=decimal_to_string(signal.stop_loss),
            quantity=decimal_to_string(quantity), reduceOnly="true", workingType="MARK_PRICE",
        )
        execution.stop_order_id = str(stop.get("algoId", ""))
        target = client.place_futures_algo_order(
            algoType="CONDITIONAL", symbol=signal.symbol, side=closing_side,
            type="TAKE_PROFIT_MARKET", triggerPrice=decimal_to_string(take_profit),
            quantity=decimal_to_string(quantity), reduceOnly="true", workingType="MARK_PRICE",
        )
        execution.take_profit_order_id = str(target.get("algoId", ""))
        execution.status = CopyExecution.Status.PROTECTED
    except BinanceServiceError as exc:
        execution.status = CopyExecution.Status.FAILED
        execution.error = str(exc)[:500]
        # If entry succeeded but no stop exists, fail closed by flattening immediately.
        if execution.entry_order_id and not execution.stop_order_id:
            try:
                client.place_futures_order(
                    symbol=signal.symbol, side=closing_side, type="MARKET",
                    quantity=decimal_to_string(quantity), reduceOnly="true",
                )
            except BinanceServiceError:
                execution.error = (execution.error + " Emergency close also failed; check Binance now.")[:500]
            else:
                execution.position_status = CopyExecution.PositionStatus.CLOSED
                execution.close_reason = "EMERGENCY_CLOSE"
                execution.closed_at = timezone.now()
    execution.save()
    return execution


@transaction.atomic
def protect_live_execution(
    execution, client, filled_quantity, average_price, protection_quantity=None
):
    """Attach reduce-only protection after a MARKET or LIMIT entry has actually filled."""
    execution = CopyExecution.objects.select_for_update().get(pk=execution.pk)
    if execution.status == CopyExecution.Status.PROTECTED:
        return execution
    side = "BUY" if execution.direction == "LONG" else "SELL"
    closing_side = "SELL" if side == "BUY" else "BUY"
    execution.quantity = filled_quantity
    execution.entry_price = average_price
    execution.position_status = CopyExecution.PositionStatus.OPEN
    execution.status = CopyExecution.Status.SUBMITTED
    guarded_quantity = protection_quantity or filled_quantity
    try:
        stop = client.place_futures_algo_order(
            algoType="CONDITIONAL", symbol=execution.symbol, side=closing_side,
            type="STOP_MARKET", triggerPrice=decimal_to_string(execution.stop_loss),
            quantity=decimal_to_string(guarded_quantity), reduceOnly="true", workingType="MARK_PRICE",
        )
        execution.stop_order_id = str(stop.get("algoId", ""))
        target = client.place_futures_algo_order(
            algoType="CONDITIONAL", symbol=execution.symbol, side=closing_side,
            type="TAKE_PROFIT_MARKET", triggerPrice=decimal_to_string(execution.take_profit),
            quantity=decimal_to_string(guarded_quantity), reduceOnly="true", workingType="MARK_PRICE",
        )
        execution.take_profit_order_id = str(target.get("algoId", ""))
        execution.status = CopyExecution.Status.PROTECTED
        execution.error = ""
    except BinanceServiceError as exc:
        execution.status = CopyExecution.Status.FAILED
        execution.error = str(exc)[:500]
        if not execution.stop_order_id:
            try:
                client.place_futures_order(
                    symbol=execution.symbol, side=closing_side, type="MARKET",
                    quantity=decimal_to_string(guarded_quantity), reduceOnly="true",
                )
            except BinanceServiceError:
                execution.error = (execution.error + " Emergency close also failed; check Binance now.")[:500]
            else:
                execution.position_status = CopyExecution.PositionStatus.CLOSED
                execution.close_reason = "EMERGENCY_CLOSE"
                execution.closed_at = timezone.now()
    execution.save()
    return execution
