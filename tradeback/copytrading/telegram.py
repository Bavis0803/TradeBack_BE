import asyncio
from datetime import timedelta
import logging
from pathlib import PurePath
import re
from urllib.parse import unquote, urlparse

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.files.base import ContentFile
from django.utils import timezone
from telethon import TelegramClient, events, utils
from telethon.errors import (
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeHashEmptyError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

from .execution import process_telegram_message
from .positions import get_position_payload


logger = logging.getLogger(__name__)


def chat_reference_candidates(chat_reference):
    """Return Telegram entity references for usernames, IDs and Web links.

    Telegram Web represents channels as ``#-<channel id>`` while Telethon's
    canonical peer id is ``-100<channel id>``. Numeric values must also be
    passed to Telethon as integers; otherwise they are treated as usernames.
    """
    reference = (chat_reference or "").strip()
    parsed = urlparse(reference)
    if parsed.netloc.lower() == "web.telegram.org" and parsed.fragment:
        reference = unquote(parsed.fragment).lstrip("#/").strip()

    if not re.fullmatch(r"-?\d+", reference):
        return (reference,)

    peer_id = int(reference)
    digits = str(abs(peer_id))
    if digits.startswith("100") and peer_id < 0:
        return (peer_id,)

    channel_peer_id = -int(f"100{digits}")
    if peer_id > 0:
        return (channel_peer_id,)

    # A negative Telegram Web id is usually a channel/supergroup id, but a
    # legacy basic group legitimately uses the unprefixed negative id.
    return (channel_peer_id, peer_id)


def phone_hint(phone):
    cleaned = (phone or "").strip()
    return f"***{cleaned[-4:]}" if len(cleaned) >= 4 else "***"


async def begin_login(connection, phone):
    client = TelegramClient(StringSession(connection.session or ""), connection.api_id, connection.api_hash)
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
        connection.session = StringSession.save(client.session)
        connection.phone = phone
        connection.phone_hint = phone_hint(phone)
        connection.phone_code_hash = sent.phone_code_hash
        connection.challenge_expires_at = timezone.now() + timedelta(minutes=10)
        connection.status = connection.Status.PENDING
        connection.last_error = ""
        await connection.asave()
    finally:
        await client.disconnect()


async def verify_login(connection, code, password=""):
    if not connection.challenge_expires_at or connection.challenge_expires_at < timezone.now():
        raise ValueError("Telegram verification code has expired. Request a new code.")
    client = TelegramClient(StringSession(connection.session), connection.api_id, connection.api_hash)
    await client.connect()
    try:
        try:
            try:
                await client.sign_in(
                    phone=connection.phone, code=code, phone_code_hash=connection.phone_code_hash
                )
            except SessionPasswordNeededError:
                if not password:
                    raise ValueError("Telegram 2-step verification password is required.")
                await client.sign_in(password=password)
        except PhoneCodeInvalidError as exc:
            raise ValueError(
                "That Telegram login code is incorrect. Use the newest code from the verified Telegram service chat."
            ) from exc
        except (PhoneCodeExpiredError, PhoneCodeHashEmptyError) as exc:
            raise ValueError(
                "That Telegram login code has expired. Return to the previous step and request a new code."
            ) from exc
        except FloodWaitError as exc:
            raise ValueError(
                f"Telegram temporarily limited verification attempts. Try again in {exc.seconds} seconds."
            ) from exc
        if not await client.is_user_authorized():
            raise ValueError("Telegram authorization was not completed.")
        connection.session = StringSession.save(client.session)
        connection.phone_code_hash = ""
        connection.challenge_expires_at = None
        connection.status = connection.Status.CONNECTED
        connection.last_connected_at = timezone.now()
        connection.last_error = ""
        await connection.asave()
    finally:
        await client.disconnect()


async def resolve_chat(connection, chat_reference):
    client = TelegramClient(StringSession(connection.session), connection.api_id, connection.api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise ValueError("Telegram session is no longer authorized.")
        candidates = chat_reference_candidates(chat_reference)
        entity = None
        if all(isinstance(candidate, int) for candidate in candidates):
            candidate_ids = set(candidates)
            async for dialog in client.iter_dialogs():
                if int(utils.get_peer_id(dialog.entity)) in candidate_ids:
                    entity = dialog.entity
                    break

        last_error = None
        if entity is None:
            for candidate in candidates:
                try:
                    entity = await client.get_entity(candidate)
                    break
                except (TypeError, ValueError) as exc:
                    last_error = exc
        if entity is None:
            raise last_error or ValueError("Telegram chat was not found in this account's dialogs.")
        chat_id = int(utils.get_peer_id(entity))
        if chat_id >= 0 or not getattr(entity, "title", None):
            raise ValueError("The reference must point to a Telegram group or channel.")
        title = getattr(entity, "title", None) or getattr(entity, "username", None) or str(chat_id)
        username = getattr(entity, "username", "") or ""
        return {"chat_id": chat_id, "chat_title": title, "chat_username": username}
    finally:
        await client.disconnect()


async def get_strategy_telegram_connection(strategy):
    """Load the Telegram connection without triggering sync ORM in async code."""
    from .models import TelegramConnection

    return await TelegramConnection.objects.aget(pk=strategy.telegram_connection_id)


async def import_history(strategy, limit=50):
    connection = await get_strategy_telegram_connection(strategy)
    client = TelegramClient(StringSession(connection.session), connection.api_id, connection.api_hash)
    await client.connect()
    try:
        entity = await client.get_entity(strategy.chat_username or strategy.chat_id)
        messages = [item async for item in client.iter_messages(entity, limit=min(limit, 100))]
        for item in reversed(messages):
            await _process_item(client, strategy, item, execute=False)
        if messages:
            strategy.last_message_id = max(item.id for item in messages)
            await strategy.asave(update_fields=("last_message_id", "updated_at"))
    finally:
        await client.disconnect()


def _save_media(message, payload, media_type, mime_type, extension):
    if message.media_file or not payload:
        return
    safe_extension = extension if extension.startswith(".") and len(extension) <= 10 else ""
    message.media_file.save(
        f"telegram-{message.strategy_id}-{message.telegram_message_id}{safe_extension}",
        ContentFile(payload),
        save=False,
    )
    message.media_type = media_type
    message.media_mime_type = mime_type or "application/octet-stream"
    message.media_size = len(payload)
    message.save(update_fields=("media_file", "media_type", "media_mime_type", "media_size"))


def _mark_media(message, media_type):
    if message.media_type:
        return
    message.media_type = media_type
    message.save(update_fields=("media_type",))


async def _attach_media(client, item, message):
    if message.media_file or message.media_type:
        return
    file_info = getattr(item, "file", None)
    mime_type = getattr(file_info, "mime_type", "") or ""
    is_photo = bool(getattr(item, "photo", None))
    is_image_document = bool(getattr(item, "document", None)) and mime_type.startswith("image/")
    if not is_photo and not is_image_document:
        await sync_to_async(_mark_media, thread_sensitive=True)(
            message, "UNSUPPORTED" if getattr(item, "media", None) else "NONE"
        )
        return
    expected_size = int(getattr(file_info, "size", 0) or 0)
    if expected_size > settings.COPY_TRADING_MEDIA_MAX_BYTES:
        logger.warning("Telegram image %s exceeds the configured media limit.", item.id)
        await sync_to_async(_mark_media, thread_sensitive=True)(message, "TOO_LARGE")
        return
    try:
        payload = await client.download_media(item, file=bytes)
        if not payload or len(payload) > settings.COPY_TRADING_MEDIA_MAX_BYTES:
            return
        extension = getattr(file_info, "ext", "") or PurePath(getattr(file_info, "name", "") or "").suffix
        await sync_to_async(_save_media, thread_sensitive=True)(
            message, payload, "PHOTO", mime_type or "image/jpeg", extension or ".jpg"
        )
    except Exception:
        logger.exception("Unable to download media for Telegram message %s.", item.id)


async def _process_item(client, strategy, item, execute=True):
    sender = getattr(item, "sender", None)
    sender_name = utils.get_display_name(sender) if sender else ""
    result = await sync_to_async(process_telegram_message, thread_sensitive=True)(
        strategy, item.id, item.raw_text or "", item.date, sender_name, execute
    )
    await _attach_media(client, item, result[0])
    if not strategy.last_message_id or item.id > strategy.last_message_id:
        strategy.last_message_id = item.id
        await strategy.asave(update_fields=("last_message_id", "updated_at"))
    return result


async def _active_strategies(connection):
    return [
        strategy async for strategy in connection.strategies.filter(status="ACTIVE")
    ]


async def _backfill_recent_media(client, strategy, entity):
    missing_ids = [
        message_id async for message_id in strategy.messages.filter(media_type="")
        .order_by("-telegram_message_id")
        .values_list("telegram_message_id", flat=True)[:50]
    ]
    if not missing_ids:
        return
    items = await client.get_messages(entity, ids=missing_ids)
    for item in items:
        if item:
            await _process_item(client, strategy, item, execute=False)


async def _catch_up(client, connection, on_processed=None):
    """Persist messages missed while the worker was stopped or reconnecting."""
    for strategy in await _active_strategies(connection):
        try:
            entity = await client.get_entity(strategy.chat_username or strategy.chat_id)
            await _backfill_recent_media(client, strategy, entity)
            missed = [
                item async for item in client.iter_messages(
                    entity, min_id=strategy.last_message_id or 0, limit=100
                )
            ]
            for item in reversed(missed):
                age = timezone.now() - item.date
                execute = age <= timedelta(seconds=settings.COPY_TRADING_SIGNAL_MAX_AGE_SECONDS)
                result = await _process_item(client, strategy, item, execute=execute)
                if on_processed:
                    await on_processed(strategy, *result)
        except Exception as exc:
            strategy.last_error = f"Telegram catch-up failed: {exc}"[:500]
            await strategy.asave(update_fields=("last_error", "updated_at"))
            logger.exception("Telegram catch-up failed for strategy %s.", strategy.id)


async def _reconcile_loop(client, connection, on_processed):
    while client.is_connected():
        await asyncio.sleep(20)
        await _catch_up(client, connection, on_processed)
        await sync_to_async(get_position_payload, thread_sensitive=True)(connection.user)


async def listen_connection(connection, on_processed=None):
    """Long-running listener used only by the dedicated management command."""
    client = TelegramClient(StringSession(connection.session), connection.api_id, connection.api_hash)
    @client.on(events.NewMessage)
    async def handle(event):
        strategy = await sync_to_async(
            connection.strategies.filter(
                status="ACTIVE", chat_id=int(event.chat_id or 0)
            ).first,
            thread_sensitive=True,
        )()
        if not strategy:
            return
        result = await _process_item(client, strategy, event.message, execute=True)
        if on_processed:
            await on_processed(strategy, *result)

    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise ValueError("Telegram session is no longer authorized.")
    await _catch_up(client, connection, on_processed)
    reconcile_task = asyncio.create_task(_reconcile_loop(client, connection, on_processed))
    try:
        await client.run_until_disconnected()
    finally:
        reconcile_task.cancel()
        await asyncio.gather(reconcile_task, return_exceptions=True)
