"""Image generation providers.

One narrow interface — `generate(prompt, aspect, n) -> list[bytes]` — with an
adapter per vendor. Moving to another provider means adding an adapter here and
changing a setting; nothing else in the codebase knows which one drew the
picture.

**Every adapter returns bytes at the requested aspect ratio.** That is a
contract, not a request parameter, because the providers disagree about whether
aspect is expressible at all: MiniMax takes `aspect_ratio` directly, OpenAI
offers three fixed sizes and no 4:3 among them. An adapter that cannot ask for
the ratio crops to it before returning.

The ratio matters more than it looks. Craigslist's largest display variant is
`_1200x900.jpg` — 4:3 exactly, see `images._LARGEST_VARIANT` — so an image at
any other ratio is cropped by the site instead, which on a cover takes the
composited phone number with it.
"""
from __future__ import annotations

import base64
import io
from typing import Protocol

import httpx
from loguru import logger

REQUEST_TIMEOUT = 180.0


class ImageGenError(RuntimeError):
    """Provider failed. Callers treat images as optional, so this is never fatal
    to a draft — the post simply goes out with fewer pictures."""


class ImageProvider(Protocol):
    name: str

    def generate(self, prompt: str, *, aspect: str, n: int) -> list[bytes]:
        ...


# ---------------------------------------------------------------------------
# Aspect enforcement
# ---------------------------------------------------------------------------

def parse_aspect(aspect: str) -> float:
    """'4:3' -> 1.333…. Falls back to 4:3 rather than raising, because a
    malformed setting should cost a ratio, not a batch of paid-for images."""
    try:
        w, h = aspect.split(":")
        ratio = float(w) / float(h)
        if ratio > 0:
            return ratio
    except (ValueError, ZeroDivisionError, AttributeError):
        pass
    logger.warning(f"unparseable aspect {aspect!r}; treating as 4:3")
    return 4 / 3


