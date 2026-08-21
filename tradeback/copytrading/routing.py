from django.urls import re_path

from .consumers import CopyTradingConsumer

websocket_urlpatterns = [
    re_path(r"^ws/copy-trading/(?P<strategy_id>[0-9a-f-]+)/$", CopyTradingConsumer.as_asgi()),
]
