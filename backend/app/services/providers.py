"""Resolving which provider is active, and getting its key.

Two rules live here and nowhere else.

**Plaintext keys do not leave this module except through `active_config`.**
`GET /settings/generation` returns the settings row wholesale, so a key field
sitting in that row would serialize straight to the browser. `redacted()` is
what the API gets; `active_config()` is what the two call sites that actually
talk to a provider get. Ciphertext never exists above the service layer.

**Resolution order is stored key, then environment, then nothing.** The
environment is a permanent fallback rather than something adopted into the
database on first read (DESIGN_PROVIDERS decision 7). An adopted row would
shadow the env var, so a later `jwt_secret` rotation would leave a perfectly
good environment variable being ignored in favour of a row that no longer
decrypts. As it stands, clearing the stored key is a complete recovery.
"""
from __future__ import annotations

import psycopg
from loguru import logger

from ..config import get_settings
from . import secrets as secrets_svc

# Providers the UI may offer. A name absent from these is refused by the router
# rather than reaching `build_provider` and failing at generation time.
TEXT_PROVIDERS = ("minimax", "openai")
IMAGE_PROVIDERS = ("minimax", "openai")

# Break-glass environment variables, read only when no key is stored. Gemini is
# listed ahead of its adapter so a key can be in place before the adapter lands.
_ENV_ATTR = {
    "minimax": "minimax_api_key",
    "openai": "openai_api_key",
    "gemini": "gemini_api_key",
}


def _blob_column(kind: str) -> str:
    if kind not in ("text", "image"):
        raise ValueError(f"kind must be 'text' or 'image', got {kind!r}")
    return f"{kind}_providers"


def env_key(provider: str) -> str:
    attr = _ENV_ATTR.get(provider)
    return (getattr(get_settings(), attr, "") or "") if attr else ""


def env_var_name(provider: str) -> str:
    """The environment variable backing this provider, for error messages.

    Returns "" for a provider with no fallback, so a caller can tell the
    difference between "set THIS" and "there is nothing to set".
    """
    return (_ENV_ATTR.get(provider) or "").upper()


def resolve_key(entry: dict, provider: str) -> tuple[str, str | None]:
    """Return (plaintext, error). Never raises.

    A stored key that will not decrypt is reported as an error *and* falls
    through to the environment, because a broken stored key should not be worse
    than never having stored one.
    """
    error: str | None = None
    stored = (entry or {}).get("api_key_enc")
    if stored:
        try:
            return secrets_svc.decrypt(stored), None
        except secrets_svc.DecryptionError as e:
            error = str(e)
            logger.warning(f"{provider}: {e}")
    fallback = env_key(provider)
    if fallback:
        return fallback, error
    return "", error


def key_status(entry: dict, provider: str) -> dict:
    """What the UI is allowed to know about a key: whether it works, and which
    one it is. Never the key."""
    plaintext, error = resolve_key(entry, provider)
    if not plaintext:
        source = None
    elif (entry or {}).get("api_key_enc") and not error:
        source = "stored"
    else:
        # Either nothing was stored, or what was stored would not decrypt and
        # the environment covered for it. Both are "environment" to a reader.
        source = "environment"
    return {
        "configured": bool(plaintext),
        "hint": secrets_svc.hint(plaintext) if plaintext else None,
        "source": source,
        "error": error,
    }


def redacted(row: dict, *, kind: str) -> dict:
    """Every configured provider for this kind, with keys replaced by status."""
    out = {}
    for name, entry in (row.get(_blob_column(kind)) or {}).items():
        clean = {k: v for k, v in (entry or {}).items() if k != "api_key_enc"}
        clean["key"] = key_status(entry, name)
        out[name] = clean
    return out


def active_config(conn: psycopg.Connection, *, kind: str) -> dict:
    """The active provider's config, with `api_key` resolved to plaintext.

    The only route to a usable key. Callers are `generator.call_model` and
    `images.generate_images`, and there is no reason for a third.
    """
    row = conn.execute(
        f"SELECT {kind}_provider AS provider, {_blob_column(kind)} AS blob "
        "FROM generation_settings LIMIT 1"
    ).fetchone()
    if row is None:  # pragma: no cover — migration seeds the singleton
        raise RuntimeError("generation_settings is empty; run migrations")

    provider = row["provider"]
    entry = dict((row["blob"] or {}).get(provider) or {})
    api_key, error = resolve_key(entry, provider)
    entry.pop("api_key_enc", None)
    entry["provider"] = provider
    entry["api_key"] = api_key
    entry["key_error"] = error
    entry.setdefault("options", {})
    return entry
