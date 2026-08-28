from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from .dashboard import build_dashboard_payload, persist_portfolio_history
from .models import (
    ExchangeAccount,
    ExchangeCredential,
    PortfolioSnapshot,
    TradeLog,
    TradeSyncState,
)
from .services import BinanceService, calculate_risk_reward, calculate_risk_sized_order


def symbol_context():
    return {
        "symbol": "BTCUSDT",
        "base_asset": "BTC",
        "quote_asset": "USDT",
        "current_price": Decimal("60000"),
        "price_precision": 2,
        "quantity_precision": 3,
        "min_volume": Decimal("0.001"),
        "max_volume": Decimal("1000"),
        "volume_step": Decimal("0.001"),
        "min_notional": Decimal("5"),
        "symbols": [{"symbol": "BTCUSDT", "base_asset": "BTC", "quote_asset": "USDT"}],
    }


def leverage_brackets():
    return [{
        "initial_leverage": 20,
        "notional_floor": Decimal("0"),
        "notional_cap": Decimal("1000000"),
        "maint_margin_ratio": Decimal("0.004"),
    }]


class CalculatorDomainTests(SimpleTestCase):
    @patch.object(BinanceService, "_request_json", return_value={"symbols": []})
    def test_exchange_information_is_cached(self, mock_request):
        cache.clear()
        service = BinanceService()
        service.get_exchange_info()
        service.get_exchange_info()
        mock_request.assert_called_once_with("/fapi/v1/exchangeInfo")

    def test_calculates_results_from_volume_and_leverage(self):
        result = calculate_risk_reward(
            {
                "direction": "LONG",
                "entry_price": Decimal("60000"),
                "stop_loss": Decimal("59000"),
                "take_profit": Decimal("62000"),
                "volume": Decimal("0.01"),
                "leverage": 10,
            },
            Decimal("1000"),
            symbol_context(),
            leverage_brackets(),
        )
        self.assertEqual(result["risk_amount"], "10")
        self.assertEqual(result["potential_profit"], "20")
        self.assertEqual(result["risk_reward_ratio"], "2")
        self.assertEqual(result["margin_required"], "60")

    def test_rejects_order_when_margin_exceeds_balance(self):
        with self.assertRaisesRegex(ValueError, "exceeds account balance"):
            calculate_risk_reward(
                {
                    "direction": "LONG",
                    "entry_price": Decimal("60000"),
                    "stop_loss": Decimal("59000"),
                    "take_profit": Decimal("62000"),
                    "volume": Decimal("1"),
                    "leverage": 10,
                },
                Decimal("1000"),
                symbol_context(),
                leverage_brackets(),
            )

    def test_risk_sizing_derives_leverage_and_caps_loss_at_stop(self):
        brackets = [{
            "initial_leverage": 125, "notional_floor": Decimal("0"),
            "notional_cap": Decimal("1000000"), "maint_margin_ratio": Decimal("0.004"),
        }]
        result = calculate_risk_sized_order(
            {
                "direction": "LONG", "entry_price": Decimal("60000"),
                "stop_loss": Decimal("57000"), "take_profit": Decimal("66000"),
            },
            Decimal("5"), symbol_context(), brackets, leverage_cap=20,
        )

        self.assertEqual(result["leverage"], 15)
        self.assertEqual(result["risk_reward_ratio"], "2")
        self.assertLessEqual(Decimal(result["risk_amount"]), Decimal("5"))
        self.assertLessEqual(Decimal(result["margin_required"]), Decimal("5"))
        self.assertLess(Decimal(result["estimated_liquidation_price"]), Decimal("57000"))

    def test_signal_leverage_does_not_override_risk_formula(self):
        brackets = [{
            "initial_leverage": 125, "notional_floor": Decimal("0"),
            "notional_cap": Decimal("1000000"), "maint_margin_ratio": Decimal("0.004"),
        }]
        result = calculate_risk_sized_order(
            {
                "direction": "LONG", "entry_price": Decimal("60000"),
                "stop_loss": Decimal("57000"), "take_profit": Decimal("66000"),
            },
            Decimal("5"), symbol_context(), brackets, leverage_cap=125,
            requested_leverage=50,
        )

        self.assertEqual(result["leverage"], 15)
        self.assertEqual(result["leverage_source"], "RISK_FORMULA")

    def test_risk_percentage_divided_by_stop_percentage_sets_leverage(self):
        brackets = [{
            "initial_leverage": 125, "notional_floor": Decimal("0"),
            "notional_cap": Decimal("1000000"), "maint_margin_ratio": Decimal("0.004"),
        }]
        result = calculate_risk_sized_order(
            {
                "direction": "LONG", "entry_price": Decimal("100"),
                "stop_loss": Decimal("99.52"), "take_profit": Decimal("101"),
            },
            Decimal("5"), symbol_context(), brackets, leverage_cap=125,
            requested_leverage=10, risk_budget=Decimal("1.5"),
        )

        # Leverage rounds up; volume is capped separately so SL risk cannot exceed 1.5 USDT.
        self.assertEqual(result["leverage"], 63)
        self.assertEqual(result["leverage_source"], "RISK_FORMULA")
        self.assertLessEqual(Decimal(result["risk_amount"]), Decimal("1.5"))
        self.assertLessEqual(Decimal(result["margin_required"]), Decimal("5"))

    def test_sui_sizing_rounds_leverage_up_and_uses_the_risk_budget(self):
        context = {
            **symbol_context(), "volume_step": Decimal("0.1"),
            "min_volume": Decimal("0.1"), "min_notional": Decimal("5"),
        }
        brackets = [{
            "initial_leverage": 125, "notional_floor": Decimal("0"),
            "notional_cap": Decimal("1000000"), "maint_margin_ratio": Decimal("0.004"),
        }]
        result = calculate_risk_sized_order(
            {
                "direction": "LONG", "entry_price": Decimal("0.784"),
                "stop_loss": Decimal("0.744"), "take_profit": Decimal("0.820"),
            },
            Decimal("5"), context, brackets, leverage_cap=125,
            risk_budget=Decimal("1.5"),
        )

        self.assertEqual(result["leverage"], 6)
        self.assertEqual(result["volume"], "37.5")
        self.assertEqual(result["risk_amount"], "1.5")
        self.assertEqual(result["margin_required"], "4.9")

    def test_short_risk_sizing_keeps_liquidation_above_stop(self):
        brackets = [{
            "initial_leverage": 125, "notional_floor": Decimal("0"),
            "notional_cap": Decimal("1000000"), "maint_margin_ratio": Decimal("0.004"),
        }]
        result = calculate_risk_sized_order(
            {
                "direction": "SHORT", "entry_price": Decimal("100"),
                "stop_loss": Decimal("105"), "take_profit": Decimal("90"),
            },
            Decimal("5"), symbol_context(), brackets, leverage_cap=20,
        )

        self.assertEqual(result["risk_reward_ratio"], "2")
        self.assertLessEqual(Decimal(result["risk_amount"]), Decimal("5"))
        self.assertGreater(Decimal(result["estimated_liquidation_price"]), Decimal("105"))

    def test_separate_risk_budget_limits_loss_without_exceeding_margin(self):
        brackets = [{
            "initial_leverage": 125, "notional_floor": Decimal("0"),
            "notional_cap": Decimal("1000000"), "maint_margin_ratio": Decimal("0.004"),
        }]
        result = calculate_risk_sized_order(
            {
                "direction": "LONG", "entry_price": Decimal("100"),
                "stop_loss": Decimal("95"), "take_profit": Decimal("110"),
            },
            Decimal("5"), symbol_context(), brackets, leverage_cap=20,
            risk_budget=Decimal("1"),
        )

        self.assertLessEqual(Decimal(result["risk_amount"]), Decimal("1"))
        self.assertLessEqual(Decimal(result["margin_required"]), Decimal("5"))
        self.assertEqual(result["risk_budget"], "1")


class RiskRewardAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()

    @patch("exchanges.views.BinanceService.get_symbol_context", return_value=symbol_context())
    def test_demo_context_has_editable_balance_and_public_market_data(self, _mock_context):
        response = self.client.get("/exchange/risk-reward/context/?symbol=BTCUSDT")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["current_price"], "60000")
        self.assertEqual(response.data["account"]["mode"], "demo")
        self.assertTrue(response.data["account"]["balance_editable"])

    @patch("exchanges.views.BinanceService.get_symbol_context")
    def test_context_can_omit_the_large_symbol_list(self, mock_context):
        compact_context = symbol_context()
        compact_context.pop("symbols")
        mock_context.return_value = compact_context
        response = self.client.get(
            "/exchange/risk-reward/context/?symbol=BTCUSDT&include_symbols=false"
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("symbols", response.data)
        mock_context.assert_called_once_with("BTCUSDT", include_symbols=False)

    @patch("exchanges.views.BinanceService.get_symbol_context", return_value=symbol_context())
    def test_demo_calculation_requires_and_uses_fake_balance(self, _mock_context):
        response = self.client.post(
            "/exchange/risk-reward/calculate/",
            {
                "symbol": "BTCUSDT",
                "direction": "LONG",
                "entry_price": "60000",
                "stop_loss": "59000",
                "take_profit": "62000",
                "volume": "0.01",
                "leverage": 10,
                "account_balance": "1000",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["account_mode"], "demo")
        self.assertEqual(response.data["calculation"]["risk_reward_ratio"], "2")

    @patch("exchanges.views.BinanceService.get_leverage_brackets", return_value=leverage_brackets())
    @patch("exchanges.views.BinanceService.get_usdt_balance", return_value=Decimal("500"))
    @patch("exchanges.views.BinanceService.get_symbol_context", return_value=symbol_context())
    def test_connected_calculation_ignores_client_balance(
        self, _mock_context, _mock_balance, _mock_brackets
    ):
        user = get_user_model().objects.create_user(
            username="trader", email="trader@example.com", password="secret-pass"
        )
        account = ExchangeAccount.objects.create(
            user=user,
            exchange="BINANCE",
            status=ExchangeAccount.Status.CONNECTED,
        )
        ExchangeCredential.objects.create(
            account=account, api_key="key", api_secret="secret"
        )
        self.client.force_authenticate(user)
        response = self.client.post(
            "/exchange/risk-reward/calculate/",
            {
                "symbol": "BTCUSDT",
                "direction": "LONG",
                "entry_price": "60000",
                "stop_loss": "59000",
                "take_profit": "62000",
                "volume": "0.01",
                "leverage": 10,
                "account_balance": "999999",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["account_mode"], "connected")
        self.assertEqual(response.data["account_balance"], "500")


class ExchangeConnectionAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(
            username="exchange-user",
            email="exchange@example.com",
            password="secret-pass",
        )
        self.client.force_authenticate(self.user)

    def test_credentials_are_encrypted_at_rest_and_round_trip(self):
        account = ExchangeAccount.objects.create(
            user=self.user,
            exchange="BINANCE",
            status=ExchangeAccount.Status.CONNECTED,
        )
        credential = ExchangeCredential.objects.create(
            account=account,
            api_key="real-api-key",
            api_secret="real-api-secret",
        )

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT api_key, api_secret FROM exchanges_exchangecredential WHERE id = %s",
                [credential.pk],
            )
            stored_key, stored_secret = cursor.fetchone()

        self.assertTrue(stored_key.startswith("enc:v1:"))
        self.assertTrue(stored_secret.startswith("enc:v1:"))
        self.assertNotIn("real-api-key", stored_key)
        self.assertNotIn("real-api-secret", stored_secret)
        credential.refresh_from_db()
        self.assertEqual(credential.api_key, "real-api-key")
        self.assertEqual(credential.api_secret, "real-api-secret")

    @patch(
        "exchanges.connection.BinanceService.verify_credentials",
        return_value={"success": True, "can_read_futures": True},
    )
    def test_connect_status_and_disconnect_never_return_secrets(self, _mock_verify):
        response = self.client.post(
            "/exchange/check/",
            {
                "api_key": "my-binance-api-key",
                "api_secret": "my-binance-secret",
                "is_testnet": True,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        response_text = str(response.data)
        self.assertNotIn("my-binance-api-key", response_text)
        self.assertNotIn("my-binance-secret", response_text)
        self.assertEqual(response.data["account"]["api_key_hint"], "my-b****-key")

        account = ExchangeAccount.objects.get(user=self.user, exchange="BINANCE")
        self.assertEqual(account.status, ExchangeAccount.Status.CONNECTED)
        self.assertTrue(account.is_testnet)
        self.assertTrue(hasattr(account, "credential"))

        status_response = self.client.get("/exchange/status/")
        self.assertEqual(status_response.status_code, 200)
        self.assertTrue(status_response.data["connected"])
        self.assertNotIn("secret", str(status_response.data).lower())

        disconnect_response = self.client.delete("/exchange/disconnect/")
        self.assertEqual(disconnect_response.status_code, 200)
        self.assertFalse(ExchangeAccount.objects.filter(pk=account.pk).exists())
        self.assertFalse(ExchangeCredential.objects.filter(account_id=account.pk).exists())

    @patch(
        "exchanges.connection.BinanceService.verify_credentials",
        return_value={"success": True, "can_read_futures": True},
    )
    def test_verify_refreshes_connection_health(self, _mock_verify):
        account = ExchangeAccount.objects.create(
            user=self.user,
            exchange="BINANCE",
            status=ExchangeAccount.Status.ERROR,
            last_error="Previous failure",
        )
        ExchangeCredential.objects.create(
            account=account, api_key="key", api_secret="secret"
        )

        response = self.client.post("/exchange/verify/", {}, format="json")
        self.assertEqual(response.status_code, 200)
        account.refresh_from_db()
        self.assertEqual(account.status, ExchangeAccount.Status.CONNECTED)
        self.assertEqual(account.last_error, "")
        self.assertIsNotNone(account.last_verified_at)

    def test_connection_endpoints_require_tradeback_authentication(self):
        self.client.force_authenticate(user=None)
        for method, path in (
            (self.client.get, "/exchange/status/"),
            (self.client.post, "/exchange/check/"),
            (self.client.post, "/exchange/verify/"),
            (self.client.delete, "/exchange/disconnect/"),
        ):
            response = method(path)
            self.assertIn(response.status_code, (401, 403))


class DashboardAPITests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(
            username="dashboard-user",
            email="dashboard@example.com",
            password="secret-pass",
        )
        self.account = ExchangeAccount.objects.create(
            user=self.user,
            exchange="BINANCE",
            status=ExchangeAccount.Status.CONNECTED,
        )
        ExchangeCredential.objects.create(
            account=self.account, api_key="key", api_secret="secret"
        )
        self.client.force_authenticate(self.user)

    @patch("exchanges.services.BinanceService.get_futures_user_trades")
    @patch("exchanges.services.BinanceService.get_spot_tickers")
    @patch("exchanges.services.BinanceService.get_spot_account")
    @patch("exchanges.services.BinanceService.get_futures_tickers")
    @patch("exchanges.services.BinanceService.get_futures_income")
    @patch("exchanges.services.BinanceService.get_futures_account")
    def test_dashboard_uses_real_account_shape_and_persists_snapshots(
        self,
        mock_futures_account,
        mock_income,
        mock_futures_tickers,
        mock_spot_account,
        mock_spot_tickers,
        mock_user_trades,
    ):
        now_ms = int(timezone.now().timestamp() * 1000)
        mock_futures_account.return_value = {
            "totalMarginBalance": "500",
            "assets": [{"asset": "USDT", "marginBalance": "500"}],
            "positions": [{
                "symbol": "BTCUSDT",
                "positionAmt": "0.01",
                "entryPrice": "60000",
                "unrealizedProfit": "10",
                "leverage": "10",
            }],
        }
        mock_income.return_value = [{
            "symbol": "BTCUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "25",
            "time": now_ms,
        }]
        mock_futures_tickers.return_value = [{
            "symbol": "BTCUSDT", "lastPrice": "63000", "priceChangePercent": "2.5"
        }]
        mock_spot_account.return_value = {
            "balances": [
                {"asset": "BTC", "free": "0.01", "locked": "0"},
                {"asset": "USDT", "free": "100", "locked": "0"},
            ]
        }
        mock_spot_tickers.return_value = [{
            "symbol": "BTCUSDT", "lastPrice": "63000", "priceChangePercent": "2.5"
        }]
        mock_user_trades.return_value = [{
            "id": 77,
            "orderId": 88,
            "symbol": "BTCUSDT",
            "side": "SELL",
            "price": "63000",
            "qty": "0.01",
            "quoteQty": "630",
            "realizedPnl": "25",
            "commission": "0.25",
            "commissionAsset": "USDT",
            "time": now_ms,
        }]

        response = self.client.get("/exchange/dashboard/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["summary"]["total_portfolio_usdt"], "1230")
        self.assertEqual(response.data["summary"]["open_positions"], 1)
        self.assertEqual(response.data["summary"]["win_rate_30d"], "100")
        self.assertAlmostEqual(
            Decimal(response.data["summary"]["pnl_24h_percent"]),
            Decimal("25") * Decimal("100") / Decimal("475"),
        )
        self.assertEqual(response.data["recent_trades"][0]["side"], "SELL")
        self.assertEqual(PortfolioSnapshot.objects.filter(account=self.account).count(), 30)
        self.assertEqual(TradeLog.objects.filter(account=self.account).count(), 1)

        self.client.get("/exchange/dashboard/")
        mock_futures_account.assert_called_once()

    def test_dashboard_requires_a_connected_binance_account(self):
        self.account.delete()
        response = self.client.get("/exchange/dashboard/")
        self.assertEqual(response.status_code, 409)

    def test_estimated_history_never_overwrites_a_live_daily_snapshot(self):
        yesterday = timezone.localdate() - timedelta(days=1)
        PortfolioSnapshot.objects.create(
            account=self.account,
            snapshot_date=yesterday,
            total_value_usdt=Decimal("50"),
            spot_value_usdt=Decimal("10"),
            futures_value_usdt=Decimal("40"),
            source=PortfolioSnapshot.Source.LIVE,
        )

        persist_portfolio_history(
            self.account,
            total_value=Decimal("60"),
            spot_value=Decimal("10"),
            futures_value=Decimal("50"),
            incomes=[{
                "time": int(timezone.now().timestamp() * 1000),
                "income": "10",
            }],
        )

        snapshot = PortfolioSnapshot.objects.get(
            account=self.account, snapshot_date=yesterday
        )
        self.assertEqual(snapshot.source, PortfolioSnapshot.Source.LIVE)
        self.assertEqual(snapshot.total_value_usdt, Decimal("50"))

    def test_estimated_performance_excludes_transfers_and_refreshes_old_estimates(self):
        yesterday = timezone.localdate() - timedelta(days=1)
        PortfolioSnapshot.objects.create(
            account=self.account,
            snapshot_date=yesterday,
            total_value_usdt=Decimal("0.001"),
            futures_value_usdt=Decimal("0.001"),
            source=PortfolioSnapshot.Source.ESTIMATED,
        )
        now_ms = int(timezone.now().timestamp() * 1000)

        persist_portfolio_history(
            self.account,
            total_value=Decimal("60"),
            spot_value=Decimal("0"),
            futures_value=Decimal("60"),
            incomes=[
                {"time": now_ms, "incomeType": "TRANSFER", "income": "100"},
                {"time": now_ms, "incomeType": "REALIZED_PNL", "income": "-5"},
            ],
        )

        snapshot = PortfolioSnapshot.objects.get(
            account=self.account, snapshot_date=yesterday
        )
        self.assertEqual(snapshot.source, PortfolioSnapshot.Source.ESTIMATED)
        self.assertEqual(snapshot.total_value_usdt, Decimal("65"))

    @patch("exchanges.dashboard._build_dashboard_payload")
    def test_concurrent_dashboard_request_uses_stale_redis_snapshot(self, mock_build):
        prefix = f"binance-dashboard:v3:{self.account.pk}"
        stale = {"as_of": "cached", "summary": {"total_portfolio_usdt": "50"}}
        cache.set(f"{prefix}:stale", stale, timeout=300)
        cache.set(f"{prefix}:build-lock", "1", timeout=20)

        result = build_dashboard_payload(self.account)

        self.assertEqual(result, stale)
        mock_build.assert_not_called()


class TransactionLogAPITests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(
            username="log-user",
            email="logs@example.com",
            password="secret-pass",
        )
        self.account = ExchangeAccount.objects.create(
            user=self.user,
            exchange="BINANCE",
            status=ExchangeAccount.Status.CONNECTED,
        )
        ExchangeCredential.objects.create(
            account=self.account, api_key="key", api_secret="secret"
        )
        self.client.force_authenticate(self.user)

    def test_draft_trade_lifecycle_and_filtered_stats(self):
        response = self.client.post(
            "/exchange/transactions/",
            {
                "market": "FUTURES",
                "symbol": "BTC/USDT",
                "side": "BUY",
                "price": "60000",
                "quantity": "0.01",
                "stop_loss": "59000",
                "take_profit": "62000",
                "leverage": 10,
                "note": "Paper setup",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["source"], "DRAFT")
        self.assertEqual(response.data["status"], "DRAFT")
        self.assertEqual(response.data["quote_quantity"], "600.000000000000")

        trade_id = response.data["id"]
        update = self.client.patch(
            f"/exchange/transactions/{trade_id}/",
            {"status": "FILLED", "realized_pnl": "20"},
            format="json",
        )
        self.assertEqual(update.status_code, 200)

        listing = self.client.get("/exchange/transactions/?source=DRAFT&side=BUY")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.data["count"], 1)
        self.assertEqual(listing.data["stats"]["gross_pnl"], "20")
        self.assertEqual(listing.data["stats"]["win_rate"], "100")

        delete = self.client.delete(f"/exchange/transactions/{trade_id}/")
        self.assertEqual(delete.status_code, 204)
        self.assertFalse(TradeLog.objects.filter(user=self.user).exists())

    def test_list_query_count_stays_constant_for_thousands_of_logs(self):
        now = timezone.now()
        TradeLog.objects.bulk_create(
            [
                TradeLog(
                    user=self.user,
                    account=self.account,
                    source=TradeLog.Source.DRAFT,
                    status=TradeLog.Status.DRAFT,
                    market=TradeLog.Market.FUTURES,
                    symbol="BTCUSDT",
                    side="BUY",
                    price="60000",
                    quantity="0.001",
                    quote_quantity="60",
                    executed_at=now,
                )
                for _ in range(10000)
            ],
            batch_size=500,
        )
        with self.assertNumQueries(3):
            response = self.client.get("/exchange/transactions/?page_size=50")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 10000)
        self.assertEqual(len(response.data["results"]), 50)

    @patch("exchanges.services.BinanceService.get_spot_exchange_info", return_value={"symbols": []})
    @patch("exchanges.services.BinanceService.get_spot_account", return_value={"balances": []})
    @patch("exchanges.services.BinanceService.get_futures_account", return_value={"positions": []})
    @patch("exchanges.services.BinanceService.get_futures_income")
    @patch("exchanges.services.BinanceService.get_futures_user_trades")
    def test_real_trade_sync_is_incremental_and_deduplicated(
        self,
        mock_user_trades,
        mock_income,
        _mock_futures_account,
        _mock_spot_account,
        _mock_spot_info,
    ):
        now_ms = int(timezone.now().timestamp() * 1000)
        mock_income.return_value = [{"symbol": "BTCUSDT", "time": now_ms}]
        mock_user_trades.return_value = [{
            "id": 99,
            "orderId": 100,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "price": "60000",
            "qty": "0.01",
            "quoteQty": "600",
            "realizedPnl": "5",
            "commission": "0.2",
            "commissionAsset": "USDT",
            "time": now_ms,
        }]
        first = self.client.post("/exchange/transactions/sync/")
        TradeSyncState.objects.update(
            last_synced_at=timezone.now() - timedelta(minutes=2)
        )
        second = self.client.post("/exchange/transactions/sync/")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data["created"], 1)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(TradeLog.objects.filter(source="BINANCE").count(), 1)
        self.assertEqual(mock_user_trades.call_args_list[-1].kwargs["from_id"], 100)
