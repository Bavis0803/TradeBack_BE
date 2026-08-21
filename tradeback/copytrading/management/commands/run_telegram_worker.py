import asyncio
import logging

from channels.layers import get_channel_layer
from django.core.management.base import BaseCommand

from copytrading.models import TelegramConnection
from copytrading.serializers import TelegramMessageSerializer
from copytrading.telegram import listen_connection

logger = logging.getLogger(__name__)


async def _broadcast(strategy, message, signal, execution):
    payload = await asyncio.to_thread(lambda: TelegramMessageSerializer(message).data)
    await get_channel_layer().group_send(
        f"copy_{strategy.id}", {"type": "copy.event", "payload": {"type": "message", "message": payload}}
    )


async def _run():
    connections = [
        item async for item in TelegramConnection.objects.filter(
            status=TelegramConnection.Status.CONNECTED
        ).select_related("user")
    ]
    if not connections:
        logger.warning("No connected Telegram accounts; worker is idle.")
        return
    await asyncio.gather(
        *(listen_connection(connection, _broadcast) for connection in connections),
        return_exceptions=False,
    )


class Command(BaseCommand):
    help = "Listen for Telegram messages and process copy-trading signals."

    def handle(self, *args, **options):
        self.stdout.write("Starting Telegram copy-trading worker...")
        asyncio.run(_run())
