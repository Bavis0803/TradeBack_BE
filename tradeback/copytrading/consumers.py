from channels.generic.websocket import AsyncJsonWebsocketConsumer

from .models import CopyStrategy


class CopyTradingConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        user = self.scope.get("user")
        strategy_id = self.scope["url_route"]["kwargs"]["strategy_id"]
        if not user or not user.is_authenticated:
            await self.close(code=4401)
            return
        exists = await CopyStrategy.objects.filter(id=strategy_id, user=user).aexists()
        if not exists:
            await self.close(code=4404)
            return
        self.group_name = f"copy_{strategy_id}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept(subprotocol="tradeback")

    async def disconnect(self, code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def copy_event(self, event):
        await self.send_json(event["payload"])
