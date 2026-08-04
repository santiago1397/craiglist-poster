"""Guardrail settings and machine-token administration.

The server owns the throttles (decision 14), but the desktop clamps whatever it
receives to ceilings compiled into `craigslist_auto.config`. Bounds here are a
first line of defence, not the only one — a value that slips through still gets
clamped desktop-side and logged.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..auth import require_admin
from ..db import conn, tx
from ..security import (
    issue_api_key,
    issue_machine_token,
    revoke_api_key,
    revoke_machine_token,
)
from ..services import queue as queue_svc

router = APIRouter(dependencies=[Depends(require_admin)])


class GuardrailUpdate(BaseModel):
    min_hours_between_posts_same_account: int | None = Field(default=None, ge=1, le=168)
    max_posts_per_day_total: int | None = Field(default=None, ge=1, le=50)
    # A calendar-day count, unlike the two rolling-window caps either side of it
    # — so this one is set to the figure it enforces, not one above it.
    max_posts_per_account_per_day: int | None = Field(default=None, ge=1, le=24)
    max_posts_per_account_per_week: int | None = Field(default=None, ge=1, le=100)
    post_window_start_hour: int | None = Field(default=None, ge=0, le=23)
    post_window_end_hour: int | None = Field(default=None, ge=1, le=24)
    post_weekdays_only: bool | None = None
    queue_depth_floor: int | None = Field(default=None, ge=0, le=500)
    queue_depth_target: int | None = Field(default=None, ge=1, le=1000)

    # How long an account stands down after a failed post, so one broken
    # account cannot consume every fire of the day. Zero disables the backoff.
    failure_backoff_minutes: int | None = Field(default=None, ge=0, le=1440)
    billing_failure_backoff_minutes: int | None = Field(default=None, ge=0, le=10080)

    # Editing (DESIGN_EDITS.md decision 30). Bounds here are a first line of
    # defence; the desktop clamps again to ceilings compiled into config.py.
    edits_enabled: bool | None = None
    min_hours_between_edits_same_post: int | None = Field(default=None, ge=1, le=720)
    max_edits_per_account_per_day: int | None = Field(default=None, ge=0, le=50)
    max_edits_per_post_lifetime: int | None = Field(default=None, ge=1, le=200)
    edit_window_start_hour: int | None = Field(default=None, ge=0, le=23)
    edit_window_end_hour: int | None = Field(default=None, ge=1, le=24)
    edits_paused_reason: str | None = Field(default=None, max_length=200)

    # Image reuse (migration 0027). These are the revert path for the loosened
    # reuse rules: turning binding back on and raising the cooldown are the two
    # moves if duplicate photos start getting ads ghosted, and both must be
    # possible without a deploy.
    image_owner_binding: bool | None = None
    image_reuse_cooldown_days: int | None = Field(default=None, ge=0, le=365)


class TokenCreate(BaseModel):
    machine: str
    label: str = ""


class ApiKeyCreate(BaseModel):
    label: str = Field(default="", max_length=100)
    # 'read'  — the whole agent read surface. May travel in a URL.
    # 'post'  — the above, plus publishing a draft a human marked reviewed.
    # 'agent' — the above, plus composing: generating images, writing drafts and
    #           attaching pictures. Header-only on every request, including
    #           reads, because a key that can publish must not reach an access
    #           log. It still cannot mark a draft reviewed.
    scope: str = Field(default="read", pattern="^(read|post|agent)$")


class PostingSwitch(BaseModel):
    enabled: bool
    # Free text shown wherever the pause is surfaced, so a week later you know
    # why it was stopped.
    reason: str | None = Field(default=None, max_length=200)


@router.get("/posting")
def get_posting_state() -> dict:
    with conn() as c:
        g = queue_svc.get_guardrails(c)
    return {
        "enabled": g.get("posting_enabled", True),
        "paused_at": g.get("paused_at"),
        "paused_reason": g.get("paused_reason"),
    }


@router.put("/posting")
def set_posting_state(body: PostingSwitch) -> dict:
    """Stop or resume posting across every machine.

    The queue is left alone — drafts keep their order and resume where they
    left off. Takes effect on the next claim; a post already in flight runs to
    completion.
    """
    with tx() as c:
        g = queue_svc.set_posting_enabled(c, enabled=body.enabled, reason=body.reason)
    return {
        "enabled": g["posting_enabled"],
        "paused_at": g.get("paused_at"),
        "paused_reason": g.get("paused_reason"),
    }


@router.get("/guardrails")
def get_guardrails() -> dict:
    with conn() as c:
        return queue_svc.get_guardrails(c)


@router.put("/guardrails")
def put_guardrails(body: GuardrailUpdate) -> dict:
    patch = body.model_dump(exclude_unset=True)
    with tx() as c:
        current = queue_svc.get_guardrails(c)
        merged = {**current, **{k: v for k, v in patch.items() if v is not None}}
        if merged["post_window_start_hour"] >= merged["post_window_end_hour"]:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="post_window_start_hour must be before post_window_end_hour",
            )
        if merged["queue_depth_floor"] > merged["queue_depth_target"]:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="queue_depth_floor cannot exceed queue_depth_target",
            )
        if merged["edit_window_start_hour"] >= merged["edit_window_end_hour"]:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="edit_window_start_hour must be before edit_window_end_hour",
            )
        return queue_svc.update_guardrails(c, patch)


class ProviderEntryUpdate(BaseModel):
    """One provider's config. Applies to the provider named in the same request,
    or to the active one if the request is not changing it."""

    model: str | None = Field(default=None, max_length=100)
    api_base: str | None = Field(default=None, max_length=300)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    aspect: str | None = Field(default=None, max_length=20)
    cost_usd: float | None = Field(default=None, ge=0.0, le=100.0)
    options: dict | None = None
    # Omit to leave the stored key alone; send "" to clear it. The UI renders a
    # last-four hint and never echoes it back here, so a plain form submit
    # cannot overwrite a real key with its own fingerprint.
    api_key: str | None = Field(default=None, max_length=500)


class GenerationUpdate(BaseModel):
    enabled: bool | None = None
    text_provider: str | None = Field(default=None, max_length=40)
    image_provider: str | None = Field(default=None, max_length=40)
    text_provider_config: ProviderEntryUpdate | None = None
    image_provider_config: ProviderEntryUpdate | None = None
    system_prompt: str | None = None
    user_template: str | None = None
    tail_template: str | None = None
    photos_min: int | None = Field(default=None, ge=0, le=24)
    photos_max: int | None = Field(default=None, ge=0, le=24)
    imageless_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    # Background photo-stack refill. Ships off; turn it on once the image
    # prompts are settled, since this is what spends money on them unattended.
    image_topup_enabled: bool | None = None
    image_stack_floor: int | None = Field(default=None, ge=0, le=100_000)
    image_stack_target: int | None = Field(default=None, ge=0, le=100_000)
    image_topup_batch: int | None = Field(default=None, ge=1, le=100)


@router.get("/generation")
def get_generation() -> dict:
    """Prompts, providers and run stats. `seed_ads` count tells you whether the
    workbook fallback has anything to fall back to.

    Provider entries come back redacted — each carries a `key` status, never a
    key. `tests/test_provider_keys.py` asserts that this response contains
    neither the ciphertext nor the plaintext.
    """
    from ..services import generator, providers as providers_svc

    with conn() as c:
        g = generator.get_generation_settings(c)
        # The banner this feeds is about ad copy, so it tracks the active *text*
        # provider — the one whose absence silently degrades every draft to
        # workbook copy.
        g["api_key_configured"] = bool(
            providers_svc.active_config(c, kind="text")["api_key"]
        )
        g["known_text_providers"] = list(providers_svc.TEXT_PROVIDERS)
        g["known_image_providers"] = list(providers_svc.IMAGE_PROVIDERS)
        g["seed_ads"] = c.execute(
            "SELECT COUNT(*) AS n FROM seed_ads WHERE active"
        ).fetchone()["n"]
    return g


@router.put("/generation")
def put_generation(body: GenerationUpdate) -> dict:
    from ..services import generator, providers as providers_svc

    patch = body.model_dump(exclude_unset=True)

    known = {"text": providers_svc.TEXT_PROVIDERS, "image": providers_svc.IMAGE_PROVIDERS}
    for kind, names in known.items():
        chosen = patch.get(f"{kind}_provider")
        if chosen is not None and chosen not in names:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"unknown {kind} provider {chosen!r}. Known: "
                    f"{', '.join(names)}."
                ),
            )

    with tx() as c:
        # Check against the merged result, not the patch: sending only
        # photos_max could otherwise invert the range against the stored min.
        merged = {**generator.get_generation_settings(c),
                  **{k: v for k, v in patch.items() if v is not None}}
        if merged["photos_min"] > merged["photos_max"]:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"photos_min ({merged['photos_min']}) cannot exceed "
                    f"photos_max ({merged['photos_max']})"
                ),
            )
        if merged["image_stack_floor"] > merged["image_stack_target"]:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"image_stack_floor ({merged['image_stack_floor']}) cannot "
                    f"exceed image_stack_target ({merged['image_stack_target']})"
                ),
            )

        # Provider entries are written before the selector, so a key supplied in
        # this same request counts toward the check below. Everything here is
        # one transaction, so a refusal rolls the key write back with it.
        touched: list[str] = []
        for kind in ("text", "image"):
            entry = patch.pop(f"{kind}_provider_config", None)
            selected = patch.get(f"{kind}_provider")
            if entry is None and selected is None:
                continue
            touched.append(kind)
            target = selected or merged[f"{kind}_provider"]
            if entry:
                generator.update_provider_entry(
                    c, kind=kind, provider=target, patch=entry
                )

        result = generator.update_generation_settings(c, patch)

        # Refuse a provider with no reachable key. Only for the generators this
        # request actually touched — saving a photo count should not fail
        # because some other provider is half-configured.
        #
        # This matters most for text, which fails *silently*: build_draft falls
        # back to workbook copy so the queue keeps filling, and weeks of
        # repeated copy is the documented cause of ghosting. An image provider
        # with no key announces itself the first time you press Generate.
        for kind in touched:
            cfg = providers_svc.active_config(c, kind=kind)
            if not cfg["api_key"]:
                env_var = providers_svc.env_var_name(cfg["provider"])
                remedy = (
                    f" Enter one in its section, or set {env_var} on the server "
                    f"and redeploy."
                    if env_var
                    else " Enter one in its section."
                )
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        cfg.get("key_error")
                        or (
                            f"{cfg['provider']} has no API key, so it cannot be "
                            f"the {kind} provider.{remedy}"
                        )
                    ),
                )
        return result


@router.get("/machine-tokens")
def list_machine_tokens() -> dict:
    with conn() as c:
        rows = c.execute(
            "SELECT id, machine, label, created_at, last_seen_at, revoked_at "
            "FROM machine_tokens ORDER BY created_at DESC"
        ).fetchall()
    return {"tokens": [dict(r) for r in rows]}


@router.post("/machine-tokens", status_code=status.HTTP_201_CREATED)
def create_machine_token(body: TokenCreate) -> dict:
    """The plaintext token is shown once and never stored. Copy it now."""
    token = issue_machine_token(body.machine, body.label)
    return {"machine": body.machine, "token": token}


@router.delete("/machine-tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_machine_token(token_id: int) -> None:
    if not revoke_machine_token(token_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Token not found or already revoked"
        )


# ---------------------------------------------------------------------------
# Agent API keys
#
# Separate table from machine tokens on purpose — see migration 0018. These
# open `/agent/*` and nothing else; they can never claim a draft or report an
# event.
# ---------------------------------------------------------------------------

@router.get("/api-keys")
def list_api_keys() -> dict:
    """Every key, with what each has spent and written.

    Image generation through an `agent` key is deliberately uncapped, so this
    is the entire control: the spend has to be visible somewhere, or "no cap,
    but logged" is just "no cap".
    """
    from ..services import images as images_svc

    with conn() as c:
        rows = c.execute(
            "SELECT id, label, scope, created_at, last_seen_at, revoked_at "
            "FROM api_keys ORDER BY created_at DESC"
        ).fetchall()
        usage = images_svc.key_usage(c)
    empty = {"images_generated": 0, "cost_usd": 0.0, "drafts_created": 0}
    return {"keys": [{**dict(r), **usage.get(r["id"], empty)} for r in rows]}


@router.post("/api-keys", status_code=status.HTTP_201_CREATED)
def create_api_key(body: ApiKeyCreate) -> dict:
    """The plaintext key is shown once and never stored. Copy it now."""
    key = issue_api_key(body.label, body.scope)
    return {"label": body.label, "scope": body.scope, "key": key}


@router.delete("/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_api_key(key_id: int) -> None:
    if not revoke_api_key(key_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Key not found or already revoked",
        )
