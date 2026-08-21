import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models


ENCRYPTED_PREFIX = "enc:v1:"


def _cipher():
    material = getattr(settings, "EXCHANGE_CREDENTIAL_ENCRYPTION_KEY", settings.SECRET_KEY)
    key = base64.urlsafe_b64encode(hashlib.sha256(material.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_value(value):
    if value in (None, "") or str(value).startswith(ENCRYPTED_PREFIX):
        return value
    token = _cipher().encrypt(str(value).encode("utf-8")).decode("ascii")
    return f"{ENCRYPTED_PREFIX}{token}"


def decrypt_value(value):
    if value in (None, "") or not str(value).startswith(ENCRYPTED_PREFIX):
        # Legacy plaintext is accepted only so the data migration can read it.
        return value
    token = str(value)[len(ENCRYPTED_PREFIX):]
    try:
        return _cipher().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as error:
        raise ValueError("Exchange credential encryption key is invalid or has changed.") from error


class EncryptedTextField(models.TextField):
    description = "Text encrypted with the application credential key"

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        return encrypt_value(value)

    def from_db_value(self, value, expression, connection):
        return decrypt_value(value)
