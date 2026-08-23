from django.contrib import admin

from .models import (
    AISignalAnalysis, CopyExecution, CopyStrategy, SignalCandidate,
    TelegramConnection, TelegramMessage, TradeSignal,
)

admin.site.register([
    TelegramConnection, AISignalAnalysis, CopyStrategy,
    TelegramMessage, TradeSignal, SignalCandidate, CopyExecution,
])
