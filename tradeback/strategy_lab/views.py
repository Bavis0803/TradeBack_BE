from decimal import Decimal

from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from exchanges.services import BinanceService, BinanceServiceError

from .execution import _close_paper
from .models import (
    StrategyDefinition, StrategyPosition, StrategyRuntime, StrategyTrainingRun,
)
from .serializers import (
    StrategyDefinitionSerializer, StrategyPositionSerializer, StrategyRuntimeSerializer,
    TrainingRunSerializer,
)
from .training import SUPPORTED_TIMEFRAMES, queue_training
from .serializers import MAX_STRATEGY_SYMBOLS


class StrategyAPIView(APIView):
    permission_classes = (IsAuthenticated,)
    throttle_scope = "strategy_lab"


class StrategyCatalogAPIView(StrategyAPIView):
    def get(self, request):
        try:
            symbols = BinanceService().get_top_futures_symbols(20)
        except BinanceServiceError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response({
            "default_symbols": symbols,
            "max_symbols": MAX_STRATEGY_SYMBOLS,
            "timeframes": list(SUPPORTED_TIMEFRAMES),
            "supported_indicators": ["ta.sma", "ta.ema", "ta.rsi", "ta.atr"],
            "supported_triggers": ["ta.crossover", "ta.crossunder", "and", "or", "not"],
            "template": (
                "//@version=6\nindicator(\"EMA + RSI\", overlay=true)\n"
                "fast = ta.ema(close, 9)\nslow = ta.ema(close, 21)\n"
                "rsi = ta.rsi(close, 14)"
            ),
            "default_long_condition": "ta.crossover(fast, slow) and rsi < 70",
            "default_short_condition": "ta.crossunder(fast, slow) and rsi > 30",
        })


class StrategySymbolValidationAPIView(StrategyAPIView):
    def get(self, request):
        symbol = request.query_params.get("symbol", "").upper().replace("/", "").strip()
        if symbol and not symbol.endswith("USDT"):
            symbol = f"{symbol}USDT"
        if not symbol or not symbol.isalnum():
            return Response({"valid": False, "symbol": symbol, "detail": "Enter a valid coin symbol."})
        try:
            valid = symbol in BinanceService().get_active_futures_symbols()
        except BinanceServiceError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response({
            "valid": valid, "symbol": symbol,
            "detail": "Active Binance USDT perpetual." if valid else "This coin is not an active Binance USDT perpetual.",
        })


class StrategyDefinitionListCreateAPIView(StrategyAPIView):
    def get(self, request):
        rows = StrategyDefinition.objects.filter(user=request.user).prefetch_related("training_runs")
        return Response(StrategyDefinitionSerializer(rows, many=True).data)

    def post(self, request):
        payload = request.data.copy()
        if not payload.get("symbols"):
            try:
                payload["symbols"] = BinanceService().get_top_futures_symbols(20)
            except BinanceServiceError as exc:
                return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        if not payload.get("timeframes"):
            payload["timeframes"] = ["15m", "1h", "4h"]
        serializer = StrategyDefinitionSerializer(data=payload)
        serializer.is_valid(raise_exception=True)
        strategy = serializer.save(user=request.user)
        return Response(
            StrategyDefinitionSerializer(strategy).data,
            status=status.HTTP_201_CREATED,
        )


