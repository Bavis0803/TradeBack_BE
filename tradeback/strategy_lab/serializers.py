from decimal import Decimal

from django.conf import settings
from rest_framework import serializers

from exchanges.connection import get_binance_account
from exchanges.models import ExchangeAccount

from .engine import StrategyCompileError, compile_strategy
from .models import (
    StrategyBacktestResult, StrategyDefinition, StrategyPosition, StrategyRuntime,
    StrategyTrainingRun,
)
from .training import SUPPORTED_TIMEFRAMES


class StrategyDefinitionSerializer(serializers.ModelSerializer):
    latest_training_id = serializers.SerializerMethodField()

    class Meta:
        model = StrategyDefinition
        fields = (
            "id", "name", "description", "indicator_code", "long_condition",
            "short_condition", "risk_reward_ratio", "stop_loss_percent", "symbols",
            "timeframes", "history_days", "status", "version", "last_error",
            "trained_at", "latest_training_id", "created_at", "updated_at",
        )
        read_only_fields = (
            "id", "status", "version", "last_error", "trained_at",
            "latest_training_id", "created_at", "updated_at",
        )

    def get_latest_training_id(self, obj):
        run = next(iter(obj.training_runs.all()), None)
        return str(run.id) if run else None

    def validate_symbols(self, value):
        if len(value) > 20:
            raise serializers.ValidationError("Select at most 20 symbols per strategy.")
        normalized = []
        for item in value:
            symbol = str(item).upper().replace("/", "").strip()
            if not symbol.endswith("USDT") or not symbol.isalnum():
                raise serializers.ValidationError(f"Invalid USDT perpetual symbol: {item}.")
            if symbol not in normalized:
                normalized.append(symbol)
        return normalized

    def validate_timeframes(self, value):
        unique = list(dict.fromkeys(value))
        if not unique or len(unique) > 5:
            raise serializers.ValidationError("Select between 1 and 5 timeframes.")
        invalid = set(unique) - set(SUPPORTED_TIMEFRAMES)
        if invalid:
            raise serializers.ValidationError(f"Unsupported timeframes: {', '.join(sorted(invalid))}.")
        return unique

    def validate(self, attrs):
        instance = self.instance
        code = attrs.get("indicator_code", getattr(instance, "indicator_code", ""))
        long_condition = attrs.get("long_condition", getattr(instance, "long_condition", ""))
        short_condition = attrs.get("short_condition", getattr(instance, "short_condition", ""))
        try:
            attrs["parsed_spec"] = compile_strategy(code, long_condition, short_condition)
        except StrategyCompileError as exc:
            raise serializers.ValidationError({"indicator_code": str(exc)}) from exc
        return attrs

    def update(self, instance, validated_data):
        instance = super().update(instance, validated_data)
        instance.status = StrategyDefinition.Status.DRAFT
        instance.version += 1
        instance.last_error = ""
        instance.save(update_fields=("status", "version", "last_error", "updated_at"))
        return instance


class BacktestResultSerializer(serializers.ModelSerializer):
    class Meta:
        model = StrategyBacktestResult
        fields = (
            "symbol", "timeframe", "bars_tested", "total_trades", "winning_trades",
            "losing_trades", "win_rate", "net_return_percent", "profit_factor",
            "max_drawdown_percent", "trades", "equity_curve", "period_start", "period_end",
        )


class TrainingRunSerializer(serializers.ModelSerializer):
    strategy_name = serializers.CharField(source="strategy.name", read_only=True)
    results = BacktestResultSerializer(many=True, read_only=True)

    class Meta:
        model = StrategyTrainingRun
        fields = (
            "id", "strategy", "strategy_name", "status", "progress_percent", "summary",
            "error", "queued_at", "started_at", "completed_at", "results",
        )


class StrategyPositionSerializer(serializers.ModelSerializer):
    strategy_name = serializers.CharField(source="runtime.strategy.name", read_only=True)
    mode = serializers.CharField(source="runtime.mode", read_only=True)

    class Meta:
        model = StrategyPosition
        fields = (
            "id", "runtime", "strategy_name", "mode", "symbol", "timeframe", "direction",
            "status", "entry_price", "current_price", "quantity", "leverage", "margin_usdt",
            "stop_loss", "take_profit", "unrealized_pnl", "realized_pnl", "close_reason",
            "error", "opened_at", "closed_at", "updated_at",
        )


