from django.urls import path

from .views import (
    ExchangeAPIView,
    ExchangeDisconnectAPIView,
    ExchangeStatusAPIView,
    ExchangeVerifyAPIView,
    DashboardAPIView,
    RiskRewardCalculateAPIView,
    RiskRewardContextAPIView,
)
from .transaction_views import (
    TradeLogDetailAPIView,
    TradeLogListCreateAPIView,
    TradeLogSyncAPIView,
)


urlpatterns = [
    path("transactions/", TradeLogListCreateAPIView.as_view()),
    path("transactions/sync/", TradeLogSyncAPIView.as_view()),
    path("transactions/<uuid:trade_id>/", TradeLogDetailAPIView.as_view()),
    path("dashboard/", DashboardAPIView.as_view()),
    path("check/", ExchangeAPIView.as_view()),
    path("status/", ExchangeStatusAPIView.as_view()),
    path("verify/", ExchangeVerifyAPIView.as_view()),
    path("disconnect/", ExchangeDisconnectAPIView.as_view()),
    path("risk-reward/context/", RiskRewardContextAPIView.as_view()),
    path("risk-reward/calculate/", RiskRewardCalculateAPIView.as_view()),
]
