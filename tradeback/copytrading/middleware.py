from channels.db import database_sync_to_async
from django.contrib.auth.models import AnonymousUser
from rest_framework_simplejwt.authentication import JWTAuthentication


@database_sync_to_async
def _user_from_token(raw_token):
    auth = JWTAuthentication()
    try:
        return auth.get_user(auth.get_validated_token(raw_token))
    except Exception:
        return AnonymousUser()


class JwtSubprotocolAuthMiddleware:
    """Authenticate WS without putting JWTs in URLs or access logs."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        scope = dict(scope)
        token = next(
            (value[4:] for value in scope.get("subprotocols", []) if value.startswith("jwt.")),
            "",
        )
        scope["user"] = await _user_from_token(token) if token else AnonymousUser()
        return await self.app(scope, receive, send)
