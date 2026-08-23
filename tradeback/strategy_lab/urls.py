from django.urls import path

from .views import (
    ClosePaperStrategyPositionAPIView, StrategyCatalogAPIView,
    StrategySymbolValidationAPIView,
    StrategyDefinitionDetailAPIView, StrategyDefinitionListCreateAPIView,
    StrategyPositionListAPIView, StrategyRuntimeDetailAPIView,
    StrategyRuntimeListCreateAPIView, StrategyTrainAPIView, TrainingRunDetailAPIView,
)

urlpatterns = [
    path("catalog/", StrategyCatalogAPIView.as_view()),
    path("catalog/symbol/", StrategySymbolValidationAPIView.as_view()),
    path("definitions/", StrategyDefinitionListCreateAPIView.as_view()),
    path("definitions/<uuid:strategy_id>/", StrategyDefinitionDetailAPIView.as_view()),
    path("definitions/<uuid:strategy_id>/train/", StrategyTrainAPIView.as_view()),
    path("training/<uuid:run_id>/", TrainingRunDetailAPIView.as_view()),
    path("executions/", StrategyRuntimeListCreateAPIView.as_view()),
    path("executions/<uuid:runtime_id>/", StrategyRuntimeDetailAPIView.as_view()),
    path("positions/", StrategyPositionListAPIView.as_view()),
    path("positions/<uuid:position_id>/close/", ClosePaperStrategyPositionAPIView.as_view()),
]
