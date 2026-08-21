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
from .models import CopyExecution, CopyStrategy, TelegramConnection, TelegramMessage
from .parser import SignalParseError, parse_signal
from .telegram import chat_reference_candidates, get_strategy_telegram_connection
from .positions import get_position_payload


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
            max_daily_loss_usdt="50",
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
    def test_auto_leverage_uses_binance_symbol_maximum(self, mock_client):
        mock_client.return_value = (self.account, FakeBinance())
        self.strategy.use_binance_max_leverage = True
        self.strategy.save(update_fields=("use_binance_max_leverage",))

        execution = process_telegram_message(
            self.strategy, 105, CHN_SIGNAL, timezone.now()
        )[2]

        self.assertEqual(execution.status, CopyExecution.Status.PAPER_FILLED)
        self.assertEqual(execution.leverage, 20)

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
            {"allocation_usdt": "25", "max_leverage": 7, "max_daily_loss_usdt": "80", "status": "PAUSED"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        strategy.refresh_from_db()
        self.assertEqual(strategy.allocation_usdt, Decimal("25"))
        self.assertEqual(strategy.max_leverage, 7)
        self.assertEqual(strategy.status, CopyStrategy.Status.PAUSED)
