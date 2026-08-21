from django.contrib import admin

from .models import CopyExecution, CopyStrategy, TelegramConnection, TelegramMessage, TradeSignal

admin.site.register([TelegramConnection, CopyStrategy, TelegramMessage, TradeSignal, CopyExecution])
