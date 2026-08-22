from decimal import Decimal
import re

from django.conf import settings
from rest_framework import serializers

from .models import CopyExecution, CopyStrategy, TelegramConnection, TelegramMessage, TradeSignal


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
            "take_profits", "requested_leverage",
        )


class CopyExecutionSerializer(serializers.ModelSerializer):
    class Meta:
        model = CopyExecution
        fields = (
            "id", "status", "symbol", "direction", "entry_price", "quantity", "leverage",
            "margin_usdt", "stop_loss", "take_profit", "error", "created_at",
            "position_status", "exit_price", "realized_pnl", "close_reason", "closed_at",
        )


class TelegramMessageSerializer(serializers.ModelSerializer):
    signal = TradeSignalSerializer(read_only=True)
    execution = serializers.SerializerMethodField()
    media_url = serializers.SerializerMethodField()

    class Meta:
        model = TelegramMessage
        fields = (
            "telegram_message_id", "sender_name", "text", "sent_at", "parse_status",
            "signal", "execution", "media_type", "media_mime_type", "media_size", "media_url",
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


class CopyStrategySerializer(serializers.ModelSerializer):
    class Meta:
        model = CopyStrategy
        fields = (
            "id", "chat_id", "chat_title", "chat_username", "mode", "status",
            "allocation_usdt", "max_leverage", "max_daily_loss_usdt", "allowed_symbols",
            "use_binance_max_leverage", "entry_tolerance_percent",
            "last_message_id", "last_error", "created_at", "updated_at",
        )
        read_only_fields = ("id", "chat_id", "chat_title", "chat_username", "last_message_id", "last_error")


class CopyStrategyCreateSerializer(serializers.Serializer):
    chat_reference = serializers.CharField(min_length=2, max_length=255)
    allocation_usdt = serializers.DecimalField(max_digits=20, decimal_places=8, min_value=1)
    max_leverage = serializers.IntegerField(min_value=1, max_value=125, default=10)
    use_binance_max_leverage = serializers.BooleanField(default=True)
    max_daily_loss_usdt = serializers.DecimalField(max_digits=20, decimal_places=8, min_value=1, default=50)
    entry_tolerance_percent = serializers.DecimalField(
        max_digits=5, decimal_places=3, min_value=0, max_value=2, default=Decimal("0.300")
    )
    allowed_symbols = serializers.ListField(
        child=serializers.RegexField(r"^[A-Z0-9]{2,20}USDT$"), required=False, default=list
    )
    mode = serializers.ChoiceField(choices=CopyStrategy.Mode.choices, default=CopyStrategy.Mode.PAPER)
    confirm_live = serializers.BooleanField(default=False, write_only=True)

    def validate(self, attrs):
        if attrs["allocation_usdt"] > Decimal(str(settings.COPY_TRADING_MAX_ALLOCATION_USDT)):
            raise serializers.ValidationError({"allocation_usdt": "Exceeds the server safety limit."})
        if attrs["mode"] == CopyStrategy.Mode.LIVE:
            if not attrs["confirm_live"]:
                raise serializers.ValidationError({"confirm_live": "Explicit confirmation is required for LIVE mode."})
            if not settings.COPY_TRADING_LIVE_ENABLED:
                raise serializers.ValidationError({"mode": "Live copy trading is disabled by the server."})
        return attrs


class CopyStrategyUpdateSerializer(serializers.ModelSerializer):
    confirm_live = serializers.BooleanField(default=False, write_only=True)

    class Meta:
        model = CopyStrategy
        fields = (
            "mode", "status", "allocation_usdt", "max_leverage", "max_daily_loss_usdt",
            "allowed_symbols", "use_binance_max_leverage", "entry_tolerance_percent",
            "confirm_live",
        )

    def validate(self, attrs):
        allocation = attrs.get("allocation_usdt", self.instance.allocation_usdt)
        if allocation > Decimal(str(settings.COPY_TRADING_MAX_ALLOCATION_USDT)):
            raise serializers.ValidationError({"allocation_usdt": "Exceeds the server safety limit."})
        mode = attrs.get("mode", self.instance.mode)
        if (
            mode != self.instance.mode
            and self.instance.executions.filter(
                position_status=CopyExecution.PositionStatus.OPEN
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
        return attrs
