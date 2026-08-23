from asgiref.sync import async_to_sync
from decimal import Decimal
from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .execution import execute_signal
from .models import CopyExecution, CopyStrategy, TelegramConnection, TelegramMessage
from .positions import cancel_pending_entry, close_paper_position, get_position_payload
from .serializers import (
    CopyStrategyCreateSerializer, CopyStrategySerializer, CopyStrategyUpdateSerializer,
    CopyExecutionSerializer, TelegramConnectionSerializer, TelegramMessageSerializer, TelegramStartSerializer,
    TelegramVerifySerializer,
)
from .telegram import begin_login, import_history, resolve_chat, verify_login


def connection_payload(connection):
    data = TelegramConnectionSerializer(connection).data if connection else None
    if data is not None:
        data["live_enabled"] = settings.COPY_TRADING_LIVE_ENABLED
    return data


class CopyTradingAPIView(APIView):
    permission_classes = (IsAuthenticated,)
    throttle_scope = "copy_trading"


class TelegramConnectionAPIView(CopyTradingAPIView):
    def get(self, request):
        connection = TelegramConnection.objects.filter(user=request.user).first()
        return Response(connection_payload(connection))

    def delete(self, request):
        TelegramConnection.objects.filter(user=request.user).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class TelegramStartAPIView(CopyTradingAPIView):
    throttle_scope = "telegram_auth"

    def post(self, request):
        serializer = TelegramStartSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        connection, _ = TelegramConnection.objects.update_or_create(
            user=request.user,
            defaults={"api_id": values["api_id"], "api_hash": values["api_hash"]},
        )
        try:
            async_to_sync(begin_login)(connection, values["phone"])
        except Exception as exc:
            connection.status = TelegramConnection.Status.ERROR
            connection.last_error = str(exc)[:500]
            connection.save(update_fields=("status", "last_error", "updated_at"))
            return Response({"detail": f"Telegram connection failed: {exc}"}, status=400)
        return Response({"detail": "Verification code sent.", "connection": connection_payload(connection)})


class TelegramVerifyAPIView(CopyTradingAPIView):
    throttle_scope = "telegram_auth"

    def post(self, request):
        serializer = TelegramVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        connection = get_object_or_404(TelegramConnection, user=request.user)
        try:
            async_to_sync(verify_login)(connection, **serializer.validated_data)
        except Exception as exc:
            return Response({"detail": str(exc)}, status=400)
        connection.refresh_from_db()
        return Response(connection_payload(connection))


class StrategyListCreateAPIView(CopyTradingAPIView):
    def get(self, request):
        rows = CopyStrategy.objects.filter(user=request.user).order_by("-created_at")
        return Response(CopyStrategySerializer(rows, many=True).data)

    def post(self, request):
        serializer = CopyStrategyCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        connection = get_object_or_404(
            TelegramConnection, user=request.user, status=TelegramConnection.Status.CONNECTED
        )
        values = serializer.validated_data
        try:
            chat = async_to_sync(resolve_chat)(connection, values.pop("chat_reference"))
            values.pop("confirm_live", None)
            strategy = CopyStrategy.objects.create(
                user=request.user, telegram_connection=connection, **chat, **values
            )
        except IntegrityError:
            return Response({"detail": "You already follow this Telegram chat."}, status=409)
        except Exception as exc:
            return Response({"detail": f"Cannot access that Telegram chat: {exc}"}, status=400)
        try:
            async_to_sync(import_history)(strategy, 30)
        except Exception as exc:
            strategy.last_error = f"Connected, but history import failed: {exc}"[:500]
            strategy.save(update_fields=("last_error", "updated_at"))
        return Response(CopyStrategySerializer(strategy).data, status=201)


