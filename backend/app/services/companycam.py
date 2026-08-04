"""CompanyCam API transport. No database, no Pillow, no policy.

Everything here is about getting bytes off `api.companycam.com` without being
rate-limited or handed an expired URL. What those bytes become is
`image_import.py`'s problem, and which of them we want is the importer's.

Two properties are load-bearing and easy to lose in a refactor:

**`list_photos` is a generator.** The caller downloads each page's photos before
asking for the next one. `uris[].url` are presigned CDN links with a TTL, so
collecting all thirty pages of a 3,000-photo account and *then* downloading
means the tail of the run fetches against signatures that expired while the
earlier pages were being pulled. Materialising this into a list reintroduces
that bug silently — nothing fails until the account is big enough.

**Cursor paging first, `page=` only as a fallback.** Offset paging over a
collection that is being appended to by crews in the field skips and repeats
rows. CompanyCam hands back `X-Next-Cursor`; when it does, we use it.
"""
from __future__ import annotations

import random
import time
from collections.abc import Iterator

import httpx
from loguru import logger

# Their documented ceiling is 240 GET/minute. Pace under it rather than
# discovering the limit — a 429 mid-import costs a retry and the backoff, and
# the import is not in a hurry.
_MAX_RPM = 180
_MIN_INTERVAL = 60.0 / _MAX_RPM

# Listing is small JSON; downloads are multi-MB off a CDN and get their own,
# longer budget.
LIST_TIMEOUT = 30.0
DOWNLOAD_TIMEOUT = 120.0

MAX_ATTEMPTS = 5
PER_PAGE = 100  # their documented maximum

# A phone photo is a few MB. Anything an order of magnitude past that is not a
# photo, and streaming it into memory to find out is how a 512MB container dies.
MAX_DOWNLOAD_BYTES = 40 * 1024 * 1024


class CompanyCamError(RuntimeError):
    """The API could not be read. Never raised for an individual bad photo."""


class _Pacer:
    """Spaces calls to the API. Downloads hit the CDN and are not paced."""

    def __init__(self) -> None:
        self._last = 0.0

    def wait(self) -> None:
        gap = time.monotonic() - self._last
        if gap < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - gap)
        self._last = time.monotonic()


def _retry_after(response: httpx.Response, attempt: int) -> float:
    """Prefer the server's own answer; fall back to backoff with jitter.

    The jitter is not decoration — a fixed backoff means a retried burst
    re-collides at exactly the same moment it did the first time.
    """
    raw = response.headers.get("Retry-After")
    if raw:
        try:
            return min(60.0, max(1.0, float(raw)))
        except ValueError:
            pass
    return min(60.0, (2.0**attempt) + random.random())


def _get(
    client: httpx.Client,
    url: str,
    *,
    token: str,
    pacer: _Pacer,
    params: dict | None = None,
) -> httpx.Response:
    """One GET against the API, retrying 429 and 5xx."""
    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        pacer.wait()
        try:
            response = client.get(
                url,
                params=params,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
                timeout=LIST_TIMEOUT,
            )
        except httpx.HTTPError as e:  # connect/read/timeout
            last = e
            time.sleep(min(30.0, 2.0**attempt))
            continue

        if response.status_code == 429 or response.status_code >= 500:
            delay = _retry_after(response, attempt)
            logger.warning(
                f"companycam {response.status_code} on {url}; "
                f"retrying in {delay:.1f}s ({attempt + 1}/{MAX_ATTEMPTS})"
            )
            last = CompanyCamError(f"HTTP {response.status_code}")
            time.sleep(delay)
            continue

        if response.status_code == 401:
            raise CompanyCamError(
                "CompanyCam rejected the token (401). Check COMPANYCAM_API_TOKEN "
                "or pass --token."
            )
        if response.status_code >= 400:
            raise CompanyCamError(
                f"HTTP {response.status_code} from {url}: {response.text[:300]}"
            )
        return response

    raise CompanyCamError(f"gave up on {url} after {MAX_ATTEMPTS} attempts: {last}")


