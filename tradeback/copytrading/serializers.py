from decimal import Decimal
import re

from django.conf import settings
from django.db.models import Count, Sum
from django.utils import timezone
from rest_framework import serializers

from .models import (
    AISignalAgent, CopyExecution, CopyStrategy, SignalCandidate, TelegramConnection,
    TelegramMessage, TradeSignal,
)


class AISignalAgentSerializer(serializers.ModelSerializer):
    usage_today = serializers.SerializerMethodField()

    def get_usage_today(self, obj):
        usage = obj.user.ai_signal_analyses.filter(
            created_at__date=timezone.localdate()
        ).aggregate(calls=Count("id"), input_tokens=Sum("input_tokens"), output_tokens=Sum("output_tokens"))
        return {
            "calls": usage["calls"] or 0,
            "input_tokens": usage["input_tokens"] or 0,
            "output_tokens": usage["output_tokens"] or 0,
        }

    class Meta:
        model = AISignalAgent
        fields = (
            "provider", "api_key_hint", "model", "enabled", "min_confidence",
            "daily_call_limit", "status", "last_error", "last_verified_at", "updated_at",
            "usage_today",
        )
        read_only_fields = (
            "api_key_hint", "status", "last_error", "last_verified_at", "updated_at",
            "usage_today",
        )


class AISignalAgentConnectSerializer(serializers.Serializer):
    api_key = serializers.CharField(min_length=20, max_length=512, write_only=True)
    model = serializers.RegexField(r"^[A-Za-z0-9._:-]{2,64}$", default="gpt-5-nano")
    min_confidence = serializers.DecimalField(
        max_digits=4, decimal_places=3, min_value=Decimal("0.500"),
        max_value=Decimal("1.000"), default=Decimal("0.900"),
    )
    daily_call_limit = serializers.IntegerField(min_value=1, max_value=1000, default=50)


class TelegramConnectionSerializer(serializers.ModelSerializer):
    class Meta:
        model = TelegramConnection
        fields = ("status", "phone_hint", "last_error", "last_connected_at", "updated_at")


class TelegramStartSerializer(serializers.Serializer):
    api_id = serializers.IntegerField(min_value=1)
    api_hash = serializers.RegexField(
        r"^[0-9a-fA-F]{32}$",
        trim_whitespace=True,
        write_only=True,
        error_messages={
            "invalid": "API Hash must contain exactly 32 hexadecimal characters without a label or colon."
        },
    )
    phone = serializers.RegexField(
        r"^\+[1-9]\d{6,14}$",
        trim_whitespace=True,
        write_only=True,
        error_messages={"invalid": "Use international phone format, for example +84901234567."},
    )


class TelegramVerifySerializer(serializers.Serializer):
    code = serializers.CharField(min_length=3, max_length=128, write_only=True)
    password = serializers.CharField(required=False, allow_blank=True, max_length=256, write_only=True)

    def validate_code(self, value):
        # Telegram users often paste the complete service message instead of
        # only its login code. Accept one unambiguous 5-6 digit code token.
        matches = re.findall(r"(?<!\d)\d{5,6}(?!\d)", value)
        if len(matches) != 1:
            raise serializers.ValidationError(
                "Enter the latest 5-6 digit login code sent by Telegram."
            )
        return matches[0]


class TradeSignalSerializer(serializers.ModelSerializer):
    class Meta:
        model = TradeSignal
        fields = (
            "symbol", "direction", "entry_low", "entry_high", "stop_loss",
            "take_profits", "requested_leverage", "parser_version",
        )


class CopyExecutionSerializer(serializers.ModelSerializer):
    class Meta:
        model = CopyExecution
        fields = (
            "id", "status", "symbol", "direction", "entry_price", "quantity", "leverage",
            "margin_usdt", "stop_loss", "take_profit", "error", "created_at",
            "position_status", "exit_price", "realized_pnl", "close_reason", "closed_at",
            "entry_order_type", "limit_price", "entry_expires_at",
            "take_profit_quantity", "remaining_quantity", "tp1_close_percent",
            "runner_take_profit", "break_even_activated_at",
        )


class SignalCandidateSerializer(serializers.ModelSerializer):
    class Meta:
        model = SignalCandidate
        fields = (
            "symbol", "direction", "target_hint", "reason", "status",
            "reviewed_at", "created_at",
        )


class SignalCandidateReviewSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=("APPROVE", "REJECT"))
    entry_price = serializers.DecimalField(
        max_digits=32, decimal_places=12, min_value=Decimal("0.000000000001"), required=False
    )
    stop_loss = serializers.DecimalField(
        max_digits=32, decimal_places=12, min_value=Decimal("0.000000000001"), required=False
    )
    take_profit = serializers.DecimalField(
        max_digits=32, decimal_places=12, min_value=Decimal("0.000000000001"), required=False
    )
    leverage = serializers.IntegerField(min_value=1, max_value=125, required=False)
    confirm_live = serializers.BooleanField(default=False, write_only=True)

    def validate(self, attrs):
        if attrs["action"] == "REJECT":
            return attrs
        missing = [
            field for field in ("entry_price", "stop_loss", "take_profit")
            if field not in attrs
        ]
        if missing:
            raise serializers.ValidationError({field: "This field is required." for field in missing})
        candidate = self.context["candidate"]
        if (
            candidate.message.strategy.mode == CopyStrategy.Mode.LIVE
            and not attrs.get("confirm_live")
        ):
            raise serializers.ValidationError({
                "confirm_live": "Confirm that this approval may place a real Binance order."
            })
        entry = attrs["entry_price"]
        stop = attrs["stop_loss"]
        target = attrs["take_profit"]
        if candidate.direction == TradeSignal.Direction.LONG and not stop < entry < target:
            raise serializers.ValidationError(
                "LONG review requires stop loss below entry and take profit above entry."
            )
        if candidate.direction == TradeSignal.Direction.SHORT and not target < entry < stop:
            raise serializers.ValidationError(
                "SHORT review requires take profit below entry and stop loss above entry."
            )
        return attrs


class TelegramMessageSerializer(serializers.ModelSerializer):
    signal = TradeSignalSerializer(read_only=True)
    candidate = serializers.SerializerMethodField()
    execution = serializers.SerializerMethodField()
    media_url = serializers.SerializerMethodField()

    class Meta:
        model = TelegramMessage
        fields = (
            "telegram_message_id", "sender_name", "text", "sent_at", "parse_status",
            "signal", "candidate", "execution", "media_type", "media_mime_type", "media_size", "media_url",
        )

    def get_media_url(self, obj):
        if not obj.media_file:
            return None
        return f"/copy-trading/strategies/{obj.strategy_id}/messages/{obj.telegram_message_id}/media/"

    def get_execution(self, obj):
        if not hasattr(obj, "signal"):
            return None
        execution = next(
            (item for item in obj.signal.executions.all() if item.strategy_id == obj.strategy_id),
            None,
        )
        return CopyExecutionSerializer(execution).data if execution else None

    def get_candidate(self, obj):
        candidate = getattr(obj, "signal_candidate", None)
        return SignalCandidateSerializer(candidate).data if candidate else None


class CopyStrategySerializer(serializers.ModelSerializer):
    class Meta:
        model = CopyStrategy
        fields = (
            "id", "chat_id", "chat_title", "chat_username", "mode", "status",
            "allocation_usdt", "risk_percent_per_order", "minimum_risk_reward",
            "tp1_close_percent",
            "max_leverage", "max_daily_loss_usdt", "allowed_symbols",
            "use_binance_max_leverage", "entry_tolerance_percent",
            "entry_order_type", "limit_expiry_minutes",
            "ai_detection_enabled",
            "last_message_id", "last_error", "created_at", "updated_at",
        )
        read_only_fields = ("id", "chat_id", "chat_title", "chat_username", "last_message_id", "last_error")