class StrategyDetailAPIView(CopyTradingAPIView):
    def patch(self, request, strategy_id):
        strategy = get_object_or_404(CopyStrategy, id=strategy_id, user=request.user)
        serializer = CopyStrategyUpdateSerializer(strategy, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.validated_data.pop("confirm_live", None)
        serializer.save()
        return Response(CopyStrategySerializer(strategy).data)

    def delete(self, request, strategy_id):
        strategy = get_object_or_404(CopyStrategy, id=strategy_id, user=request.user)
        if strategy.executions.filter(position_status__in=("OPEN", "PENDING")).exists():
            return Response(
                {"detail": "Close open positions and cancel pending entries before deleting this stream."},
                status=409,
            )
        strategy.delete()
        return Response(status=204)


class StrategyMessagesAPIView(CopyTradingAPIView):
    def get(self, request, strategy_id):
        strategy = get_object_or_404(CopyStrategy, id=strategy_id, user=request.user)
        rows = TelegramMessage.objects.filter(strategy=strategy).select_related("signal").prefetch_related("signal__executions")
        try:
            page_size = min(max(int(request.query_params.get("page_size", 20)), 1), 50)
        except (TypeError, ValueError):
            page_size = 20
        before = request.query_params.get("before")
        if before:
            rows = rows.filter(telegram_message_id__lt=before)
        rows = list(rows[:page_size])
        return Response({
            "results": TelegramMessageSerializer(rows, many=True).data,
            "next_before": rows[-1].telegram_message_id if len(rows) == page_size else None,
        })


class StrategyNotificationsAPIView(CopyTradingAPIView):
    """Return and acknowledge messages not yet delivered to the user's notification UI."""

    def get(self, request, strategy_id):
        strategy = get_object_or_404(CopyStrategy, id=strategy_id, user=request.user)
        try:
            page_size = min(max(int(request.query_params.get("page_size", 50)), 1), 100)
        except (TypeError, ValueError):
            page_size = 50

        rows = TelegramMessage.objects.filter(strategy=strategy)
        if strategy.last_notified_message_id is None:
            latest_id = rows.order_by("-telegram_message_id").values_list(
                "telegram_message_id", flat=True
            ).first()
            if latest_id is not None:
                CopyStrategy.objects.filter(pk=strategy.pk).update(
                    last_notified_message_id=latest_id
                )
            return Response({"results": [], "has_more": False})

        rows = list(
            rows.filter(telegram_message_id__gt=strategy.last_notified_message_id)
            .select_related("signal")
            .prefetch_related("signal__executions")
            .order_by("telegram_message_id")[: page_size + 1]
        )
        has_more = len(rows) > page_size
        return Response({
            "results": TelegramMessageSerializer(rows[:page_size], many=True).data,
            "has_more": has_more,
        })

    def post(self, request, strategy_id):
        strategy = get_object_or_404(CopyStrategy, id=strategy_id, user=request.user)
        try:
            message_id = int(request.data.get("telegram_message_id"))
        except (TypeError, ValueError):
            return Response({"detail": "telegram_message_id must be an integer."}, status=400)
        if not TelegramMessage.objects.filter(
            strategy=strategy, telegram_message_id=message_id
        ).exists():
            return Response({"detail": "Telegram message not found for this stream."}, status=400)

        with transaction.atomic():
            locked = CopyStrategy.objects.select_for_update().get(pk=strategy.pk)
            if locked.last_notified_message_id is None or message_id > locked.last_notified_message_id:
                locked.last_notified_message_id = message_id
                locked.save(update_fields=("last_notified_message_id", "updated_at"))
        return Response({"last_notified_message_id": locked.last_notified_message_id})


class TelegramMessageMediaAPIView(CopyTradingAPIView):
    def get(self, request, strategy_id, message_id):
        message = get_object_or_404(
            TelegramMessage,
            strategy_id=strategy_id,
            strategy__user=request.user,
            telegram_message_id=message_id,
        )
        if not message.media_file:
            raise Http404("This Telegram message has no stored media.")
        try:
            response = FileResponse(
                message.media_file.open("rb"),
                content_type=message.media_mime_type or "application/octet-stream",
            )
        except FileNotFoundError as exc:
            raise Http404("The stored Telegram media file is unavailable.") from exc
        response["Cache-Control"] = "private, max-age=86400"
        response["X-Content-Type-Options"] = "nosniff"
        return response


class CopyPositionsAPIView(CopyTradingAPIView):
    throttle_scope = "copy_positions"

    def get(self, request):
        strategy_id = request.query_params.get("strategy_id")
        if strategy_id and not CopyStrategy.objects.filter(id=strategy_id, user=request.user).exists():
            raise Http404("Copy stream not found.")
        return Response(get_position_payload(request.user, strategy_id))


class PaperReplayAPIView(CopyTradingAPIView):
    def post(self, request, strategy_id, message_id):
        message = get_object_or_404(
            TelegramMessage.objects.select_related("strategy", "signal"),
            strategy_id=strategy_id,
            strategy__user=request.user,
            telegram_message_id=message_id,
            parse_status=TelegramMessage.ParseStatus.SIGNAL,
        )
        if message.strategy.mode != CopyStrategy.Mode.PAPER:
            return Response({"detail": "Signal replay is available only in PAPER mode."}, status=400)
        execution = execute_signal(message.strategy, message.signal, paper_replay=True)
        return Response(CopyExecutionSerializer(execution).data)


class ClosePaperPositionAPIView(CopyTradingAPIView):
    def post(self, request, execution_id):
        execution = get_object_or_404(
            CopyExecution.objects.select_related("strategy"),
            id=execution_id,
            strategy__user=request.user,
            strategy__mode=CopyStrategy.Mode.PAPER,
            position_status=CopyExecution.PositionStatus.OPEN,
        )
        payload = get_position_payload(request.user, str(execution.strategy_id))
        current = next((item for item in payload["open"] if item["id"] == str(execution.id)), None)
        if current is None:
            execution.refresh_from_db()
            return Response(CopyExecutionSerializer(execution).data)
        execution = close_paper_position(execution, Decimal(current["mark_price"]), "MANUAL")
        return Response(CopyExecutionSerializer(execution).data)


class CancelPendingEntryAPIView(CopyTradingAPIView):
    def post(self, request, execution_id):
        execution = get_object_or_404(
            CopyExecution.objects.select_related("strategy"),
            id=execution_id, strategy__user=request.user,
            position_status=CopyExecution.PositionStatus.PENDING,
        )
        execution = cancel_pending_entry(execution)
        return Response(CopyExecutionSerializer(execution).data)
