from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from exchanges.services import BinanceService

from .engine import backtest, parse_klines, timestamp_datetime
from .models import StrategyBacktestResult, StrategyDefinition, StrategyTrainingRun


MAX_BARS_PER_MARKET = 5000
SUPPORTED_TIMEFRAMES = ("5m", "15m", "1h", "4h", "1d")


def fetch_history(service, symbol, timeframe, history_days):
    start_ms = int((timezone.now() - timedelta(days=history_days)).timestamp() * 1000)
    end_ms = int(timezone.now().timestamp() * 1000)
    rows = []
    cursor = start_ms
    while cursor < end_ms and len(rows) < MAX_BARS_PER_MARKET:
        batch = service.get_futures_klines(
            symbol, timeframe, start_time=cursor, end_time=end_ms,
            limit=min(1500, MAX_BARS_PER_MARKET - len(rows)),
        )
        if not batch:
            break
        rows.extend(batch)
        next_cursor = int(batch[-1][6]) + 1
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(batch) < 1500:
            break
    return parse_klines(rows)


def _snapshot(strategy):
    return {
        "version": strategy.version,
        "name": strategy.name,
        "parsed_spec": strategy.parsed_spec,
        "risk_reward_ratio": str(strategy.risk_reward_ratio),
        "stop_loss_percent": str(strategy.stop_loss_percent),
        "symbols": strategy.symbols,
        "timeframes": strategy.timeframes,
        "history_days": strategy.history_days,
        "max_bars_per_market": MAX_BARS_PER_MARKET,
    }


def queue_training(strategy):
    if strategy.training_runs.filter(
        status__in=(StrategyTrainingRun.Status.QUEUED, StrategyTrainingRun.Status.RUNNING)
    ).exists():
        raise ValueError("This strategy already has an active training run.")
    strategy.status = StrategyDefinition.Status.QUEUED
    strategy.last_error = ""
    strategy.save(update_fields=("status", "last_error", "updated_at"))
    return StrategyTrainingRun.objects.create(
        strategy=strategy,
        config_snapshot=_snapshot(strategy),
    )


def claim_next_training():
    with transaction.atomic():
        run = StrategyTrainingRun.objects.select_for_update(skip_locked=True).filter(
            status=StrategyTrainingRun.Status.QUEUED
        ).select_related("strategy").order_by("queued_at").first()
        if not run:
            return None
        now = timezone.now()
        run.status = StrategyTrainingRun.Status.RUNNING
        run.started_at = now
        run.progress_percent = 1
        run.save(update_fields=("status", "started_at", "progress_percent"))
        run.strategy.status = StrategyDefinition.Status.TRAINING
        run.strategy.save(update_fields=("status", "updated_at"))
        return run


def process_training_run(run, service=None):
    service = service or BinanceService()
    strategy = StrategyDefinition.objects.get(pk=run.strategy_id)
    combinations = [(symbol, timeframe) for symbol in strategy.symbols for timeframe in strategy.timeframes]
    try:
        StrategyBacktestResult.objects.filter(training_run=run).delete()
        summaries = []
        for index, (symbol, timeframe) in enumerate(combinations, start=1):
            candles = fetch_history(service, symbol, timeframe, strategy.history_days)
            if len(candles) < 50:
                result = {
                    "bars_tested": len(candles), "total_trades": 0, "winning_trades": 0,
                    "losing_trades": 0, "win_rate": 0, "net_return_percent": 0,
                    "profit_factor": 0, "max_drawdown_percent": 0, "trades": [],
                    "equity_curve": [],
                }
            else:
                result = backtest(
                    candles, strategy.parsed_spec, strategy.risk_reward_ratio,
                    strategy.stop_loss_percent,
                )
            row = StrategyBacktestResult.objects.create(
                training_run=run, symbol=symbol, timeframe=timeframe,
                bars_tested=result["bars_tested"], total_trades=result["total_trades"],
                winning_trades=result["winning_trades"], losing_trades=result["losing_trades"],
                win_rate=Decimal(str(round(result["win_rate"], 3))),
                net_return_percent=Decimal(str(round(result["net_return_percent"], 4))),
                profit_factor=Decimal(str(round(result["profit_factor"], 4))),
                max_drawdown_percent=Decimal(str(round(result["max_drawdown_percent"], 4))),
                trades=result["trades"], equity_curve=result["equity_curve"],
                period_start=timestamp_datetime(candles[0]["open_time"]) if candles else None,
                period_end=timestamp_datetime(candles[-1]["close_time"]) if candles else None,
            )
            summaries.append(row)
            StrategyTrainingRun.objects.filter(pk=run.pk).update(
                progress_percent=max(1, int(index * 100 / len(combinations)))
            )

        total = sum(item.total_trades for item in summaries)
        wins = sum(item.winning_trades for item in summaries)
        losses = sum(item.losing_trades for item in summaries)
        best = max(
            summaries,
            key=lambda item: (item.net_return_percent, item.win_rate, item.total_trades),
            default=None,
        )
        summary = {
            "markets_tested": len(summaries), "total_trades": total,
            "winning_trades": wins, "losing_trades": losses,
            "win_rate": round(wins * 100 / total, 3) if total else 0,
            "average_return_percent": round(
                sum(float(item.net_return_percent) for item in summaries) / len(summaries), 4
            ) if summaries else 0,
            "best_market": ({
                "symbol": best.symbol, "timeframe": best.timeframe,
                "win_rate": float(best.win_rate),
                "net_return_percent": float(best.net_return_percent),
                "total_trades": best.total_trades,
            } if best else None),
        }
        now = timezone.now()
        run.status = StrategyTrainingRun.Status.COMPLETED
        run.progress_percent = 100
        run.summary = summary
        run.completed_at = now
        run.error = ""
        run.save(update_fields=("status", "progress_percent", "summary", "completed_at", "error"))
        strategy.status = StrategyDefinition.Status.TRAINED
        strategy.trained_at = now
        strategy.last_error = ""
        strategy.save(update_fields=("status", "trained_at", "last_error", "updated_at"))
        return run
    except Exception as exc:
        message = str(exc)[:500]
        run.status = StrategyTrainingRun.Status.FAILED
        run.error = message
        run.completed_at = timezone.now()
        run.save(update_fields=("status", "error", "completed_at"))
        strategy.status = StrategyDefinition.Status.FAILED
        strategy.last_error = message
        strategy.save(update_fields=("status", "last_error", "updated_at"))
        return run