class CopyStrategyCreateSerializer(serializers.Serializer):
    chat_reference = serializers.CharField(min_length=2, max_length=255)
    allocation_usdt = serializers.DecimalField(max_digits=20, decimal_places=8, min_value=1)
    risk_percent_per_order = serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=Decimal("0.10"),
        max_value=Decimal("100.00"), default=Decimal("30.00"),
    )
    minimum_risk_reward = serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=Decimal("0.10"),
        max_value=Decimal("20.00"), default=Decimal("1.50"),
    )
    tp1_close_percent = serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=Decimal("1.00"),
        max_value=Decimal("100.00"), default=Decimal("70.00"),
    )
    max_leverage = serializers.IntegerField(min_value=1, max_value=125, default=10)
    use_binance_max_leverage = serializers.BooleanField(default=True)
    max_daily_loss_usdt = serializers.DecimalField(max_digits=20, decimal_places=8, min_value=1, default=50)
    entry_tolerance_percent = serializers.DecimalField(
        max_digits=5, decimal_places=3, min_value=0, max_value=2, default=Decimal("0.300")
    )
    entry_order_type = serializers.ChoiceField(
        choices=CopyStrategy.EntryOrderType.choices, default=CopyStrategy.EntryOrderType.SMART
    )
    limit_expiry_minutes = serializers.IntegerField(min_value=11, max_value=120, default=15)
    ai_detection_enabled = serializers.BooleanField(default=False)
    allowed_symbols = serializers.ListField(
        child=serializers.RegexField(r"^[A-Z0-9]{2,20}USDT$"), required=False, default=list
    )
    mode = serializers.ChoiceField(choices=CopyStrategy.Mode.choices, default=CopyStrategy.Mode.PAPER)
    confirm_live = serializers.BooleanField(default=False, write_only=True)
    confirm_ai_live = serializers.BooleanField(default=False, write_only=True)

    def validate(self, attrs):
        if attrs["allocation_usdt"] > Decimal(str(settings.COPY_TRADING_MAX_ALLOCATION_USDT)):
            raise serializers.ValidationError({"allocation_usdt": "Exceeds the server safety limit."})
        if attrs["mode"] == CopyStrategy.Mode.LIVE:
            if not attrs["confirm_live"]:
                raise serializers.ValidationError({"confirm_live": "Explicit confirmation is required for LIVE mode."})
            if not settings.COPY_TRADING_LIVE_ENABLED:
                raise serializers.ValidationError({"mode": "Live copy trading is disabled by the server."})
            if attrs.get("ai_detection_enabled") and not attrs.get("confirm_ai_live"):
                raise serializers.ValidationError({
                    "confirm_ai_live": "Explicit confirmation is required for AI-detected LIVE orders."
                })
        return attrs


class CopyStrategyUpdateSerializer(serializers.ModelSerializer):
    confirm_live = serializers.BooleanField(default=False, write_only=True)
    confirm_ai_live = serializers.BooleanField(default=False, write_only=True)

    class Meta:
        model = CopyStrategy
        fields = (
            "mode", "status", "allocation_usdt", "risk_percent_per_order",
            "minimum_risk_reward", "tp1_close_percent", "max_leverage", "max_daily_loss_usdt",
            "allowed_symbols", "use_binance_max_leverage", "entry_tolerance_percent",
            "entry_order_type", "limit_expiry_minutes",
            "ai_detection_enabled",
            "confirm_live",
            "confirm_ai_live",
        )

    def validate(self, attrs):
        allocation = attrs.get("allocation_usdt", self.instance.allocation_usdt)
        if allocation > Decimal(str(settings.COPY_TRADING_MAX_ALLOCATION_USDT)):
            raise serializers.ValidationError({"allocation_usdt": "Exceeds the server safety limit."})
        mode = attrs.get("mode", self.instance.mode)
        if (
            mode != self.instance.mode
            and self.instance.executions.filter(
                position_status__in=(
                    CopyExecution.PositionStatus.OPEN, CopyExecution.PositionStatus.PENDING,
                )
            ).exists()
        ):
            raise serializers.ValidationError({
                "mode": "Close all running positions before changing the execution mode."
            })
        if mode == CopyStrategy.Mode.LIVE and self.instance.mode != CopyStrategy.Mode.LIVE:
            if not attrs.get("confirm_live"):
                raise serializers.ValidationError({"confirm_live": "Explicit confirmation is required for LIVE mode."})
            if not settings.COPY_TRADING_LIVE_ENABLED:
                raise serializers.ValidationError({"mode": "Live copy trading is disabled by the server."})
        ai_enabled = attrs.get("ai_detection_enabled", self.instance.ai_detection_enabled)
        if (
            mode == CopyStrategy.Mode.LIVE and ai_enabled
            and not self.instance.ai_detection_enabled
            and not attrs.get("confirm_ai_live")
        ):
            raise serializers.ValidationError({
                "confirm_ai_live": "Explicit confirmation is required for AI-detected LIVE orders."
            })
        return attrs
