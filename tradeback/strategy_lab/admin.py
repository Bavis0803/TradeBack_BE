from django.contrib import admin

from .models import (
    StrategyBacktestResult, StrategyDefinition, StrategyPosition, StrategyRuntime,
    StrategyTrainingRun,
)

admin.site.register(StrategyDefinition)
admin.site.register(StrategyTrainingRun)
admin.site.register(StrategyBacktestResult)
admin.site.register(StrategyRuntime)
admin.site.register(StrategyPosition)