def crop_to_aspect(data: bytes, aspect: str) -> bytes:
    """Center-crop to the target ratio. Returns the input untouched if it is
    already within half a percent, so a provider that got it right pays no
    re-encode and keeps its original bytes — and therefore its digest."""
    from PIL import Image

    target = parse_aspect(aspect)
    try:
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGB")
            w, h = im.size
            if abs((w / h) - target) <= target * 0.005:
                return data
            if (w / h) > target:
                new_w = max(1, round(h * target))
                box = ((w - new_w) // 2, 0, (w - new_w) // 2 + new_w, h)
            else:
                new_h = max(1, round(w / target))
                box = (0, (h - new_h) // 2, w, (h - new_h) // 2 + new_h)
            out = io.BytesIO()
            im.crop(box).save(out, format="JPEG", quality=90)
            logger.debug(f"cropped {w}x{h} to {box[2]-box[0]}x{box[3]-box[1]} for {aspect}")
            return out.getvalue()
    except Exception as e:
        # A crop failure must not lose an image that has already been paid for.
        # Craigslist will crop it for us; that is worse, not fatal.
        logger.warning(f"could not crop to {aspect}: {e}; using the original")
        return data


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------

class MiniMaxImages:
    """MiniMax image-01.

    Two quirks worth knowing: it answers HTTP 200 even for logical failures,
    putting the real result in `base_resp.status_code`, and it returns images as
    bare base64 rather than data URLs.
    """

    name = "minimax"

    def __init__(self, api_key: str, api_base: str, model: str, options: dict | None = None) -> None:
        if not api_key:
            raise ImageGenError("no API key is configured for MiniMax")
        self.api_key = api_key
        self.api_base = api_base.rstrip("/")
        self.model = model
        self.options = options or {}

    def generate(self, prompt: str, *, aspect: str = "4:3", n: int = 1) -> list[bytes]:
        try:
            resp = httpx.post(
                f"{self.api_base}/image_generation",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "aspect_ratio": aspect,
                    "response_format": "base64",
                    "n": n,
                    **self.options,
                },
                timeout=REQUEST_TIMEOUT,
            )
        except httpx.HTTPError as e:
            raise ImageGenError(f"image request failed: {e!r}") from e

        if resp.status_code // 100 != 2:
            raise ImageGenError(f"image HTTP {resp.status_code}: {resp.text[:200]}")

        try:
            body = resp.json()
        except ValueError as e:
            raise ImageGenError(f"image response was not JSON: {e}") from e

        # HTTP 200 is not success on its own here.
        base_resp = body.get("base_resp") or {}
        if base_resp.get("status_code", 0) != 0:
            raise ImageGenError(
                f"provider error {base_resp.get('status_code')}: "
                f"{base_resp.get('status_msg')}"
            )

        encoded = (body.get("data") or {}).get("image_base64") or []
        if not encoded:
            raise ImageGenError(f"no images returned: {str(body)[:200]}")

        out = [_decode(item) for item in encoded]
        logger.info(f"generated {len(out)} image(s) via {self.name}/{self.model}")
        # MiniMax honours aspect_ratio, so this is normally a no-op that returns
        # the original bytes. It runs anyway: the setting is free text.
        return [crop_to_aspect(b, aspect) for b in out]


class OpenAIImages:
    """OpenAI gpt-image-1.

    **It has no 4:3.** The sizes are 1024x1024, 1536x1024 and 1024x1536, so the
    landscape option is 3:2 and the ratio has to be reached by cropping. The
    default `size` is 1536x1024 because cropping 3:2 down to 4:3 costs ~11% of
    the width, where cropping a square would cost 25% of the height — and the
    model composes for the frame it is given.

    `quality` is the dominant cost lever and lives in `options`, so it travels
    with the provider entry that also carries `cost_usd`. Changing one without
    the other makes `key_usage()` — the only control on uncapped agent spend —
    report a number that is no longer true.
    """

    name = "openai"

    def __init__(self, api_key: str, api_base: str, model: str, options: dict | None = None) -> None:
        if not api_key:
            raise ImageGenError("no API key is configured for OpenAI")
        self.api_key = api_key
        self.api_base = api_base.rstrip("/")
        self.model = model
        self.options = dict(options or {})

    def generate(self, prompt: str, *, aspect: str = "4:3", n: int = 1) -> list[bytes]:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "n": n,
            "size": self.options.get("size", "1536x1024"),
            "quality": self.options.get("quality", "medium"),
        }
        # Anything else the operator put in options rides along untouched, minus
        # the two keys consumed above.
        payload.update(
            {k: v for k, v in self.options.items() if k not in ("size", "quality")}
        )
        try:
            resp = httpx.post(
                f"{self.api_base}/images/generations",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )
        except httpx.HTTPError as e:
            raise ImageGenError(f"image request failed: {e!r}") from e

        if resp.status_code // 100 != 2:
            raise ImageGenError(f"image HTTP {resp.status_code}: {resp.text[:200]}")

        try:
            body = resp.json()
        except ValueError as e:
            raise ImageGenError(f"image response was not JSON: {e}") from e

        items = body.get("data") or []
        encoded = [i.get("b64_json") for i in items if isinstance(i, dict) and i.get("b64_json")]
        if not encoded:
            raise ImageGenError(f"no images returned: {str(body)[:200]}")

        out = [_decode(item) for item in encoded]
        logger.info(f"generated {len(out)} image(s) via {self.name}/{self.model}")
        # Not optional here: gpt-image-1 cannot produce 4:3, so this is what
        # makes the adapter honour the ratio it was asked for.
        return [crop_to_aspect(b, aspect) for b in out]


def _decode(item: str) -> bytes:
    # Tolerate a data: URL prefix in case a format ever changes.
    if isinstance(item, str) and item.startswith("data:"):
        item = item.split(",", 1)[-1]
    try:
        return base64.b64decode(item)
    except Exception as e:
        raise ImageGenError(f"could not decode returned image: {e}") from e


_ADAPTERS = {
    "minimax": MiniMaxImages,
    "openai": OpenAIImages,
}


def build_provider(
    name: str, *, api_key: str, api_base: str, model: str, options: dict | None = None
) -> ImageProvider:
    """Adding a provider is one class above and one entry in `_ADAPTERS`.

    The adapter owns three things the caller must not have to know: the request
    shape, where the bytes are in the response, and how to satisfy the aspect
    contract. Anything vendor-specific beyond that belongs in `options`, which
    is passed through untouched from the provider's settings entry.
    """
    adapter = _ADAPTERS.get(name)
    if adapter is None:
        raise ImageGenError(
            f"unknown image provider: {name!r} (known: {', '.join(sorted(_ADAPTERS))})"
        )
    return adapter(api_key=api_key, api_base=api_base, model=model, options=options)