class StrategyRuntimeSerializer(serializers.ModelSerializer):
    strategy_name = serializers.CharField(source="strategy.name", read_only=True)
    open_positions = serializers.SerializerMethodField()
    total_realized_pnl = serializers.SerializerMethodField()
    confirm_live = serializers.BooleanField(write_only=True, required=False, default=False)

    class Meta:
        model = StrategyRuntime
        fields = (
            "id", "strategy", "strategy_name", "training_run", "mode", "status", "symbols",
            "timeframes", "allocation_per_order", "total_budget", "max_daily_loss",
            "leverage", "max_open_positions", "last_error", "open_positions",
            "total_realized_pnl", "confirm_live", "created_at", "updated_at",
        )
        read_only_fields = (
            "id", "training_run", "last_error", "open_positions", "total_realized_pnl",
            "created_at", "updated_at",
        )

    def get_open_positions(self, obj):
        return obj.positions.filter(status=StrategyPosition.Status.OPEN).count()

    def get_total_realized_pnl(self, obj):
        return str(sum(
            (item.realized_pnl for item in obj.positions.filter(status=StrategyPosition.Status.CLOSED)),
            Decimal("0"),
        ))

    def validate(self, attrs):
        request = self.context["request"]
        strategy = attrs.get("strategy", getattr(self.instance, "strategy", None))
        if not strategy or strategy.user_id != request.user.id:
            raise serializers.ValidationError({"strategy": "Strategy not found."})
        if self.instance and strategy.pk != self.instance.strategy_id:
            raise serializers.ValidationError({"strategy": "An execution cannot change strategy."})
        if not self.instance and strategy.status != StrategyDefinition.Status.TRAINED:
            raise serializers.ValidationError({"strategy": "Train this strategy successfully before execution."})
        symbols = attrs.get("symbols", getattr(self.instance, "symbols", []))
        timeframes = attrs.get("timeframes", getattr(self.instance, "timeframes", []))
        if not symbols or not set(symbols).issubset(set(strategy.symbols)):
            raise serializers.ValidationError({"symbols": "Choose symbols from the completed training run."})
        if not timeframes or not set(timeframes).issubset(set(strategy.timeframes)):
            raise serializers.ValidationError({"timeframes": "Choose timeframes from the completed training run."})
        allocation = attrs.get("allocation_per_order", getattr(self.instance, "allocation_per_order", 0))
        budget = attrs.get("total_budget", getattr(self.instance, "total_budget", 0))
        if allocation > budget:
            raise serializers.ValidationError({"total_budget": "Budget must cover at least one order."})
        if budget > Decimal(str(settings.STRATEGY_MAX_BUDGET_USDT)):
            raise serializers.ValidationError({"total_budget": "Budget exceeds the server safety limit."})
        mode = attrs.get("mode", getattr(self.instance, "mode", StrategyRuntime.Mode.PAPER))
        live_transition = (
            mode == StrategyRuntime.Mode.LIVE
            and (not self.instance or self.instance.mode != StrategyRuntime.Mode.LIVE)
        )
        if live_transition and not settings.STRATEGY_LIVE_ENABLED:
            raise serializers.ValidationError({"mode": "Live strategy execution is disabled by the server."})
        if live_transition:
            account = get_binance_account(request.user)
            if not account or account.status != ExchangeAccount.Status.CONNECTED:
                raise serializers.ValidationError({"mode": "Connect and verify Binance before enabling LIVE."})
        if live_transition and not attrs.pop("confirm_live", False):
            raise serializers.ValidationError({"confirm_live": "Confirm real Binance strategy execution."})
        attrs.pop("confirm_live", None)
        return attrs

    def create(self, validated_data):
        strategy = validated_data["strategy"]
        run = strategy.training_runs.filter(
            status=StrategyTrainingRun.Status.COMPLETED
        ).first()
        if not run:
            raise serializers.ValidationError({"strategy": "No completed training report is available."})
        return StrategyRuntime.objects.create(
            user=self.context["request"].user,
            training_run=run,
            **validated_data,
        )
