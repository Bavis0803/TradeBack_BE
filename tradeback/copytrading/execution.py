from datetime import timedelta
from decimal import Decimal, ROUND_DOWN

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from exchanges.models import ExchangeAccount, TradeLog
from exchanges.services import BinanceService, BinanceServiceError, decimal_to_string, get_bracket_for_notional

from .models import CopyExecution, CopyStrategy, TelegramMessage, TradeSignal
from .parser import SignalParseError, parse_signal, signal_symbol_hint


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


def _accepted_entry_range(strategy, signal):
    tolerance = Decimal(str(strategy.entry_tolerance_percent)) / Decimal("100")
    return (
        signal.entry_low * (Decimal("1") - tolerance),
        signal.entry_high * (Decimal("1") + tolerance),
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


@transaction.atomic
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

    try:
        parsed = parse_signal(message.text)
    except SignalParseError:
        direct_parse_failed = True
        parsed = None
    if parsed is None:
        parsed = _multipart_signal(strategy, message)
        multipart = parsed is not None
    if parsed is None:
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
        parser_version="chn-v2-multi" if multipart else "chn-v2",
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
        position_status=CopyExecution.PositionStatus.OPEN,
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
    if not paper_replay and not accepted_low <= price <= accepted_high:
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

    binance_symbol_max = max(item["initial_leverage"] for item in brackets)
    strategy_cap = binance_symbol_max if strategy.use_binance_max_leverage else strategy.max_leverage
    requested = signal.requested_leverage or strategy_cap
    leverage = min(requested, strategy_cap, 125)
    tentative_notional = allocation * leverage
    leverage = min(leverage, get_bracket_for_notional(brackets, tentative_notional)["initial_leverage"])
    notional = allocation * leverage
    quantity = _floor_step(notional / price, context["volume_step"])
    take_profit = Decimal(str(signal.take_profits[0]))
    if quantity < context["min_volume"] or price * quantity < context["min_notional"]:
        return _skip(strategy, signal, price, "Allocation is below Binance minimum order size.")

    if strategy.mode == CopyStrategy.Mode.PAPER:
        execution = CopyExecution.objects.create(
            strategy=strategy, signal=signal, status=CopyExecution.Status.PAPER_FILLED,
            position_status=CopyExecution.PositionStatus.OPEN,
            symbol=signal.symbol, direction=signal.direction, entry_price=price,
            quantity=quantity, leverage=leverage, margin_usdt=allocation,
            stop_loss=signal.stop_loss, take_profit=take_profit,
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
    )
    try:
        client.change_initial_leverage(signal.symbol, leverage)
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