class StrategyDefinitionDetailAPIView(StrategyAPIView):
    def get_object(self, request, strategy_id):
        return get_object_or_404(
            StrategyDefinition.objects.prefetch_related("training_runs"),
            id=strategy_id,
            user=request.user,
        )

    def get(self, request, strategy_id):
        return Response(StrategyDefinitionSerializer(self.get_object(request, strategy_id)).data)

    def patch(self, request, strategy_id):
        strategy = self.get_object(request, strategy_id)
        if strategy.training_runs.filter(
            status__in=(StrategyTrainingRun.Status.QUEUED, StrategyTrainingRun.Status.RUNNING)
        ).exists():
            return Response(
                {"detail": "Wait for the active training run before editing this strategy."},
                status=status.HTTP_409_CONFLICT,
            )
        if strategy.runtimes.filter(status=StrategyRuntime.Status.ACTIVE).exists():
            return Response(
                {"detail": "Pause active executions before editing and retraining this strategy."},
                status=status.HTTP_409_CONFLICT,
            )
        serializer = StrategyDefinitionSerializer(strategy, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        return Response(StrategyDefinitionSerializer(serializer.save()).data)

    def delete(self, request, strategy_id):
        strategy = self.get_object(request, strategy_id)
        if strategy.runtimes.filter(Q(status=StrategyRuntime.Status.ACTIVE) | Q(positions__status="OPEN")).exists():
            return Response(
                {"detail": "Pause execution and close open positions before deleting."},
                status=status.HTTP_409_CONFLICT,
            )
        strategy.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class StrategyTrainAPIView(StrategyAPIView):
    throttle_scope = "strategy_training"

    def post(self, request, strategy_id):
        strategy = get_object_or_404(StrategyDefinition, id=strategy_id, user=request.user)
        try:
            run = queue_training(strategy)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response(TrainingRunSerializer(run).data, status=status.HTTP_202_ACCEPTED)


class TrainingRunDetailAPIView(StrategyAPIView):
    def get(self, request, run_id):
        run = get_object_or_404(
            StrategyTrainingRun.objects.select_related("strategy").prefetch_related("results"),
            id=run_id,
            strategy__user=request.user,
        )
        return Response(TrainingRunSerializer(run).data)


class StrategyRuntimeListCreateAPIView(StrategyAPIView):
    def get(self, request):
        rows = StrategyRuntime.objects.filter(user=request.user).select_related(
            "strategy", "training_run"
        ).prefetch_related("positions")
        return Response(StrategyRuntimeSerializer(rows, many=True).data)

    def post(self, request):
        serializer = StrategyRuntimeSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        runtime = serializer.save()
        return Response(
            StrategyRuntimeSerializer(runtime, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class StrategyRuntimeDetailAPIView(StrategyAPIView):
    def patch(self, request, runtime_id):
        runtime = get_object_or_404(StrategyRuntime, id=runtime_id, user=request.user)
        serializer = StrategyRuntimeSerializer(
            runtime, data=request.data, partial=True, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        return Response(StrategyRuntimeSerializer(serializer.save(), context={"request": request}).data)

    def delete(self, request, runtime_id):
        runtime = get_object_or_404(StrategyRuntime, id=runtime_id, user=request.user)
        if runtime.positions.filter(status=StrategyPosition.Status.OPEN).exists():
            return Response(
                {"detail": "Close all open positions before deleting this execution."},
                status=status.HTTP_409_CONFLICT,
            )
        runtime.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class StrategyPositionListAPIView(StrategyAPIView):
    throttle_scope = "strategy_positions"

    def get(self, request):
        runtime_id = request.query_params.get("runtime_id")
        rows = StrategyPosition.objects.filter(runtime__user=request.user).select_related(
            "runtime__strategy"
        )
        if runtime_id:
            rows = rows.filter(runtime_id=runtime_id)
        return Response(StrategyPositionSerializer(rows[:200], many=True).data)


class ClosePaperStrategyPositionAPIView(StrategyAPIView):
    def post(self, request, position_id):
        position = get_object_or_404(
            StrategyPosition.objects.select_related("runtime__strategy"),
            id=position_id,
            runtime__user=request.user,
            runtime__mode=StrategyRuntime.Mode.PAPER,
            status=StrategyPosition.Status.OPEN,
        )
        price = BinanceService().get_futures_mark_prices([position.symbol]).get(position.symbol)
        if price is None:
            return Response({"detail": "Current Binance mark price is unavailable."}, status=502)
        position = _close_paper(position, Decimal(price), "MANUAL")
        return Response(StrategyPositionSerializer(position).data)
