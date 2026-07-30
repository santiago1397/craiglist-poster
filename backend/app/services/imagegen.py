"""Image generation providers.

One narrow interface — `generate(prompt, aspect, n) -> list[bytes]` — with
MiniMax as the first implementation (decision 18). Moving to Gemini or anything
else means adding an adapter here and changing a setting; nothing else in the
codebase knows which provider produced an image.
"""
from __future__ import annotations

import base64
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


class MiniMaxImages:
    """MiniMax image-01.

    Two quirks worth knowing: it answers HTTP 200 even for logical failures,
    putting the real result in `base_resp.status_code`, and it returns images as
    bare base64 rather than data URLs.
    """

    name = "minimax"

    def __init__(self, api_key: str, api_base: str, model: str) -> None:
        if not api_key:
            raise ImageGenError("MINIMAX_API_KEY is not configured")
        self.api_key = api_key
        self.api_base = api_base.rstrip("/")
        self.model = model

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

        out: list[bytes] = []
        for item in encoded:
            # Tolerate a data: URL prefix in case the format ever changes.
            if isinstance(item, str) and item.startswith("data:"):
                item = item.split(",", 1)[-1]
            try:
                out.append(base64.b64decode(item))
            except Exception as e:
                raise ImageGenError(f"could not decode returned image: {e}") from e

        logger.info(f"generated {len(out)} image(s) via {self.name}/{self.model}")
        return out


def build_provider(name: str, *, api_key: str, api_base: str, model: str) -> ImageProvider:
    if name == "minimax":
        return MiniMaxImages(api_key=api_key, api_base=api_base, model=model)
    raise ImageGenError(f"unknown image provider: {name!r}")
