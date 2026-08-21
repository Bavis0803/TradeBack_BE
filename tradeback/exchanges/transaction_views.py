from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.generics import RetrieveUpdateDestroyAPIView
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .connection import get_binance_account
from .models import ExchangeAccount, TradeLog
from .serializers import TradeLogSerializer
from .services import BinanceServiceError, decimal_to_string
from .transactions import sync_real_trades


class TradeLogPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 100


def filtered_trade_logs(request):
    queryset = TradeLog.objects.filter(user=request.user).select_related(None)
    source = request.query_params.get("source", "ALL").upper()
    market = request.query_params.get("market", "ALL").upper()
    side = request.query_params.get("side", "ALL").upper()
    status_value = request.query_params.get("status", "ALL").upper()
    if source in TradeLog.Source.values:
        queryset = queryset.filter(source=source)
    if market in TradeLog.Market.values:
        queryset = queryset.filter(market=market)
    if side in ("BUY", "SELL"):
        queryset = queryset.filter(side=side)
    if status_value in TradeLog.Status.values:
        queryset = queryset.filter(status=status_value)
    query = request.query_params.get("q", "").strip().upper()
    if query:
        normalized = query.replace("/", "").replace("-", "")
        query_filter = Q(symbol__istartswith=normalized)
        if query.isdigit():
            query_filter |= Q(trade_id=int(query)) | Q(order_id=int(query))
        queryset = queryset.filter(query_filter)
    date_from = parse_date(request.query_params.get("date_from", ""))
    date_to = parse_date(request.query_params.get("date_to", ""))
    if date_from:
        queryset = queryset.filter(
            executed_at__gte=timezone.make_aware(datetime.combine(date_from, time.min))
        )
    if date_to:
        queryset = queryset.filter(
            executed_at__lt=timezone.make_aware(
                datetime.combine(date_to + timedelta(days=1), time.min)
            )
        )
    return queryset.order_by("-executed_at", "-id")


def trade_stats(queryset):
    aggregate = queryset.aggregate(
        total=Count("id"),
        gross_pnl=Sum("realized_pnl"),
        total_volume=Sum("quote_quantity"),
        wins=Count("id", filter=Q(realized_pnl__gt=0)),
        outcomes=Count("id", filter=~Q(realized_pnl=0)),
    )
    outcomes = aggregate["outcomes"] or 0
    wins = aggregate["wins"] or 0
    win_rate = Decimal(wins * 100) / Decimal(outcomes) if outcomes else Decimal("0")
    return {
        "total": aggregate["total"] or 0,
        "gross_pnl": decimal_to_string(aggregate["gross_pnl"] or Decimal("0")),
        "total_volume": decimal_to_string(aggregate["total_volume"] or Decimal("0")),
        "wins": wins,
        "outcomes": outcomes,
        "win_rate": decimal_to_string(win_rate),
    }


class TradeLogListCreateAPIView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_scope = "transaction_log"

    def get(self, request):
        queryset = filtered_trade_logs(request)
        stats = trade_stats(queryset)
        paginator = TradeLogPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        response = paginator.get_paginated_response(
            TradeLogSerializer(page, many=True).data
        )
        response.data["stats"] = stats
        return response

    def post(self, request):
        serializer = TradeLogSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        account = get_binance_account(request.user)
        serializer.save(
            user=request.user,
            account=account,
            source=TradeLog.Source.DRAFT,
            status=serializer.validated_data.get("status", TradeLog.Status.DRAFT),
        )
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class TradeLogDetailAPIView(RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = TradeLogSerializer
    lookup_field = "client_ref"
    lookup_url_kwarg = "trade_id"

    def get_queryset(self):
        return TradeLog.objects.filter(
            user=self.request.user,
            source=TradeLog.Source.DRAFT,
        )


class TradeLogSyncAPIView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_scope = "transaction_sync"

    def post(self, request):
        account = get_binance_account(request.user)
        if not account or account.status != ExchangeAccount.Status.CONNECTED:
            return Response(
                {"detail": "Connect and verify Binance before syncing real trades."},
                status=status.HTTP_409_CONFLICT,
            )
        try:
            result = sync_real_trades(account)
            return Response({"success": True, **result})
        except BinanceServiceError as error:
            return Response({"detail": str(error)}, status=status.HTTP_502_BAD_GATEWAY)
