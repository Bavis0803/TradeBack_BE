from django.db import transaction
from django.utils import timezone

from .models import ExchangeAccount, ExchangeCredential
from .services import BinanceService


def mask_api_key(api_key):
    if len(api_key) <= 8:
        return "****"
    return f"{api_key[:4]}****{api_key[-4:]}"


def serialize_exchange_account(account):
    return {
        "exchange": account.exchange,
        "status": account.status,
        "is_testnet": account.is_testnet,
        "api_key_hint": account.api_key_hint,
        "last_verified_at": account.last_verified_at,
        "last_synced_at": account.last_synced_at,
        "last_error": account.last_error or None,
        "created_at": account.created_at,
        "updated_at": account.updated_at,
    }


def get_binance_account(user):
    return (
        ExchangeAccount.objects.filter(
            user=user, exchange=ExchangeAccount.Exchange.BINANCE
        )
        .first()
    )


def build_binance_service(account):
    credential = account.credential
    return BinanceService(credential.api_key, credential.api_secret, account.is_testnet)


def connect_binance_account(user, api_key, api_secret, is_testnet=False):
    service = BinanceService(api_key, api_secret, is_testnet)
    result = service.verify_credentials()
    if not result["success"]:
        return None, result

    now = timezone.now()
    with transaction.atomic():
        account, _ = ExchangeAccount.objects.update_or_create(
            user=user,
            exchange=ExchangeAccount.Exchange.BINANCE,
            defaults={
                "status": ExchangeAccount.Status.CONNECTED,
                "api_key_hint": mask_api_key(api_key),
                "is_testnet": is_testnet,
                "last_verified_at": now,
                "last_synced_at": now,
                "last_error": "",
            },
        )
        ExchangeCredential.objects.update_or_create(
            account=account,
            defaults={"api_key": api_key, "api_secret": api_secret},
        )
    return account, result


def verify_binance_account(account):
    result = build_binance_service(account).verify_credentials()
    now = timezone.now()
    if result["success"]:
        account.status = ExchangeAccount.Status.CONNECTED
        account.last_verified_at = now
        account.last_synced_at = now
        account.last_error = ""
    else:
        account.status = ExchangeAccount.Status.ERROR
        account.last_error = result["message"][:500]
    account.save(
        update_fields=(
            "status",
            "last_verified_at",
            "last_synced_at",
            "last_error",
            "updated_at",
        )
    )
    return result


def touch_exchange_sync(account):
    now = timezone.now()
    ExchangeAccount.objects.filter(pk=account.pk).update(last_synced_at=now)
    account.last_synced_at = now
