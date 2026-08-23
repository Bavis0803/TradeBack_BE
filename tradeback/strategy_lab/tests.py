from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from .engine import StrategyCompileError, backtest, compile_strategy
from .models import StrategyDefinition, StrategyRuntime, StrategyTrainingRun
from .training import process_training_run, queue_training


CODE = "fast = ta.ema(close, 3)\nslow = ta.ema(close, 8)\nrsi = ta.rsi(close, 4)"


def candles(count=120):
    rows = []
    for index in range(count):
        close = 100 + ((index // 10) % 2) * 6 + (index % 10)
        rows.append({
            "open_time": index * 60_000, "close_time": (index + 1) * 60_000 - 1,
            "open": close - .2, "high": close + 1, "low": close - 1,
            "close": close, "volume": 1000,
        })
    return rows


class StrategyEngineTests(TestCase):
    def test_compiles_safe_pine_subset_and_backtests(self):
        spec = compile_strategy(CODE, "ta.crossover(fast, slow)", "ta.crossunder(fast, slow)")
        result = backtest(candles(), spec, Decimal("2"), Decimal("1"))
        self.assertEqual(spec["version"], "safe-pine-v1")
        self.assertGreater(result["total_trades"], 0)
        self.assertEqual(result["winning_trades"] + result["losing_trades"], result["total_trades"])

    def test_rejects_arbitrary_code(self):
        with self.assertRaises(StrategyCompileError):
            compile_strategy("x = request.security('X', '1D', close)", "x > close")


class StrategyAPITests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="strategy-user", email="strategy@example.com", password="secret-pass"
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.symbol_patcher = patch(
            "strategy_lab.serializers.BinanceService.get_active_futures_symbols",
            return_value={"BTCUSDT", "ETHUSDT"},
        )
        self.symbol_patcher.start()
        self.addCleanup(self.symbol_patcher.stop)
        self.payload = {
            "name": "EMA cross", "indicator_code": CODE,
            "long_condition": "ta.crossover(fast, slow)",
            "short_condition": "ta.crossunder(fast, slow)",
            "risk_reward_ratio": "2", "stop_loss_percent": "1",
            "symbols": ["BTCUSDT"], "timeframes": ["15m"], "history_days": 30,
        }

    def test_create_and_queue_training(self):
        response = self.client.post("/strategies/definitions/", self.payload, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        queued = self.client.post(
            f"/strategies/definitions/{response.data['id']}/train/", {}, format="json"
        )
        self.assertEqual(queued.status_code, 202)
        self.assertEqual(queued.data["status"], StrategyTrainingRun.Status.QUEUED)

    @patch(
        "strategy_lab.views.BinanceService.get_active_futures_symbols",
        return_value={"BTCUSDT", "PENGUUSDT"},
    )
    def test_custom_symbol_validation_uses_binance_contracts(self, _symbols):
        valid = self.client.get("/strategies/catalog/symbol/?symbol=PENGU")
        invalid = self.client.get("/strategies/catalog/symbol/?symbol=NOTREAL")
        self.assertTrue(valid.data["valid"])
        self.assertEqual(valid.data["symbol"], "PENGUUSDT")
        self.assertFalse(invalid.data["valid"])

    def test_training_builds_report_and_unlocks_paper_execution(self):
        strategy = StrategyDefinition.objects.create(
            user=self.user, parsed_spec=compile_strategy(
                CODE, "ta.crossover(fast, slow)", "ta.crossunder(fast, slow)"
            ), **self.payload,
        )
        run = queue_training(strategy)
        with patch("strategy_lab.training.fetch_history", return_value=candles()):
            process_training_run(run)
        strategy.refresh_from_db()
        run.refresh_from_db()
        self.assertEqual(strategy.status, StrategyDefinition.Status.TRAINED)
        self.assertEqual(run.status, StrategyTrainingRun.Status.COMPLETED)
        response = self.client.post("/strategies/executions/", {
            "strategy": str(strategy.id), "mode": "PAPER", "status": "ACTIVE",
            "symbols": ["BTCUSDT"], "timeframes": ["15m"],
            "allocation_per_order": "10", "total_budget": "100",
            "max_daily_loss": "25", "leverage": 2, "max_open_positions": 2,
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)

    def test_active_execution_protects_strategy_edit_and_delete(self):
        strategy = StrategyDefinition.objects.create(
            user=self.user, parsed_spec=compile_strategy(
                CODE, "ta.crossover(fast, slow)", "ta.crossunder(fast, slow)"
            ), status=StrategyDefinition.Status.TRAINED, **self.payload,
        )
        run = StrategyTrainingRun.objects.create(
            strategy=strategy, status=StrategyTrainingRun.Status.COMPLETED
        )
        StrategyRuntime.objects.create(
            user=self.user, strategy=strategy, training_run=run,
            mode=StrategyRuntime.Mode.PAPER, status=StrategyRuntime.Status.ACTIVE,
            symbols=["BTCUSDT"], timeframes=["15m"], allocation_per_order=10,
            total_budget=100, max_daily_loss=25, leverage=2, max_open_positions=2,
        )
        patch_response = self.client.patch(
            f"/strategies/definitions/{strategy.id}/", {"name": "unsafe edit"}, format="json"
        )
        delete_response = self.client.delete(f"/strategies/definitions/{strategy.id}/")
        self.assertEqual(patch_response.status_code, 409)
        self.assertEqual(delete_response.status_code, 409)

    def test_paused_execution_without_positions_can_be_deleted(self):
        strategy = StrategyDefinition.objects.create(
            user=self.user, parsed_spec=compile_strategy(
                CODE, "ta.crossover(fast, slow)", "ta.crossunder(fast, slow)"
            ), status=StrategyDefinition.Status.TRAINED, **self.payload,
        )
        run = StrategyTrainingRun.objects.create(
            strategy=strategy, status=StrategyTrainingRun.Status.COMPLETED
        )
        runtime = StrategyRuntime.objects.create(
            user=self.user, strategy=strategy, training_run=run,
            mode=StrategyRuntime.Mode.PAPER, status=StrategyRuntime.Status.PAUSED,
            symbols=["BTCUSDT"], timeframes=["15m"], allocation_per_order=10,
            total_budget=100, max_daily_loss=25, leverage=2, max_open_positions=2,
        )
        response = self.client.delete(f"/strategies/executions/{runtime.id}/")
        self.assertEqual(response.status_code, 204)

    @override_settings(STRATEGY_LIVE_ENABLED=True)
    def test_live_requires_confirm_and_connected_binance(self):
        strategy = StrategyDefinition.objects.create(
            user=self.user, parsed_spec=compile_strategy(
                CODE, "ta.crossover(fast, slow)", "ta.crossunder(fast, slow)"
            ), status=StrategyDefinition.Status.TRAINED, **self.payload,
        )
        run = StrategyTrainingRun.objects.create(
            strategy=strategy, status=StrategyTrainingRun.Status.COMPLETED
        )
        data = {
            "strategy": str(strategy.id), "mode": "LIVE", "symbols": ["BTCUSDT"],
            "timeframes": ["15m"], "allocation_per_order": "10",
            "total_budget": "100", "max_daily_loss": "25", "leverage": 2,
            "max_open_positions": 2,
        }
        response = self.client.post("/strategies/executions/", data, format="json")
        self.assertEqual(response.status_code, 400)
        data["confirm_live"] = True
        response = self.client.post("/strategies/executions/", data, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("mode", response.data)
