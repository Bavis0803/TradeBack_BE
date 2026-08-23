from django.contrib import admin

from .models import (
    AISignalAnalysis, CopyExecution, CopyStrategy,
    TelegramConnection, TelegramMessage, TradeSignal,
)

admin.site.register([
    TelegramConnection, AISignalAnalysis, CopyStrategy,
    TelegramMessage, TradeSignal, CopyExecution,
])
