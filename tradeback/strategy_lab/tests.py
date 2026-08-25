from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from .engine import StrategyCompileError, backtest, build_series, compile_strategy
from .models import StrategyDefinition, StrategyPosition, StrategyRuntime, StrategyTrainingRun
from .training import process_training_run, queue_training


CODE = "fast = ta.ema(close, 3)\nslow = ta.ema(close, 8)\nrsi = ta.rsi(close, 4)"
SUPER_BOS_CODE = """
//@version=6
strategy("Supertrend + Market Structure BOS Strategy", overlay=true)
atrPeriod = input.int(10, "ATR Period")
atrMultiplier = input.float(3.0, "ATR Multiplier")
useWilderATR = input.bool(true, "Use Wilder ATR")
pivotLeft = input.int(5, "Pivot bars left")
pivotRight = input.int(5, "Pivot bars right")
breakConfirmation = input.string("Close", "Breakout confirmation")
acceptChoch = input.bool(false, "Accept CHoCH")
trailWithSupertrend = input.bool(true, "Trail")
closeOnOppositeST = input.bool(false, "Opposite")
enableLongs = input.bool(true, "Long")
enableShorts = input.bool(true, "Short")
riskPct = input.float(1.0, "Risk")
riskReward = input.float(2.0, "RR")
maxPositionPct = input.float(100.0, "Maximum")
pivotHigh = ta.pivothigh(high, pivotLeft, pivotRight)
pivotLow = ta.pivotlow(low, pivotLeft, pivotRight)
upBand := close[1] > previousUp ? math.max(upRaw, previousUp) : upRaw
downBand := close[1] < previousDown ? math.min(downRaw, previousDown) : downRaw
var bool longArmed = false
var bool shortArmed = false
bullBreak = not na(lastSwingHigh) and not swingHighBroken and close > lastSwingHigh
bearBreak = not na(lastSwingLow) and not swingLowBroken and close < lastSwingLow
longEntry = enableLongs and inDateRange and strategy.position_size == 0 and longArmed
shortEntry = enableShorts and inDateRange and strategy.position_size == 0 and shortArmed
strategy.entry("Long", strategy.long)
strategy.exit("Long SL/TP", "Long")
"""


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

    def test_compiles_stateful_supertrend_bos_profile(self):
        spec = compile_strategy(SUPER_BOS_CODE, "longEntry", "shortEntry")
        self.assertEqual(spec["engine"], "supertrend_bos_v1")
        self.assertEqual(spec["config"]["pivot_left"], 5)
        series = build_series(candles(300), spec)
        self.assertEqual(len(series["longEntry"]), 300)
        result = backtest(candles(300), spec, Decimal("2"), Decimal("1"))
        self.assertEqual(result["bars_tested"], 300)

    def test_compiles_advanced_series_and_multi_output_indicators(self):
        code = """
src = input.source(hl2, "Source")
fast = ta.hma(src, 9)
[basis, upper, lower] = ta.bb(close, 20, 2)
[macdLine, signalLine, histogram] = ta.macd(close, 12, 26, 9)
momentum = close - close[1]
confirmed = close > basis ? momentum : 0
"""
        spec = compile_strategy(
            code, "close > upper and confirmed > 0", "close < lower and histogram < 0"
        )
        series = build_series(candles(120), spec)
        self.assertIn("histogram", series)
        self.assertIn("confirmed", series)


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

    def test_compile_endpoint_recognizes_stateful_supertrend_bos(self):
        response = self.client.post("/strategies/compile/", {
            "indicator_code": SUPER_BOS_CODE,
            "long_condition": "longEntry", "short_condition": "shortEntry",
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["valid"])
        self.assertEqual(response.data["engine"], "supertrend_bos_v1")

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

    def test_position_history_is_paginated_filtered_and_grouped_by_runtime(self):
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
            mode=StrategyRuntime.Mode.PAPER, status=StrategyRuntime.Status.ACTIVE,
            symbols=["BTCUSDT"], timeframes=["15m"], allocation_per_order=10,
            total_budget=100, max_daily_loss=25, leverage=2, max_open_positions=2,
        )
        common = {
            "runtime": runtime, "symbol": "BTCUSDT", "timeframe": "15m",
            "direction": "LONG", "entry_price": 100, "current_price": 101,
            "quantity": 1, "leverage": 2, "margin_usdt": 10,
            "stop_loss": 99, "take_profit": 102,
        }
        StrategyPosition.objects.create(
            **common, signal_candle_time=1, status=StrategyPosition.Status.OPEN,
            unrealized_pnl=Decimal("4.25"),
        )
        StrategyPosition.objects.create(
            **common, signal_candle_time=2, status=StrategyPosition.Status.CLOSED,
            realized_pnl=Decimal("-3.50"), close_reason="STOP_LOSS",
        )

        open_response = self.client.get("/strategies/positions/?status=OPEN")
        history_response = self.client.get(
            f"/strategies/positions/?history=1&runtime_id={runtime.id}&limit=10"
        )
        with self.assertNumQueries(1):
            executions_response = self.client.get("/strategies/executions/")

        self.assertEqual(open_response.status_code, 200)
        self.assertEqual(len(open_response.data), 1)
        self.assertEqual(history_response.status_code, 200)
        self.assertEqual(history_response.data["count"], 1)
        self.assertEqual(history_response.data["results"][0]["close_reason"], "STOP_LOSS")
        self.assertEqual(executions_response.data[0]["open_positions"], 1)
        self.assertEqual(executions_response.data[0]["closed_positions"], 1)
        self.assertEqual(
            Decimal(executions_response.data[0]["open_unrealized_pnl"]), Decimal("4.25")
        )
        self.assertEqual(
            Decimal(executions_response.data[0]["total_realized_pnl"]), Decimal("-3.50")
        )

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
