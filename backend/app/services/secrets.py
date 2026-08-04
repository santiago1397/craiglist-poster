"""Reversible storage for outbound provider API keys.

Every other secret in this database is an argon2 hash — machine tokens, the
admin password, agent API keys — because nothing ever needs to read them back.
A model provider's key is different in kind: it is sent upstream verbatim on
every call, so it has to be recoverable. That is the only reason this module
exists, and it should not acquire other callers.

**The Fernet key is derived from `jwt_secret`** rather than being its own
setting (DESIGN_PROVIDERS decision 2). That couples two unrelated concerns on
purpose: rotating the JWT secret invalidates every stored provider key. It is
survivable because the recovery is re-pasting two API keys, and because callers
fall back to the environment when a stored value will not decrypt.

**Do not pass `jwt_secret` to Fernet directly.** Fernet requires exactly 32
urlsafe-base64-encoded bytes. `jwt_secret` is an arbitrary string of length >=32
and Fernet raises `ValueError` on it. The sha256 below is what bridges the two,
and anyone who "simplifies" it away breaks decryption for every key already
stored — silently, because decryption failures surface as a provider looking
unconfigured rather than as a crash.
"""
from __future__ import annotations

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from ..config import get_settings


class DecryptionError(RuntimeError):
    """A stored key could not be read back.

    Almost always means `jwt_secret` changed since the key was written. Callers
    treat this as "no key configured" and say so, rather than propagating — see
    the fail-at-use decision in DESIGN_PROVIDERS.
    """


@lru_cache
def _fernet() -> Fernet:
    digest = hashlib.sha256(get_settings().jwt_secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError, TypeError) as e:
        raise DecryptionError(
            "stored API key could not be decrypted — JWT_SECRET may have "
            "changed since it was saved. Re-enter the key in Settings, or "
            "set the provider's environment variable."
        ) from e


def hint(plaintext: str) -> str:
    """A last-four fingerprint, so the UI can show *which* key is stored.

    Short keys are reported as set without revealing any of them; there is no
    length at which showing half a secret is a good trade.
    """
    return f"…{plaintext[-4:]}" if len(plaintext) >= 12 else "(set)"
