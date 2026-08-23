from django.urls import path

from .views import (
    AISignalAgentAPIView, CancelPendingEntryAPIView, ClosePaperPositionAPIView,
    CopyPositionsAPIView, PaperReplayAPIView,
    StrategyDetailAPIView, StrategyListCreateAPIView, StrategyMessagesAPIView,
    StrategyNotificationsAPIView,
    SignalCandidateReviewAPIView,
    TelegramConnectionAPIView, TelegramMessageMediaAPIView, TelegramStartAPIView,
    TelegramVerifyAPIView,
)

urlpatterns = [
    path("ai-agent/", AISignalAgentAPIView.as_view()),
    path("telegram/", TelegramConnectionAPIView.as_view()),
    path("telegram/start/", TelegramStartAPIView.as_view()),
    path("telegram/verify/", TelegramVerifyAPIView.as_view()),
    path("strategies/", StrategyListCreateAPIView.as_view()),
    path("positions/", CopyPositionsAPIView.as_view()),
    path("positions/<uuid:execution_id>/close/", ClosePaperPositionAPIView.as_view()),
    path("positions/<uuid:execution_id>/cancel-entry/", CancelPendingEntryAPIView.as_view()),
    path("strategies/<uuid:strategy_id>/", StrategyDetailAPIView.as_view()),
    path("strategies/<uuid:strategy_id>/messages/", StrategyMessagesAPIView.as_view()),
    path(
        "strategies/<uuid:strategy_id>/notifications/",
        StrategyNotificationsAPIView.as_view(),
    ),
    path(
        "strategies/<uuid:strategy_id>/messages/<int:message_id>/review/",
        SignalCandidateReviewAPIView.as_view(),
    ),
    path(
        "strategies/<uuid:strategy_id>/messages/<int:message_id>/media/",
        TelegramMessageMediaAPIView.as_view(),
    ),
    path(
        "strategies/<uuid:strategy_id>/messages/<int:message_id>/paper-replay/",
        PaperReplayAPIView.as_view(),
    ),
]
