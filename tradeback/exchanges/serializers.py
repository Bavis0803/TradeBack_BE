from rest_framework import serializers

from .models import TradeLog


class ConnectExchangeSerializer(serializers.Serializer):
    api_key = serializers.CharField(max_length=255, trim_whitespace=True)
    api_secret = serializers.CharField(max_length=255, trim_whitespace=True)
    is_testnet = serializers.BooleanField(default=False)


class RiskRewardCalculationSerializer(serializers.Serializer):
    symbol = serializers.RegexField(r"^[A-Z0-9]{5,20}$", max_length=20)
    direction = serializers.ChoiceField(choices=("LONG", "SHORT"))
    entry_price = serializers.DecimalField(max_digits=30, decimal_places=12, min_value=0)
    stop_loss = serializers.DecimalField(max_digits=30, decimal_places=12, min_value=0)
    take_profit = serializers.DecimalField(max_digits=30, decimal_places=12, min_value=0)
    volume = serializers.DecimalField(max_digits=30, decimal_places=12, min_value=0)
    leverage = serializers.IntegerField(min_value=1, max_value=125)
    account_balance = serializers.DecimalField(
        max_digits=30, decimal_places=12, min_value=0, required=False
    )

    def validate_symbol(self, value):
        return value.upper()

    def validate(self, attrs):
        for field in ("entry_price", "stop_loss", "take_profit", "volume"):
            if attrs[field] <= 0:
                raise serializers.ValidationError({field: "Must be greater than zero."})
        return attrs


class TradeLogSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="client_ref", read_only=True)
    tx_id = serializers.SerializerMethodField()

    class Meta:
        model = TradeLog
        fields = (
            "id",
            "tx_id",
            "source",
            "status",
            "market",
            "symbol",
            "trade_id",
            "order_id",
            "side",
            "price",
            "quantity",
            "quote_quantity",
            "realized_pnl",
            "commission",
            "commission_asset",
            "stop_loss",
            "take_profit",
            "leverage",
            "note",
            "executed_at",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "source",
            "trade_id",
            "order_id",
            "quote_quantity",
            "commission",
            "commission_asset",
            "created_at",
            "updated_at",
        )
        extra_kwargs = {
            "status": {"required": False},
        }

    def get_tx_id(self, obj):
        if obj.source == TradeLog.Source.BINANCE:
            return f"BIN-{obj.market[:3]}-{obj.trade_id}"
        return f"DRAFT-{str(obj.client_ref)[:8].upper()}"

    def validate_symbol(self, value):
        normalized = value.upper().replace("/", "").replace("-", "").strip()
        if not normalized.isalnum() or not 5 <= len(normalized) <= 32:
            raise serializers.ValidationError("Enter a valid Binance symbol, e.g. BTCUSDT.")
        return normalized

    def validate(self, attrs):
        price = attrs.get("price", getattr(self.instance, "price", None))
        quantity = attrs.get("quantity", getattr(self.instance, "quantity", None))
        side = attrs.get("side", getattr(self.instance, "side", None))
        stop = attrs.get("stop_loss", getattr(self.instance, "stop_loss", None))
        target = attrs.get("take_profit", getattr(self.instance, "take_profit", None))
        if price is not None and price <= 0:
            raise serializers.ValidationError({"price": "Must be greater than zero."})
        if quantity is not None and quantity <= 0:
            raise serializers.ValidationError({"quantity": "Must be greater than zero."})
        if side == "BUY" and stop is not None and target is not None and not stop < price < target:
            raise serializers.ValidationError(
                "BUY draft requires stop loss below entry and take profit above entry."
            )
        if side == "SELL" and stop is not None and target is not None and not target < price < stop:
            raise serializers.ValidationError(
                "SELL draft requires take profit below entry and stop loss above entry."
            )
        return attrs

    def create(self, validated_data):
        validated_data["quote_quantity"] = (
            validated_data["price"] * validated_data["quantity"]
        )
        return super().create(validated_data)

    def update(self, instance, validated_data):
        result = super().update(instance, validated_data)
        result.quote_quantity = result.price * result.quantity
        result.save(update_fields=("quote_quantity", "updated_at"))
        return result