def list_photos(
    client: httpx.Client,
    *,
    token: str,
    api_base: str,
    filters: dict | None = None,
    per_page: int = PER_PAGE,
    pacer: _Pacer | None = None,
) -> Iterator[dict]:
    """Yield photo objects, paging until the API runs dry.

    A generator on purpose — see the module docstring. Consume it lazily.

    **The first request carries no pagination parameter at all.** The published
    reference lists `page` alongside the cursor params, but the live API rejects
    it outright:

        HTTP 400 {"errors":["Invalid cursor format - please use cursor from the
        headers of a previous response as a 'before' or 'after' param"]}

    So pagination is cursor-only in practice: ask for the first page bare, read
    `X-Next-Cursor` off the response, pass it back as `after`. Offset paging
    survives only as a fallback for the case where a full page comes back with
    no cursor header, and a 400 on that fallback ends the walk with a warning
    rather than throwing away the photos already yielded.
    """
    pacer = pacer or _Pacer()
    url = f"{api_base.rstrip('/')}/photos"
    params: dict = {"per_page": per_page, **(filters or {})}
    cursor: str | None = None
    page: int | None = None  # stays None unless we fall back to offset paging

    while True:
        query = dict(params)
        if cursor:
            query["after"] = cursor
        elif page is not None:
            query["page"] = page

        try:
            response = _get(client, url, token=token, pacer=pacer, params=query)
        except CompanyCamError:
            # The bare first request failing is a real error worth surfacing.
            # The offset fallback failing just means this API is cursor-only,
            # which is the documented-vs-actual mismatch above.
            if page is None:
                raise
            logger.warning(
                "companycam refused offset paging; stopping after the pages "
                "already read. Some photos may not have been seen."
            )
            return

        try:
            batch = response.json()
        except ValueError as e:
            raise CompanyCamError(f"non-JSON response from {url}: {e}") from e
        if not isinstance(batch, list):
            raise CompanyCamError(
                f"expected a JSON array of photos, got {type(batch).__name__}"
            )
        if not batch:
            return

        yield from batch

        # A short page means the end regardless of what the cursor says.
        if len(batch) < per_page:
            return

        cursor = response.headers.get("X-Next-Cursor") or None
        if not cursor:
            page = 2 if page is None else page + 1
            if page > 1000:  # runaway guard; 100k photos
                logger.warning("companycam paging hit the 1000-page guard")
                return


def pick_uri(photo: dict, preferred: str = "original") -> tuple[str, str] | None:
    """Return `(type, url)` for the best available variant, or None.

    `uris` is a list of `{type, url}` with types original | web | thumbnail.
    Falling back to `web` matters: a photo still processing, or one uploaded
    through an integration, does not always carry every variant.
    """
    uris = photo.get("uris")
    if not isinstance(uris, list):
        return None
    by_type = {
        u.get("type"): u.get("url")
        for u in uris
        if isinstance(u, dict) and u.get("url")
    }
    for want in (preferred, "original", "web"):
        if by_type.get(want):
            return want, by_type[want]
    return None


def download(
    client: httpx.Client, url: str, *, max_bytes: int = MAX_DOWNLOAD_BYTES
) -> bytes:
    """Fetch one image, streaming so an oversized file is abandoned early.

    No auth header: these are presigned CDN links, and sending a bearer token to
    a third-party asset host is a good way to leak one. No pacing either — the
    CDN is not the API and does not share its budget.
    """
    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            with client.stream("GET", url, timeout=DOWNLOAD_TIMEOUT) as response:
                if response.status_code == 429 or response.status_code >= 500:
                    delay = _retry_after(response, attempt)
                    last = CompanyCamError(f"HTTP {response.status_code}")
                    time.sleep(delay)
                    continue
                if response.status_code >= 400:
                    # 403 here is usually an expired presigned URL, which means
                    # the caller listed pages up front instead of downloading as
                    # it went. Say so — the symptom is otherwise baffling.
                    raise CompanyCamError(
                        f"HTTP {response.status_code} downloading {url[:120]}"
                        + (
                            " (expired presigned URL? download inside the page loop)"
                            if response.status_code == 403
                            else ""
                        )
                    )
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise CompanyCamError(
                            f"image exceeds {max_bytes // (1024 * 1024)}MB; skipped"
                        )
                    chunks.append(chunk)
                if not total:
                    raise CompanyCamError("downloaded zero bytes")
                return b"".join(chunks)
        except httpx.HTTPError as e:
            last = e
            time.sleep(min(30.0, 2.0**attempt))

    raise CompanyCamError(f"download failed after {MAX_ATTEMPTS} attempts: {last}")


def count_photos(
    client: httpx.Client, *, token: str, api_base: str, filters: dict | None = None
) -> int:
    """How many photos match, walking pages but downloading no image bytes.

    There is no documented count endpoint, so this is a paged walk of the JSON —
    cheap (30 requests for 3,000 photos) and worth it before committing a
    multi-gigabyte import.
    """
    return sum(
        1
        for _ in list_photos(
            client, token=token, api_base=api_base, filters=filters
        )
    )
