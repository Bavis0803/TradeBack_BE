from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from exchanges.models import ExchangeAccount, ExchangeCredential, TradeLog

from .execution import process_telegram_message, reprocess_saved_message
from .ai_detection import is_ai_candidate
from .models import (
    AISignalAgent, AISignalAnalysis, CopyExecution, CopyStrategy,
    SignalCandidate, TelegramConnection, TelegramMessage,
)
from .parser import SignalParseError, parse_signal, parse_signal_candidate
from .telegram import chat_reference_candidates, get_strategy_telegram_connection
from .positions import (
    get_position_payload, reconcile_live_protections, reconcile_pending_entries,
)


CHN_SIGNAL = """#FUTURE #ZEC LONG ( leverage x20 )
Entry: 521.5
Target: 540.1 - 571
SL: 497 ( 4.7 % )"""

CHIP_SIGNAL_HEADER = "#CHIP LONG 0.0271"
CHIP_SIGNAL_DETAILS = """#FUTURE #CHIP

#CHIP ( leverage x10 )

Entry: 0.0271
Target: 0.0285 - 0.0309
SL: 0.0252 ( 7 % )"""

RAVE_SIGNAL_HEADER = "#RAVE #SHORT 0.2882"
RAVE_SIGNAL_DETAILS = """#FUTURE #RAVE LONG
Entry: 0.2882
Target: 0.2682 - 0.2476
SL: 0.3084 (7%)"""


class SignalParserTests(SimpleTestCase):
    def test_parses_real_chn_signal_format(self):
        signal = parse_signal(CHN_SIGNAL)
        self.assertEqual(signal.symbol, "ZECUSDT")
        self.assertEqual(signal.direction, "LONG")
        self.assertEqual(signal.leverage, 20)
        self.assertEqual(signal.take_profits, [Decimal("540.1"), Decimal("571")])

    def test_ignores_result_posts_and_rejects_incomplete_signal(self):
        self.assertIsNone(parse_signal("#PENGU LONG 0.00625 +6.6% All target done"))
        with self.assertRaises(SignalParseError):
            parse_signal("#ZEC LONG Entry: 521.5")

    def test_ai_prefilter_rejects_chat_and_result_updates(self):
        self.assertFalse(is_ai_candidate("Good morning everyone"))
        self.assertFalse(is_ai_candidate("#ZEC LONG TP hit profit 20%"))
        self.assertTrue(is_ai_candidate("#ZECUSDT BUY | Buy zone 521.5 | TP1 540 | Stop 497"))

    def test_detects_explicit_early_signal_but_not_result_commentary(self):
        candidate = parse_signal_candidate(
            "#SUI has a LONG Signal - CHN Supertrend H1 Possible Target 0.86"
        )
        self.assertEqual(candidate.symbol, "SUIUSDT")
        self.assertEqual(candidate.direction, "LONG")
        self.assertEqual(candidate.target_hint, "0.86")
        self.assertIsNone(parse_signal_candidate("#CHIP is flying after a BUY Signal"))


class TelegramChatReferenceTests(SimpleTestCase):
    def test_normalizes_telegram_web_channel_link(self):
        self.assertEqual(
            chat_reference_candidates("https://web.telegram.org/k/#-2159072814"),
            (-1002159072814, -2159072814),
        )

    def test_numeric_references_are_passed_to_telethon_as_integers(self):
        self.assertEqual(chat_reference_candidates("-1002159072814"), (-1002159072814,))
        self.assertEqual(chat_reference_candidates("2159072814"), (-1002159072814,))

    def test_public_username_is_preserved(self):
        self.assertEqual(chat_reference_candidates(" @chnglobal "), ("@chnglobal",))


class FakeBinance:
    def __init__(self, current_price=Decimal("521.5")):
        self.current_price = current_price

    def get_symbol_context(self, symbol, include_symbols=False):
        return {
            "current_price": self.current_price, "volume_step": Decimal("0.001"),
            "min_volume": Decimal("0.001"), "min_notional": Decimal("5"),
        }

    def get_leverage_brackets(self, symbol):
        return [{
            "initial_leverage": 20, "notional_floor": Decimal("0"),
            "notional_cap": Decimal("1000000"), "maint_margin_ratio": Decimal("0.004"),
        }]

    def get_usdt_balance(self):
        return Decimal("500")


class FakeLiveBinance(FakeBinance):
    def __init__(self, current_price=Decimal("523.1")):
        super().__init__(current_price)
        self.orders = []
        self.order_status = {
            "status": "NEW", "executedQty": "0", "avgPrice": "0", "orderId": 9001,
        }
        self.positions = []
        self.user_trades = []
        self.algo_orders = {}

    def change_initial_leverage(self, symbol, leverage):
        return {"leverage": leverage}

    def place_futures_order(self, **params):
        self.orders.append(params)
        return {**self.order_status, "orderId": 9001}

    def place_futures_algo_order(self, **params):
        self.orders.append(params)
        algo_id = len(self.orders) + 100
        self.algo_orders[str(algo_id)] = {
            "algoId": algo_id, "algoStatus": "NEW", "actualQty": "0", **params,
        }
        return {"algoId": algo_id}

    def get_futures_algo_order(self, algo_id):
        return self.algo_orders[str(algo_id)]

    def cancel_futures_algo_order(self, algo_id):
        order = self.algo_orders[str(algo_id)]
        order["algoStatus"] = "CANCELED"
        return order

    def get_futures_order(self, symbol, order_id):
        return self.order_status

    def cancel_futures_order(self, symbol, order_id):
        self.order_status = {**self.order_status, "status": "CANCELED"}
        return self.order_status

    def get_futures_positions(self):
        return self.positions

    def get_futures_user_trades(self, symbol, limit=100, from_id=None):
        return self.user_trades


class CopyExecutionTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="copy-user", email="copy@example.com", password="secret-pass"
        )
        self.account = ExchangeAccount.objects.create(
            user=self.user, exchange="BINANCE", status=ExchangeAccount.Status.CONNECTED
        )
        ExchangeCredential.objects.create(account=self.account, api_key="key", api_secret="secret")
        self.connection = TelegramConnection.objects.create(
            user=self.user, api_id=123, api_hash="hash-value-123456", session="session",
            status=TelegramConnection.Status.CONNECTED,
        )
        self.strategy = CopyStrategy.objects.create(
            user=self.user, telegram_connection=self.connection, chat_id=-1004446024248,
            chat_title="CHN Coin Global", chat_username="chnglobal",
            allocation_usdt="10", max_leverage=10, use_binance_max_leverage=False,
            max_daily_loss_usdt="50", risk_percent_per_order="100.00",
            minimum_risk_reward="0.50",
        )

    def test_connection_lookup_is_async_safe_for_uncached_strategy(self):
        uncached_strategy = CopyStrategy.objects.get(pk=self.strategy.pk)

        connection = async_to_sync(get_strategy_telegram_connection)(uncached_strategy)

        self.assertEqual(connection.pk, self.connection.pk)

    @patch("copytrading.execution._binance_for_user")
    def test_paper_signal_is_idempotent_and_creates_draft_log(self, mock_client):
        mock_client.return_value = (self.account, FakeBinance())
        sent = timezone.now()
        first = process_telegram_message(self.strategy, 101, CHN_SIGNAL, sent)
        second = process_telegram_message(self.strategy, 101, CHN_SIGNAL, sent)
        execution = first[2]
        self.assertEqual(execution.status, CopyExecution.Status.PAPER_FILLED)
        self.assertEqual(execution.leverage, 10)
        self.assertEqual(CopyExecution.objects.count(), 1)
        self.assertEqual(TradeLog.objects.filter(source=TradeLog.Source.DRAFT).count(), 1)
        self.assertIsNone(second[2])

    @patch("copytrading.execution._binance_for_user")
    def test_split_chn_signal_creates_one_paper_position(self, mock_client):
        mock_client.return_value = (self.account, FakeBinance(Decimal("0.0271")))
        sent = timezone.now()

        header = process_telegram_message(
            self.strategy, 201, CHIP_SIGNAL_HEADER, sent
        )
        details = process_telegram_message(
            self.strategy, 202, CHIP_SIGNAL_DETAILS, sent + timedelta(seconds=102)
        )
        duplicate = process_telegram_message(
            self.strategy, 202, CHIP_SIGNAL_DETAILS, sent + timedelta(seconds=102)
        )

        self.assertIsNone(header[1])
        self.assertEqual(header[0].parse_status, TelegramMessage.ParseStatus.INVALID)
        self.assertEqual(details[1].symbol, "CHIPUSDT")
        self.assertEqual(details[1].direction, "LONG")
        self.assertEqual(details[1].parser_version, "chn-v2-multi")
        self.assertEqual(details[2].status, CopyExecution.Status.PAPER_FILLED)
        self.assertEqual(details[2].position_status, CopyExecution.PositionStatus.OPEN)
        self.assertEqual(CopyExecution.objects.filter(strategy=self.strategy).count(), 1)
        self.assertEqual(TradeLog.objects.filter(source=TradeLog.Source.DRAFT).count(), 1)
        self.assertIsNone(duplicate[2])

    @patch("copytrading.execution._binance_for_user")
    def test_split_signal_uses_prior_direction_when_detail_has_typo(self, mock_client):
        mock_client.return_value = (self.account, FakeBinance(Decimal("0.2882")))
        sent = timezone.now()
        process_telegram_message(self.strategy, 211, RAVE_SIGNAL_HEADER, sent)
        _, signal, execution = process_telegram_message(
            self.strategy, 212, RAVE_SIGNAL_DETAILS, sent + timedelta(minutes=2)
        )

        self.assertEqual(signal.symbol, "RAVEUSDT")
        self.assertEqual(signal.direction, "SHORT")
        self.assertEqual(signal.parser_version, "chn-v2-multi")
        self.assertEqual(execution.status, CopyExecution.Status.PAPER_FILLED)

    def test_early_signal_creates_review_candidate_without_order(self):
        message, signal, execution = process_telegram_message(
            self.strategy,
            220,
            "#SUI has a LONG Signal - CHN Supertrend H1 Possible Target 0.86",
            timezone.now(),
        )

        self.assertIsNone(signal)
        self.assertIsNone(execution)
        self.assertEqual(message.parse_status, TelegramMessage.ParseStatus.REVIEW)
        self.assertEqual(message.signal_candidate.status, SignalCandidate.Status.PENDING)
        self.assertEqual(CopyExecution.objects.count(), 0)

    @patch("copytrading.views.execute_signal")
    def test_user_can_approve_review_candidate_with_complete_risk_levels(self, mock_execute):
        message, _, _ = process_telegram_message(
            self.strategy,
            221,
            "#SUI has a LONG Signal - CHN Supertrend H1 Possible Target 0.86",
            timezone.now(),
        )
        api = APIClient()
        api.force_authenticate(self.user)
        response = api.post(
            f"/copy-trading/strategies/{self.strategy.id}/messages/221/review/",
            {
                "action": "APPROVE",
                "entry_price": "0.81",
                "stop_loss": "0.77",
                "take_profit": "0.86",
                "leverage": 5,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        message.refresh_from_db()
        self.assertEqual(message.signal.direction, "LONG")
        self.assertEqual(message.signal.parser_version, "user-review-v1")
        self.assertEqual(message.signal_candidate.status, SignalCandidate.Status.APPROVED)
        mock_execute.assert_called_once()

    def test_review_rejects_invalid_price_structure(self):
        process_telegram_message(
            self.strategy,
            222,
            "#SUI has a LONG Signal - CHN Supertrend H1 Possible Target 0.86",
            timezone.now(),
        )
        api = APIClient()
        api.force_authenticate(self.user)
        response = api.post(
            f"/copy-trading/strategies/{self.strategy.id}/messages/222/review/",
            {
                "action": "APPROVE",
                "entry_price": "0.81",
                "stop_loss": "0.84",
                "take_profit": "0.86",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(CopyExecution.objects.count(), 0)

    def test_live_review_requires_explicit_real_order_confirmation(self):
        self.strategy.mode = CopyStrategy.Mode.LIVE
        self.strategy.save(update_fields=("mode",))
        process_telegram_message(
            self.strategy,
            223,
            "#SUI has a LONG Signal - CHN Supertrend H1 Possible Target 0.86",
            timezone.now(),
        )
        api = APIClient()
        api.force_authenticate(self.user)
        response = api.post(
            f"/copy-trading/strategies/{self.strategy.id}/messages/223/review/",
            {
                "action": "APPROVE",
                "entry_price": "0.81",
                "stop_loss": "0.77",
                "take_profit": "0.86",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("confirm_live", response.data)
        self.assertEqual(CopyExecution.objects.count(), 0)

    def test_split_signal_does_not_merge_different_symbols_or_stale_posts(self):
        sent = timezone.now()
        process_telegram_message(self.strategy, 301, CHIP_SIGNAL_HEADER, sent, execute=False)

        different_symbol = CHIP_SIGNAL_DETAILS.replace("CHIP", "OPENAI")
        _, wrong_signal, _ = process_telegram_message(
            self.strategy, 302, different_symbol, sent + timedelta(seconds=30), execute=False
        )
        _, stale_signal, _ = process_telegram_message(
            self.strategy, 303, CHIP_SIGNAL_DETAILS, sent + timedelta(minutes=6), execute=False
        )

        self.assertIsNone(wrong_signal)
        self.assertIsNone(stale_signal)
        self.assertEqual(CopyExecution.objects.count(), 0)

    def test_saved_split_signal_can_be_reprocessed_after_parser_upgrade(self):
        sent = timezone.now()
        TelegramMessage.objects.create(
            strategy=self.strategy,
            telegram_message_id=401,
            text=CHIP_SIGNAL_HEADER,
            sent_at=sent,
            parse_status=TelegramMessage.ParseStatus.INVALID,
        )
        details = TelegramMessage.objects.create(
            strategy=self.strategy,
            telegram_message_id=402,
            text=CHIP_SIGNAL_DETAILS,
            sent_at=sent + timedelta(seconds=90),
        )

        first = reprocess_saved_message(details, execute=False)
        second = reprocess_saved_message(details, execute=False)

        self.assertEqual(first[1].symbol, "CHIPUSDT")
        self.assertEqual(first[1].parser_version, "chn-v2-multi")
        self.assertIsNone(first[2])
        self.assertEqual(second[1].pk, first[1].pk)
        self.assertEqual(self.strategy.messages.filter(parse_status="SIGNAL").count(), 1)

    @patch("copytrading.execution._binance_for_user")
    def test_auto_leverage_is_risk_sized_instead_of_using_binance_maximum(self, mock_client):
        mock_client.return_value = (self.account, FakeBinance())
        self.strategy.use_binance_max_leverage = True
        self.strategy.save(update_fields=("use_binance_max_leverage",))

        execution = process_telegram_message(
            self.strategy, 105, CHN_SIGNAL, timezone.now()
        )[2]

        self.assertEqual(execution.status, CopyExecution.Status.PAPER_FILLED)
        self.assertEqual(execution.leverage, 15)
        risk = abs(execution.entry_price - execution.stop_loss) * execution.quantity
        self.assertLessEqual(risk, Decimal(self.strategy.allocation_usdt))

    @patch("copytrading.execution._binance_for_user")
    def test_signal_below_user_minimum_risk_reward_is_skipped(self, mock_client):
        mock_client.return_value = (self.account, FakeBinance())
        self.strategy.minimum_risk_reward = Decimal("1.00")
        self.strategy.save(update_fields=("minimum_risk_reward",))

        execution = process_telegram_message(
            self.strategy, 122, CHN_SIGNAL, timezone.now()
        )[2]

        self.assertEqual(execution.status, CopyExecution.Status.SKIPPED)
        self.assertIn("below the configured minimum 1", execution.error)

    @patch("copytrading.execution._binance_for_user")
    def test_risk_percent_caps_stop_loss_amount(self, mock_client):
        mock_client.return_value = (self.account, FakeBinance())
        self.strategy.risk_percent_per_order = Decimal("10.00")
        self.strategy.save(update_fields=("risk_percent_per_order",))

        execution = process_telegram_message(
            self.strategy, 123, CHN_SIGNAL, timezone.now()
        )[2]

        self.assertEqual(execution.status, CopyExecution.Status.PAPER_FILLED)
        risk = abs(execution.entry_price - execution.stop_loss) * execution.quantity
        allowed = Decimal(self.strategy.allocation_usdt) * Decimal("0.10")
        self.assertLessEqual(risk, allowed)

    @override_settings(COPY_TRADING_AUTO_LEVERAGE_CAP=20)
    @patch("copytrading.execution._binance_for_user")
    def test_signal_without_leverage_never_falls_back_to_binance_maximum(self, mock_client):
        fake = FakeBinance()
        fake.get_leverage_brackets = lambda _symbol: [{
            "initial_leverage": 125, "notional_floor": Decimal("0"),
            "notional_cap": Decimal("1000000"), "maint_margin_ratio": Decimal("0.004"),
        }]
        mock_client.return_value = (self.account, fake)
        self.strategy.use_binance_max_leverage = True
        self.strategy.save(update_fields=("use_binance_max_leverage",))
        signal_without_leverage = CHN_SIGNAL.replace("( leverage x20 )", "")

        execution = process_telegram_message(
            self.strategy, 121, signal_without_leverage, timezone.now()
        )[2]

        self.assertIsNone(execution.signal.requested_leverage)
        self.assertLessEqual(execution.leverage, 20)
        self.assertNotEqual(execution.leverage, 125)
        risk = abs(execution.entry_price - execution.stop_loss) * execution.quantity
        self.assertLessEqual(risk, Decimal(self.strategy.allocation_usdt))

    @patch("copytrading.execution._binance_for_user")
    def test_nearby_price_inside_configured_tolerance_is_filled(self, mock_client):
        mock_client.return_value = (self.account, FakeBinance(Decimal("523.0")))

        execution = process_telegram_message(
            self.strategy, 106, CHN_SIGNAL, timezone.now()
        )[2]

        self.assertEqual(execution.status, CopyExecution.Status.PAPER_FILLED)
        self.assertEqual(execution.entry_price, Decimal("523.0"))

    @patch("copytrading.execution._binance_for_user")
    def test_price_outside_configured_tolerance_is_skipped_with_diagnostics(self, mock_client):
        mock_client.return_value = (self.account, FakeBinance(Decimal("523.1")))
        self.strategy.entry_order_type = CopyStrategy.EntryOrderType.MARKET
        self.strategy.save(update_fields=("entry_order_type",))

        execution = process_telegram_message(
            self.strategy, 107, CHN_SIGNAL, timezone.now()
        )[2]

        self.assertEqual(execution.status, CopyExecution.Status.SKIPPED)
        self.assertIn("Current Binance price 523.1", execution.error)
        self.assertIn("0.3% tolerance", execution.error)
        self.assertIn("accepted 519.9355-523.0645", execution.error)

    @patch("copytrading.execution._binance_for_user")
    def test_smart_entry_falls_back_to_limit_after_adverse_slippage(self, mock_client):
        mock_client.return_value = (self.account, FakeBinance(Decimal("523.1")))

        execution = process_telegram_message(
            self.strategy, 112, CHN_SIGNAL, timezone.now()
        )[2]

        self.assertEqual(execution.status, CopyExecution.Status.PENDING_ENTRY)
        self.assertEqual(execution.entry_order_type, CopyStrategy.EntryOrderType.LIMIT)
        self.assertEqual(execution.limit_price, Decimal("521.5"))

    @patch("copytrading.execution._binance_for_user")
    def test_smart_entry_uses_market_for_a_favorable_price(self, mock_client):
        mock_client.return_value = (self.account, FakeBinance(Decimal("519")))

        execution = process_telegram_message(
            self.strategy, 113, CHN_SIGNAL, timezone.now()
        )[2]

        self.assertEqual(execution.status, CopyExecution.Status.PAPER_FILLED)
        self.assertEqual(execution.entry_order_type, CopyStrategy.EntryOrderType.MARKET)
        self.assertEqual(execution.entry_price, Decimal("519"))

    @patch("copytrading.ai_detection._openai_response")
    @patch("copytrading.execution._binance_for_user")
    def test_ai_fallback_parses_only_candidate_and_reuses_cached_result(self, mock_binance, mock_ai):
        mock_binance.return_value = (self.account, FakeBinance())
        AISignalAgent.objects.create(
            user=self.user, api_key="sk-test-secret-key-value", model="gpt-5-nano",
            status=AISignalAgent.Status.CONNECTED,
        )
        self.strategy.ai_detection_enabled = True
        self.strategy.save(update_fields=("ai_detection_enabled",))
        text = "#ZECUSDT BUY | Buy zone 521.5 | TP1 540.1 | Stop 497 | 20x"
        result = {
            "is_signal": True, "confidence": 0.97, "symbol": "ZECUSDT",
            "direction": "LONG", "entry_low": "521.5", "entry_high": "521.5",
            "stop_loss": "497", "take_profits": ["540.1"], "leverage": 20,
            "reason_code": "ACTIONABLE",
        }
        mock_ai.return_value = (result, 90, 45)

        first = process_telegram_message(self.strategy, 114, text, timezone.now())
        second = process_telegram_message(self.strategy, 115, text, timezone.now())

        self.assertEqual(first[1].parser_version, "ai-signal-v1")
        self.assertEqual(first[2].status, CopyExecution.Status.PAPER_FILLED)
        self.assertEqual(second[1].parser_version, "ai-signal-v1")
        self.assertEqual(mock_ai.call_count, 1)
        analysis = AISignalAnalysis.objects.get()
        self.assertEqual(analysis.status, AISignalAnalysis.Status.SIGNAL)
        self.assertEqual(analysis.input_tokens, 90)

    @patch("copytrading.positions.BinanceService.get_futures_mark_prices")
    @patch("copytrading.execution._binance_for_user")
    def test_paper_limit_waits_for_retrace_then_opens(self, mock_client, mock_marks):
        mock_client.return_value = (self.account, FakeBinance(Decimal("523.1")))
        self.strategy.entry_order_type = CopyStrategy.EntryOrderType.LIMIT
        self.strategy.save(update_fields=("entry_order_type",))

        execution = process_telegram_message(
            self.strategy, 108, CHN_SIGNAL, timezone.now()
        )[2]
        self.assertEqual(execution.status, CopyExecution.Status.PENDING_ENTRY)
        self.assertEqual(execution.limit_price, Decimal("521.5"))

        mock_marks.return_value = {"ZECUSDT": Decimal("521.4")}
        reconcile_pending_entries(self.user)
        execution.refresh_from_db()
        self.assertEqual(execution.position_status, CopyExecution.PositionStatus.OPEN)
        self.assertEqual(execution.status, CopyExecution.Status.PAPER_FILLED)

    @patch("copytrading.positions.BinanceService.get_futures_mark_prices")
    @patch("copytrading.execution._binance_for_user")
    def test_unfilled_paper_limit_expires_without_opening(self, mock_client, mock_marks):
        mock_client.return_value = (self.account, FakeBinance(Decimal("523.1")))
        mock_marks.return_value = {"ZECUSDT": Decimal("523.1")}
        self.strategy.entry_order_type = CopyStrategy.EntryOrderType.LIMIT
        self.strategy.save(update_fields=("entry_order_type",))
        execution = process_telegram_message(
            self.strategy, 110, CHN_SIGNAL, timezone.now()
        )[2]
        execution.entry_expires_at = timezone.now() - timedelta(seconds=1)
        execution.save(update_fields=("entry_expires_at",))

        reconcile_pending_entries(self.user)
        execution.refresh_from_db()
        self.assertEqual(execution.status, CopyExecution.Status.CANCELLED)
        self.assertEqual(execution.position_status, CopyExecution.PositionStatus.NONE)

    @override_settings(COPY_TRADING_LIVE_ENABLED=True)
    @patch("copytrading.positions._binance_for_user")
    @patch("copytrading.execution._binance_for_user")
    def test_live_limit_is_protected_only_after_fill(self, mock_execute_client, mock_position_client):
        fake = FakeLiveBinance()
        mock_execute_client.return_value = (self.account, fake)
        mock_position_client.return_value = (self.account, fake)
        self.strategy.mode = CopyStrategy.Mode.LIVE
        self.strategy.entry_order_type = CopyStrategy.EntryOrderType.LIMIT
        self.strategy.save(update_fields=("mode", "entry_order_type"))

        execution = process_telegram_message(
            self.strategy, 109, CHN_SIGNAL, timezone.now()
        )[2]
        self.assertEqual(execution.position_status, CopyExecution.PositionStatus.PENDING)
        self.assertEqual(fake.orders[0]["type"], "LIMIT")
        self.assertEqual(fake.orders[0]["timeInForce"], "GTD")
        self.assertGreater(fake.orders[0]["goodTillDate"], int(timezone.now().timestamp() * 1000))
        self.assertEqual(len(fake.orders), 1)

        fake.order_status = {
            "status": "FILLED", "executedQty": str(execution.quantity),
            "avgPrice": "521.5", "orderId": 9001,
        }
        fake.positions = [{
            "symbol": "ZECUSDT", "positionAmt": str(execution.quantity),
            "entryPrice": "521.5", "markPrice": "522",
            "unRealizedProfit": "0.1", "leverage": str(execution.leverage),
        }]
        reconcile_pending_entries(self.user)
        execution.refresh_from_db()
        self.assertEqual(execution.status, CopyExecution.Status.PROTECTED)
        self.assertEqual(execution.position_status, CopyExecution.PositionStatus.OPEN)
        self.assertEqual(
            [item["type"] for item in fake.orders[1:]],
            ["STOP_MARKET", "TAKE_PROFIT_MARKET", "TAKE_PROFIT_MARKET"],
        )

    @override_settings(COPY_TRADING_LIVE_ENABLED=True)
    @patch("copytrading.positions._binance_for_user")
    @patch("copytrading.execution._binance_for_user")
    def test_filled_live_limit_protects_only_remaining_binance_quantity(
        self, mock_execute_client, mock_position_client
    ):
        fake = FakeLiveBinance()
        mock_execute_client.return_value = (self.account, fake)
        mock_position_client.return_value = (self.account, fake)
        self.strategy.mode = CopyStrategy.Mode.LIVE
        self.strategy.entry_order_type = CopyStrategy.EntryOrderType.LIMIT
        self.strategy.save(update_fields=("mode", "entry_order_type"))
        execution = process_telegram_message(
            self.strategy, 119, CHN_SIGNAL, timezone.now()
        )[2]
        filled_quantity = execution.quantity
        remaining_quantity = filled_quantity / Decimal("2")
        fake.order_status = {
            "status": "FILLED", "executedQty": str(filled_quantity),
            "avgPrice": "521.5", "orderId": 9001,
        }
        fake.positions = [{
            "symbol": "ZECUSDT", "positionAmt": str(remaining_quantity),
            "entryPrice": "521.5", "markPrice": "522",
            "unRealizedProfit": "0.1", "leverage": str(execution.leverage),
        }]

        reconcile_pending_entries(self.user)

        execution.refresh_from_db()
        protection_orders = fake.orders[1:]
        self.assertEqual(execution.status, CopyExecution.Status.PROTECTED)
        self.assertEqual(execution.quantity, filled_quantity)
        self.assertEqual(Decimal(protection_orders[0]["quantity"]), remaining_quantity)
        self.assertEqual(
            Decimal(protection_orders[1]["quantity"]),
            execution.take_profit_quantity,
        )
        self.assertEqual(
            Decimal(protection_orders[2]["quantity"]),
            remaining_quantity - execution.take_profit_quantity,
        )

    @override_settings(COPY_TRADING_LIVE_ENABLED=True)
    @patch("copytrading.positions._binance_for_user")
    @patch("copytrading.execution._binance_for_user")
    def test_live_tp1_closes_partial_and_moves_runner_stop_to_entry(
        self, mock_execute_client, mock_position_client
    ):
        fake = FakeLiveBinance(Decimal("521.5"))
        mock_execute_client.return_value = (self.account, fake)
        mock_position_client.return_value = (self.account, fake)
        self.strategy.mode = CopyStrategy.Mode.LIVE
        self.strategy.entry_order_type = CopyStrategy.EntryOrderType.MARKET
        self.strategy.tp1_close_percent = Decimal("70")
        self.strategy.save(update_fields=("mode", "entry_order_type", "tp1_close_percent"))
        execution = process_telegram_message(
            self.strategy, 124, CHN_SIGNAL, timezone.now()
        )[2]
        original_stop_id = execution.stop_order_id
        target = fake.algo_orders[execution.take_profit_order_id]
        target["algoStatus"] = "FINISHED"
        target["actualQty"] = str(execution.take_profit_quantity)
        remaining = execution.quantity - execution.take_profit_quantity
        fake.positions = [{
            "symbol": execution.symbol, "positionAmt": str(remaining),
            "entryPrice": str(execution.entry_price), "markPrice": "540.1",
            "unRealizedProfit": "1", "leverage": str(execution.leverage),
        }]

        reconcile_live_protections(self.user)

        execution.refresh_from_db()
        self.assertIsNotNone(execution.break_even_activated_at)
        self.assertEqual(execution.remaining_quantity, remaining)
        self.assertEqual(execution.runner_take_profit, Decimal("571"))
        runner_target = fake.algo_orders[execution.runner_take_profit_order_id]
        self.assertEqual(Decimal(runner_target["quantity"]), remaining)
        self.assertEqual(Decimal(runner_target["triggerPrice"]), Decimal("571"))
        self.assertEqual(
            fake.algo_orders[original_stop_id]["algoStatus"], "CANCELED"
        )
        break_even = fake.algo_orders[execution.break_even_stop_order_id]
        self.assertEqual(Decimal(break_even["quantity"]), remaining)
        self.assertEqual(Decimal(break_even["triggerPrice"]), execution.entry_price)

    @patch("copytrading.positions.BinanceService.get_futures_mark_prices")
    @patch("copytrading.execution._binance_for_user")
    def test_paper_tp1_closes_partial_then_runner_exits_at_tp2(
        self, mock_client, mock_marks
    ):
        mock_client.return_value = (self.account, FakeBinance())
        execution = process_telegram_message(
            self.strategy, 125, CHN_SIGNAL, timezone.now()
        )[2]
        original_quantity = execution.quantity
        mock_marks.return_value = {execution.symbol: execution.take_profit}

        first = get_position_payload(self.user, str(self.strategy.id))

        execution.refresh_from_db()
        self.assertTrue(execution.break_even_activated_at)
        self.assertEqual(
            execution.remaining_quantity,
            original_quantity - execution.take_profit_quantity,
        )
        self.assertEqual(first["open"][0]["stop_loss"], "521.5")
        self.assertTrue(first["open"][0]["break_even_active"])

        mock_marks.return_value = {execution.symbol: execution.runner_take_profit}
        second = get_position_payload(self.user, str(self.strategy.id))

        execution.refresh_from_db()
        self.assertEqual(second["open"], [])
        self.assertEqual(execution.position_status, CopyExecution.PositionStatus.CLOSED)
        self.assertEqual(execution.close_reason, "TAKE_PROFIT_2")
        self.assertGreater(execution.realized_pnl, 0)

    @override_settings(
        COPY_TRADING_LIVE_ENABLED=True,
        COPY_TRADING_POSITION_MISSING_GRACE_SECONDS=30,
    )
    @patch("copytrading.positions.BinanceService.get_futures_mark_prices")
    @patch("copytrading.positions._binance_for_user")
    @patch("copytrading.execution._binance_for_user")
    def test_closed_live_position_uses_actual_binance_fill_and_realized_pnl(
        self, mock_execute_client, mock_position_client, mock_marks
    ):
        fake = FakeLiveBinance(Decimal("521.5"))
        mock_execute_client.return_value = (self.account, fake)
        mock_position_client.return_value = (self.account, fake)
        mock_marks.return_value = {"ZECUSDT": Decimal("521.5")}
        self.strategy.mode = CopyStrategy.Mode.LIVE
        self.strategy.entry_order_type = CopyStrategy.EntryOrderType.MARKET
        self.strategy.save(update_fields=("mode", "entry_order_type"))
        execution = process_telegram_message(
            self.strategy, 120, CHN_SIGNAL, timezone.now()
        )[2]
        execution.binance_missing_since = timezone.now() - timedelta(seconds=31)
        execution.save(update_fields=("binance_missing_since",))
        fake.user_trades = [{
            "symbol": "ZECUSDT", "side": "SELL", "price": "540",
            "qty": str(execution.quantity), "realizedPnl": "3.75",
            "time": int(timezone.now().timestamp() * 1000),
        }]

        payload = get_position_payload(self.user, str(self.strategy.id))

        execution.refresh_from_db()
        self.assertEqual(payload["open"], [])
        self.assertEqual(execution.exit_price, Decimal("540"))
        self.assertEqual(execution.realized_pnl, Decimal("3.75"))
        self.assertEqual(execution.close_reason, "TAKE_PROFIT")

    @override_settings(COPY_TRADING_LIVE_ENABLED=True)
    @patch("copytrading.execution._binance_for_user")
    def test_partial_live_limit_cancels_remainder_and_protects_fill(self, mock_client):
        fake = FakeLiveBinance()
        fake.order_status = {
            "status": "PARTIALLY_FILLED", "executedQty": "0.005",
            "avgPrice": "521.5", "orderId": 9001,
        }
        mock_client.return_value = (self.account, fake)
        self.strategy.mode = CopyStrategy.Mode.LIVE
        self.strategy.entry_order_type = CopyStrategy.EntryOrderType.LIMIT
        self.strategy.save(update_fields=("mode", "entry_order_type"))

        execution = process_telegram_message(
            self.strategy, 111, CHN_SIGNAL, timezone.now()
        )[2]

        self.assertEqual(execution.status, CopyExecution.Status.PROTECTED)
        self.assertEqual(execution.quantity, Decimal("0.005"))
        self.assertEqual(fake.order_status["status"], "CANCELED")
        self.assertEqual([item["type"] for item in fake.orders[1:]], ["STOP_MARKET", "TAKE_PROFIT_MARKET"])

    def test_history_import_parses_but_never_executes_old_signal(self):
        message, signal, execution = process_telegram_message(
            self.strategy, 99, CHN_SIGNAL, timezone.now() - timedelta(days=1), execute=False
        )
        self.assertIsNotNone(signal)
        self.assertIsNone(execution)
        self.assertEqual(CopyExecution.objects.count(), 0)
        self.assertEqual(TradeLog.objects.count(), 0)

    @patch("copytrading.execution._binance_for_user")
    def test_execution_mode_cannot_change_while_position_is_open(self, mock_client):
        mock_client.return_value = (self.account, FakeBinance())
        process_telegram_message(self.strategy, 104, CHN_SIGNAL, timezone.now())
        client = APIClient()
        client.force_authenticate(self.user)
        response = client.patch(
            f"/copy-trading/strategies/{self.strategy.id}/",
            {"mode": "LIVE", "confirm_live": True},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Close all running positions", str(response.data))

    @patch("copytrading.positions.BinanceService.get_futures_mark_prices")
    @patch("copytrading.execution._binance_for_user")
    def test_paper_position_exposes_realtime_pnl(self, mock_client, mock_marks):
        mock_client.return_value = (self.account, FakeBinance())
        mock_marks.return_value = {"ZECUSDT": Decimal("530")}
        execution = process_telegram_message(
            self.strategy, 103, CHN_SIGNAL, timezone.now()
        )[2]
        payload = get_position_payload(self.user, str(self.strategy.id))
        position = payload["open"][0]
        self.assertEqual(position["id"], str(execution.id))
        self.assertEqual(position["status"], "OPEN")
        self.assertGreater(Decimal(position["unrealized_pnl"]), 0)
        self.assertGreater(Decimal(position["roe_percent"]), 0)

    @override_settings(
        COPY_TRADING_LIVE_ENABLED=True,
        COPY_TRADING_POSITION_MISSING_GRACE_SECONDS=30,
    )
    @patch("copytrading.positions.BinanceService.get_futures_mark_prices")
    @patch("copytrading.positions._binance_for_user")
    @patch("copytrading.execution._binance_for_user")
    def test_live_position_survives_one_missing_binance_snapshot(
        self, mock_execute_client, mock_position_client, mock_marks
    ):
        fake = FakeLiveBinance(Decimal("521.5"))
        mock_execute_client.return_value = (self.account, fake)
        mock_position_client.return_value = (self.account, fake)
        mock_marks.return_value = {"ZECUSDT": Decimal("522")}
        self.strategy.mode = CopyStrategy.Mode.LIVE
        self.strategy.entry_order_type = CopyStrategy.EntryOrderType.MARKET
        self.strategy.save(update_fields=("mode", "entry_order_type"))
        execution = process_telegram_message(
            self.strategy, 116, CHN_SIGNAL, timezone.now()
        )[2]

        payload = get_position_payload(self.user, str(self.strategy.id))

        execution.refresh_from_db()
        self.assertEqual(execution.position_status, CopyExecution.PositionStatus.OPEN)
        self.assertIsNotNone(execution.binance_missing_since)
        self.assertEqual(len(payload["open"]), 1)
        self.assertEqual(payload["open"][0]["sync_status"], "RECONNECTING")

    @override_settings(
        COPY_TRADING_LIVE_ENABLED=True,
        COPY_TRADING_POSITION_MISSING_GRACE_SECONDS=30,
    )
    @patch("copytrading.positions.BinanceService.get_futures_mark_prices")
    @patch("copytrading.positions._binance_for_user")
    @patch("copytrading.execution._binance_for_user")
    def test_live_position_closes_only_after_continuous_missing_grace(
        self, mock_execute_client, mock_position_client, mock_marks
    ):
        fake = FakeLiveBinance(Decimal("521.5"))
        mock_execute_client.return_value = (self.account, fake)
        mock_position_client.return_value = (self.account, fake)
        mock_marks.return_value = {"ZECUSDT": Decimal("522")}
        self.strategy.mode = CopyStrategy.Mode.LIVE
        self.strategy.entry_order_type = CopyStrategy.EntryOrderType.MARKET
        self.strategy.save(update_fields=("mode", "entry_order_type"))
        execution = process_telegram_message(
            self.strategy, 117, CHN_SIGNAL, timezone.now()
        )[2]
        execution.binance_missing_since = timezone.now() - timedelta(seconds=31)
        execution.save(update_fields=("binance_missing_since",))

        payload = get_position_payload(self.user, str(self.strategy.id))

        execution.refresh_from_db()
        self.assertEqual(execution.position_status, CopyExecution.PositionStatus.CLOSED)
        self.assertEqual(execution.close_reason, "BINANCE_SYNC")
        self.assertEqual(payload["open"], [])

    @override_settings(COPY_TRADING_LIVE_ENABLED=True)
    @patch("copytrading.positions.BinanceService.get_futures_mark_prices")
    @patch("copytrading.positions._binance_for_user")
    @patch("copytrading.execution._binance_for_user")
    def test_live_position_recovery_clears_missing_state_and_matches_direction(
        self, mock_execute_client, mock_position_client, mock_marks
    ):
        fake = FakeLiveBinance(Decimal("521.5"))
        mock_execute_client.return_value = (self.account, fake)
        mock_position_client.return_value = (self.account, fake)
        mock_marks.return_value = {"ZECUSDT": Decimal("522")}
        self.strategy.mode = CopyStrategy.Mode.LIVE
        self.strategy.entry_order_type = CopyStrategy.EntryOrderType.MARKET
        self.strategy.save(update_fields=("mode", "entry_order_type"))
        execution = process_telegram_message(
            self.strategy, 118, CHN_SIGNAL, timezone.now()
        )[2]
        execution.binance_missing_since = timezone.now()
        execution.save(update_fields=("binance_missing_since",))
        fake.positions = [
            {
                "symbol": "ZECUSDT", "positionAmt": "-0.1", "markPrice": "522",
                "entryPrice": "520", "unRealizedProfit": "-0.2", "leverage": "5",
            },
            {
                "symbol": "ZECUSDT", "positionAmt": str(execution.quantity),
                "markPrice": "522", "entryPrice": "521.5",
                "unRealizedProfit": "0.5", "leverage": str(execution.leverage),
            },
        ]

        payload = get_position_payload(self.user, str(self.strategy.id))

        execution.refresh_from_db()
        self.assertIsNone(execution.binance_missing_since)
        self.assertIsNotNone(execution.last_binance_seen_at)
        self.assertEqual(payload["open"][0]["sync_status"], "CONFIRMED")
        self.assertEqual(payload["open"][0]["direction"], "LONG")
        self.assertEqual(payload["open"][0]["unrealized_pnl"], "0.5")

    @override_settings(COPY_TRADING_LIVE_ENABLED=False)
    @patch("copytrading.execution._binance_for_user")
    def test_live_strategy_fails_closed_when_kill_switch_is_off(self, mock_client):
        mock_client.return_value = (self.account, FakeBinance())
        self.strategy.mode = CopyStrategy.Mode.LIVE
        self.strategy.save()
        execution = process_telegram_message(
            self.strategy, 102, CHN_SIGNAL, timezone.now() + timedelta(seconds=1)
        )[2]
        self.assertEqual(execution.status, CopyExecution.Status.SKIPPED)
        self.assertIn("kill-switch", execution.error)


class CopyTradingAPITests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="api-copy", email="api-copy@example.com", password="secret-pass"
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_endpoints_require_auth_and_never_expose_telegram_secrets(self):
        TelegramConnection.objects.create(
            user=self.user, api_id=123, api_hash="very-secret-api-hash", phone="+84123456789",
            phone_hint="***6789", session="very-secret-session", status="CONNECTED",
        )
        response = self.client.get("/copy-trading/telegram/")
        self.assertEqual(response.status_code, 200)
        body = str(response.data)
        self.assertNotIn("very-secret", body)
        self.assertNotIn("+84123456789", body)
        self.client.force_authenticate(user=None)
        self.assertIn(self.client.get("/copy-trading/strategies/").status_code, (401, 403))

    @override_settings(COPY_TRADING_LIVE_ENABLED=True)
    def test_connected_frontend_receives_live_capability(self):
        TelegramConnection.objects.create(
            user=self.user, api_id=123, api_hash="hash", session="session", status="CONNECTED"
        )

        response = self.client.get("/copy-trading/telegram/")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["live_enabled"])

    @patch("copytrading.views.verify_agent")
    def test_ai_agent_key_is_verified_but_never_returned(self, mock_verify):
        secret = "sk-super-secret-openai-key-value"
        response = self.client.post(
            "/copy-trading/ai-agent/",
            {
                "api_key": secret, "model": "gpt-5-nano",
                "min_confidence": "0.920", "daily_call_limit": 25,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(secret, str(response.data))
        self.assertEqual(response.data["api_key_hint"], "***alue")
        self.assertEqual(response.data["daily_call_limit"], 25)
        self.assertEqual(self.client.get("/copy-trading/ai-agent/").status_code, 200)
        mock_verify.assert_called_once()

    def test_enabling_ai_on_live_stream_requires_explicit_confirmation(self):
        connection = TelegramConnection.objects.create(
            user=self.user, api_id=123, api_hash="hash", session="session", status="CONNECTED"
        )
        strategy = CopyStrategy.objects.create(
            user=self.user, telegram_connection=connection, chat_id=-100777,
            chat_title="Live AI", allocation_usdt="10", mode=CopyStrategy.Mode.LIVE,
        )
        response = self.client.patch(
            f"/copy-trading/strategies/{strategy.id}/",
            {"ai_detection_enabled": True}, format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("AI-detected LIVE orders", str(response.data))

        response = self.client.patch(
            f"/copy-trading/strategies/{strategy.id}/",
            {"ai_detection_enabled": True, "confirm_ai_live": True}, format="json",
        )
        self.assertEqual(response.status_code, 200)

    def test_live_creation_requires_explicit_confirmation(self):
        response = self.client.post(
            "/copy-trading/strategies/",
            {"chat_reference": "@chnglobal", "allocation_usdt": "10", "mode": "LIVE"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_telegram_credentials_reject_labels_before_network_call(self):
        response = self.client.post(
            "/copy-trading/telegram/start/",
            {"api_id": 123, "api_hash": ": abcdef0123456789abcdef0123456789", "phone": "+84901234567"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("32 hexadecimal", str(response.data))

    def test_verification_serializer_extracts_code_from_telegram_message(self):
        from copytrading.serializers import TelegramVerifySerializer

        serializer = TelegramVerifySerializer(data={"code": "Login code: 12345"})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["code"], "12345")

    def test_telegram_media_requires_ownership_and_is_streamed(self):
        connection = TelegramConnection.objects.create(
            user=self.user, api_id=123, api_hash="hash", session="session", status="CONNECTED"
        )
        strategy = CopyStrategy.objects.create(
            user=self.user, telegram_connection=connection, chat_id=-1001,
            chat_title="Photo group", allocation_usdt="10",
        )
        message = TelegramMessage.objects.create(
            strategy=strategy, telegram_message_id=45, sent_at=timezone.now(),
            media_type="PHOTO", media_mime_type="image/jpeg", media_size=4,
        )
        message.media_file.save("test-image.jpg", ContentFile(b"test"))
        self.addCleanup(message.media_file.storage.delete, message.media_file.name)
        response = self.client.get(
            f"/copy-trading/strategies/{strategy.id}/messages/45/media/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/jpeg")
        self.assertEqual(b"".join(response.streaming_content), b"test")

        other = get_user_model().objects.create_user(
            username="other", email="other@example.com", password="secret-pass"
        )
        self.client.force_authenticate(other)
        self.assertEqual(
            self.client.get(f"/copy-trading/strategies/{strategy.id}/messages/45/media/").status_code,
            404,
        )

    def test_message_history_is_cursor_paginated(self):
        connection = TelegramConnection.objects.create(
            user=self.user, api_id=321, api_hash="hash", session="session", status="CONNECTED"
        )
        strategy = CopyStrategy.objects.create(
            user=self.user, telegram_connection=connection, chat_id=-1002,
            chat_title="Paged group", allocation_usdt="10",
        )
        TelegramMessage.objects.bulk_create([
            TelegramMessage(
                strategy=strategy, telegram_message_id=message_id,
                text=f"Message {message_id}", sent_at=timezone.now() + timedelta(seconds=message_id),
            )
            for message_id in range(1, 26)
        ])
        first = self.client.get(f"/copy-trading/strategies/{strategy.id}/messages/?page_size=10")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(len(first.data["results"]), 10)
        self.assertEqual(first.data["results"][0]["telegram_message_id"], 25)
        self.assertEqual(first.data["next_before"], 16)
        older = self.client.get(
            f"/copy-trading/strategies/{strategy.id}/messages/?page_size=10&before=16"
        )
        self.assertEqual([item["telegram_message_id"] for item in older.data["results"]], list(range(15, 5, -1)))

    def test_notification_cursor_returns_only_messages_missed_while_offline(self):
        connection = TelegramConnection.objects.create(
            user=self.user, api_id=654, api_hash="hash", session="session", status="CONNECTED"
        )
        strategy = CopyStrategy.objects.create(
            user=self.user, telegram_connection=connection, chat_id=-1004,
            chat_title="Notification group", allocation_usdt="10", last_notified_message_id=1,
        )
        TelegramMessage.objects.bulk_create([
            TelegramMessage(
                strategy=strategy, telegram_message_id=message_id,
                text=f"Message {message_id}", sent_at=timezone.now() + timedelta(seconds=message_id),
            )
            for message_id in range(1, 5)
        ])

        url = f"/copy-trading/strategies/{strategy.id}/notifications/"
        pending = self.client.get(url, {"page_size": 2})
        self.assertEqual(pending.status_code, 200)
        self.assertEqual(
            [item["telegram_message_id"] for item in pending.data["results"]], [2, 3]
        )
        self.assertTrue(pending.data["has_more"])

        acknowledged = self.client.post(url, {"telegram_message_id": 3}, format="json")
        self.assertEqual(acknowledged.status_code, 200)
        self.assertEqual(acknowledged.data["last_notified_message_id"], 3)
        remaining = self.client.get(url)
        self.assertEqual(
            [item["telegram_message_id"] for item in remaining.data["results"]], [4]
        )

    def test_first_notification_poll_baselines_imported_history(self):
        connection = TelegramConnection.objects.create(
            user=self.user, api_id=655, api_hash="hash", session="session", status="CONNECTED"
        )
        strategy = CopyStrategy.objects.create(
            user=self.user, telegram_connection=connection, chat_id=-1005,
            chat_title="Existing history", allocation_usdt="10",
        )
        TelegramMessage.objects.create(
            strategy=strategy, telegram_message_id=99, text="Imported history",
            sent_at=timezone.now(),
        )

        response = self.client.get(
            f"/copy-trading/strategies/{strategy.id}/notifications/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["results"], [])
        strategy.refresh_from_db()
        self.assertEqual(strategy.last_notified_message_id, 99)

    def test_stream_risk_settings_can_be_edited(self):
        connection = TelegramConnection.objects.create(
            user=self.user, api_id=999, api_hash="hash", session="session", status="CONNECTED"
        )
        strategy = CopyStrategy.objects.create(
            user=self.user, telegram_connection=connection, chat_id=-1003,
            chat_title="Editable group", allocation_usdt="10", max_leverage=10,
            max_daily_loss_usdt="50",
        )
        response = self.client.patch(
            f"/copy-trading/strategies/{strategy.id}/",
            {
                "allocation_usdt": "25", "max_leverage": 7,
                "risk_percent_per_order": "12.50", "minimum_risk_reward": "1.80",
                "tp1_close_percent": "65.00",
                "max_daily_loss_usdt": "80", "entry_tolerance_percent": "0.450",
                "status": "PAUSED",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        strategy.refresh_from_db()
        self.assertEqual(strategy.allocation_usdt, Decimal("25"))
        self.assertEqual(strategy.risk_percent_per_order, Decimal("12.50"))
        self.assertEqual(strategy.minimum_risk_reward, Decimal("1.80"))
        self.assertEqual(strategy.tp1_close_percent, Decimal("65.00"))
        self.assertEqual(strategy.max_leverage, 7)
        self.assertEqual(strategy.entry_tolerance_percent, Decimal("0.450"))
        self.assertEqual(strategy.status, CopyStrategy.Status.PAUSED)
