from decimal import Decimal

from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .connection import (
    build_binance_service,
    connect_binance_account,
    get_binance_account,
    serialize_exchange_account,
    touch_exchange_sync,
    verify_binance_account,
)
from .dashboard import build_dashboard_payload
from .models import ExchangeAccount
from .serializers import ConnectExchangeSerializer, RiskRewardCalculationSerializer
from .services import (
    BinanceService,
    BinanceAuthenticationError,
    BinanceServiceError,
    calculate_risk_reward,
    decimal_to_string,
)


def get_exchange_account(request):
    if not request.user or not request.user.is_authenticated:
        return None
    return get_binance_account(request.user)


def get_services(request):
    account = get_exchange_account(request)
    public_service = BinanceService()
    account_service = None
    if account and account.status == ExchangeAccount.Status.CONNECTED:
        account_service = build_binance_service(account)
        public_service = account_service
    return account, public_service, account_service


def demo_brackets():
    return [{
        "initial_leverage": BinanceService.DEMO_MAX_LEVERAGE,
        "notional_floor": Decimal("0"),
        "notional_cap": Decimal("1E50"),
        "maint_margin_ratio": Decimal("0.004"),
    }]


def serialize_context(context, balance, brackets, mode):
    return {
        **{
            key: decimal_to_string(value) if isinstance(value, Decimal) else value
            for key, value in context.items()
        },
        "account": {
            "mode": mode,
            "balance": decimal_to_string(balance) if balance is not None else None,
            "balance_asset": "USDT",
            "balance_editable": mode == "demo",
        },
        "leverage": {
            "min": 1,
            "max": max(item["initial_leverage"] for item in brackets),
            "source": "account_brackets" if mode == "connected" else "public_fallback",
        },
    }


class ExchangeAPIView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_scope = "binance_connection"

    def post(self, request):
        serializer = ConnectExchangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        account, result = connect_binance_account(
            request.user,
            serializer.validated_data["api_key"],
            serializer.validated_data["api_secret"],
            serializer.validated_data["is_testnet"],
        )
        if not result["success"]:
            return Response(result, status=status.HTTP_400_BAD_REQUEST)
        return Response({**result, "account": serialize_exchange_account(account)})


class ExchangeStatusAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        account = get_binance_account(request.user)
        if not account:
            return Response({"connected": False, "account": None})
        return Response({
            "connected": account.status == ExchangeAccount.Status.CONNECTED,
            "account": serialize_exchange_account(account),
        })


class ExchangeVerifyAPIView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_scope = "binance_connection"

    def post(self, request):
        account = get_binance_account(request.user)
        if not account:
            return Response(
                {"detail": "No Binance account is connected."},
                status=status.HTTP_404_NOT_FOUND,
            )
        result = verify_binance_account(account)
        response_status = status.HTTP_200_OK if result["success"] else status.HTTP_502_BAD_GATEWAY
        return Response(
            {**result, "account": serialize_exchange_account(account)},
            status=response_status,
        )


class ExchangeDisconnectAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request):
        account = get_binance_account(request.user)
        if account:
            account.delete()
        return Response({"success": True, "message": "Binance account disconnected."})


class DashboardAPIView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_scope = "binance_dashboard"

    def get(self, request):
        account = get_binance_account(request.user)
        if not account:
            return Response(
                {"detail": "Connect a Binance account before loading the dashboard."},
                status=status.HTTP_409_CONFLICT,
            )
        if account.status != ExchangeAccount.Status.CONNECTED:
            return Response(
                {
                    "detail": "The Binance connection needs verification.",
                    "connection_status": account.status,
                },
                status=status.HTTP_409_CONFLICT,
            )
        force_refresh = request.query_params.get("refresh", "false").lower() == "true"
        try:
            return Response(build_dashboard_payload(account, force_refresh=force_refresh))
        except BinanceAuthenticationError as error:
            account.status = ExchangeAccount.Status.ERROR
            account.last_error = str(error)[:500]
            account.save(update_fields=("status", "last_error", "updated_at"))
            return Response({"detail": str(error)}, status=status.HTTP_401_UNAUTHORIZED)
        except BinanceServiceError as error:
            return Response({"detail": str(error)}, status=status.HTTP_502_BAD_GATEWAY)


class RiskRewardContextAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        symbol = request.query_params.get("symbol", "BTCUSDT").upper()
        include_symbols = request.query_params.get("include_symbols", "true").lower() == "true"
        try:
            account, public_service, account_service = get_services(request)
            context = public_service.get_symbol_context(symbol, include_symbols=include_symbols)
            if account_service:
                balance = account_service.get_usdt_balance()
                brackets = account_service.get_leverage_brackets(symbol)
                touch_exchange_sync(account)
                mode = "connected"
            else:
                balance = None
                brackets = demo_brackets()
                mode = "demo"
            return Response(serialize_context(context, balance, brackets, mode))
        except BinanceServiceError as error:
            return Response({"detail": str(error)}, status=status.HTTP_502_BAD_GATEWAY)


class RiskRewardCalculateAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = RiskRewardCalculationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            account, public_service, account_service = get_services(request)
            context = public_service.get_symbol_context(data["symbol"], include_symbols=False)
            if account_service:
                balance = account_service.get_usdt_balance()
                brackets = account_service.get_leverage_brackets(data["symbol"])
                touch_exchange_sync(account)
                mode = "connected"
            else:
                if "account_balance" not in data:
                    return Response(
                        {"account_balance": ["This field is required in demo mode."]},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                balance = data["account_balance"]
                brackets = demo_brackets()
                mode = "demo"
            result = calculate_risk_reward(data, balance, context, brackets)
            return Response({
                "symbol": data["symbol"],
                "direction": data["direction"],
                "account_mode": mode,
                "account_balance": decimal_to_string(balance),
                "calculation": result,
            })
        except ValueError as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        except BinanceServiceError as error:
            return Response({"detail": str(error)}, status=status.HTTP_502_BAD_GATEWAY)
