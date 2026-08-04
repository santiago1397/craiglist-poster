"""Provider keys must never leave the server, and the crop must actually crop.

Two invariants that prose cannot enforce.

The first is that `GET /settings/generation` returns the generation settings row
wholesale, and the provider entries now live in that row. A key field added
there — encrypted or not — serializes straight to the browser. The ciphertext is
useless without JWT_SECRET, but it lands in devtools, in any exported HAR and in
the response cache, which is the same category of mistake as a key in a query
string. `providers.redacted()` is what prevents it; this is what notices when
someone stops calling it.

The second is that 4:3 is an output contract rather than a request parameter.
OpenAI cannot produce 4:3 at all, so the adapter crops — and Craigslist's own
largest display variant is 1200x900, so an image at any other ratio gets cropped
by the site instead, taking the composited phone number off the cover with it.
"""
import io
import json

from PIL import Image

from app.db import conn, init_pool, tx
from app.services import generator, imagegen, providers as providers_svc
from app.services import images as images_svc
from app.services import secrets as secrets_svc

init_pool()
ok = []

SENTINEL = "sk-test-DO-NOT-LEAK-abcdefghijklmnop"


def jpeg(size):
    buf = io.BytesIO()
    Image.new("RGB", size, (120, 130, 140)).save(buf, format="JPEG")
    return buf.getvalue()


# --- the secrets round trip ------------------------------------------------
token = secrets_svc.encrypt(SENTINEL)
assert token != SENTINEL, "encrypt returned the plaintext"
assert secrets_svc.decrypt(token) == SENTINEL
ok.append("a stored key round-trips through encrypt/decrypt")

# A key written under a different JWT_SECRET must fail loudly rather than
# returning rubbish. This is the failure the environment fallback exists for.
try:
    secrets_svc.decrypt("gAAAAABnot-a-real-fernet-token")
    raise AssertionError("decrypt accepted a token it could not have written")
except secrets_svc.DecryptionError:
    pass
ok.append("an undecryptable key raises DecryptionError rather than returning junk")

# --- a stored key never reaches the API response ---------------------------
with tx() as c:
    generator.update_provider_entry(
        c, kind="text", provider="minimax", patch={"api_key": SENTINEL}
    )

with conn() as c:
    stored = c.execute(
        "SELECT text_providers -> 'minimax' ->> 'api_key_enc' AS enc "
        "FROM generation_settings"
    ).fetchone()["enc"]
assert stored, "the key was not stored at all"
assert stored != SENTINEL, "the key was stored in plaintext"

with conn() as c:
    payload = json.dumps(generator.get_generation_settings(c), default=str)
assert SENTINEL not in payload, "the plaintext key is in the settings payload"
assert stored not in payload, "the ciphertext is in the settings payload"
ok.append("neither the key nor its ciphertext appears in the settings payload")

# The UI still has to be able to say *which* key is stored, or rotating one is
# guesswork. A fingerprint is not the key.
with conn() as c:
    redacted = providers_svc.redacted(
        c.execute("SELECT * FROM generation_settings").fetchone(), kind="text"
    )
entry = redacted["minimax"]
assert "api_key_enc" not in entry, entry
assert entry["key"]["configured"] is True
assert entry["key"]["hint"] and SENTINEL not in entry["key"]["hint"]
assert entry["key"]["source"] == "stored"
ok.append("the redacted entry reports a stored key by fingerprint, not by value")

# --- active_config is the only route to plaintext --------------------------
with conn() as c:
    cfg = providers_svc.active_config(c, kind="text")
assert cfg["api_key"] == SENTINEL, "active_config could not resolve the stored key"
assert "api_key_enc" not in cfg
ok.append("active_config resolves the plaintext, and carries no ciphertext")

# --- clearing is explicit; omitting is not ---------------------------------
with tx() as c:
    generator.update_provider_entry(
        c, kind="text", provider="minimax", patch={"model": "MiniMax-Text-01"}
    )
with conn() as c:
    assert providers_svc.active_config(c, kind="text")["api_key"] == SENTINEL, \
        "a patch without api_key cleared the stored key"
with tx() as c:
    generator.update_provider_entry(
        c, kind="text", provider="minimax", patch={"api_key": ""}
    )
with conn() as c:
    after = providers_svc.active_config(c, kind="text")
assert after["api_key"] == providers_svc.env_key("minimax"), \
    "clearing did not fall through to the environment"
ok.append("omitting api_key keeps the stored one; an empty string clears it")

# --- one provider's config cannot clobber another's ------------------------
with tx() as c:
    generator.update_provider_entry(
        c, kind="image", provider="openai", patch={"api_key": SENTINEL, "cost_usd": 0.04}
    )
    generator.update_provider_entry(
        c, kind="image", provider="minimax", patch={"model": "image-01"}
    )
with conn() as c:
    blob = c.execute("SELECT image_providers FROM generation_settings").fetchone()["image_providers"]
assert blob["openai"].get("api_key_enc"), "writing minimax dropped openai's key"
assert blob["openai"]["cost_usd"] == 0.04
assert blob["minimax"]["model"] == "image-01"
ok.append("a provider write merges rather than replacing the whole blob")

# --- cost travels with the provider ----------------------------------------
# `key_usage()` totals images.cost_usd per API key, and that total is the only
# control on deliberately-uncapped agent generation. A cost that did not move
# with the provider would under-report by an order of magnitude after a switch.
with conn() as c:
    mini = providers_svc.active_config(c, kind="image")
assert mini["provider"] == "minimax" and float(mini["cost_usd"]) == 0.0035, mini
with tx() as c:
    c.execute("UPDATE generation_settings SET image_provider = 'openai' WHERE singleton")
with conn() as c:
    oai = providers_svc.active_config(c, kind="image")
assert float(oai["cost_usd"]) == 0.04, oai
with tx() as c:
    c.execute("UPDATE generation_settings SET image_provider = 'minimax' WHERE singleton")
ok.append("switching the image provider switches the per-image cost with it")

# --- the aspect contract ---------------------------------------------------
assert abs(imagegen.parse_aspect("4:3") - 4 / 3) < 1e-9
assert abs(imagegen.parse_aspect("nonsense") - 4 / 3) < 1e-9, "a bad aspect must not raise"

# What OpenAI actually returns for a landscape request, and what it has to
# become. 1536x1024 is 3:2; 4:3 at that height is 1365 wide.
cropped = imagegen.crop_to_aspect(jpeg((1536, 1024)), "4:3")
with Image.open(io.BytesIO(cropped)) as im:
    assert im.size == (1365, 1024), im.size
ok.append("a 3:2 render is cropped to 4:3 at full height (1536x1024 -> 1365x1024)")

# Already-correct bytes must come back untouched: re-encoding would change the
# digest, and storage is content-addressed.
already = jpeg((1200, 900))
assert imagegen.crop_to_aspect(already, "4:3") is already, \
    "a 4:3 image was re-encoded, which would change its sha256"
ok.append("an image already at 4:3 is returned byte-identical")

# Portrait sources crop on the other axis rather than failing.
tall = imagegen.crop_to_aspect(jpeg((1024, 1536)), "4:3")
with Image.open(io.BytesIO(tall)) as im:
    assert im.size == (1024, 768), im.size
ok.append("a portrait render crops on the other axis")

# A crop that cannot be performed must not lose an image already paid for.
assert imagegen.crop_to_aspect(b"not an image at all", "4:3") == b"not an image at all"
ok.append("an uncroppable image is kept rather than discarded")

# --- unknown providers are refused, not silently attempted -----------------
try:
    imagegen.build_provider("nano-banana", api_key="x", api_base="y", model="z")
    raise AssertionError("an unknown provider was built")
except imagegen.ImageGenError as e:
    assert "nano-banana" in str(e) and "minimax" in str(e), str(e)
ok.append("an unknown image provider is refused with the known list in the message")

# --- generation without a key names the variable that fixes it -------------
# "no API key" and "bad endpoint" read identically otherwise, and this is the
# one people actually hit.
with tx() as c:
    generator.update_provider_entry(
        c, kind="image", provider="minimax", patch={"api_key": ""}
    )
    res = images_svc.generate_images(c, count=1)
assert res["created"] == 0 and res["cost_usd"] == 0.0, res
assert "MINIMAX_API_KEY" in (res["error"] or ""), res["error"]
ok.append("generating without a key names the environment variable that fixes it")

# --- the endpoint refuses a provider it cannot reach -----------------------
from fastapi.testclient import TestClient  # noqa: E402

from app.auth import require_admin  # noqa: E402
from app.main import app  # noqa: E402

app.dependency_overrides[require_admin] = lambda: "admin@test"
client = TestClient(app, raise_server_exceptions=False)

r = client.put("/settings/generation", json={"text_provider": "openai"})
assert r.status_code == 422, (r.status_code, r.text)
assert "OPENAI_API_KEY" in r.json()["detail"], r.json()
ok.append("selecting a provider with no key is refused, naming the variable")

with conn() as c:
    assert c.execute(
        "SELECT text_provider FROM generation_settings"
    ).fetchone()["text_provider"] == "minimax", "the refused switch was still written"
ok.append("a refused switch leaves the previous provider in place")

# The same switch succeeds when the key travels with it — the merged-state
# check is what makes "configure and switch in one save" work without opening a
# hole. Mirrors how photos_min/photos_max have always been validated.
r = client.put(
    "/settings/generation",
    json={"text_provider": "openai", "text_provider_config": {"api_key": SENTINEL}},
)
assert r.status_code == 200, (r.status_code, r.text)
assert r.json()["text_provider"] == "openai"
ok.append("a key supplied in the same request satisfies the check")

body = r.text
assert SENTINEL not in body, "the endpoint echoed the key back"
ok.append("the PUT response does not echo the key it just stored")

# An unrelated save must not be held hostage by a provider it did not touch.
with tx() as c:
    generator.update_provider_entry(
        c, kind="image", provider="minimax", patch={"api_key": ""}
    )
    c.execute("UPDATE generation_settings SET image_provider = 'minimax' WHERE singleton")
r = client.put("/settings/generation", json={"photos_min": 3})
assert r.status_code == 200, (r.status_code, r.text)
ok.append("saving an unrelated field is not blocked by an unconfigured provider")

r = client.put("/settings/generation", json={"image_provider": "nano-banana"})
assert r.status_code == 422 and "nano-banana" in r.json()["detail"], r.text
ok.append("an unknown provider name is refused before anything is written")

# Restore, so a re-run starts where this one did.
with tx() as c:
    c.execute("UPDATE generation_settings SET text_provider = 'minimax' WHERE singleton")
    generator.update_provider_entry(
        c, kind="text", provider="openai", patch={"api_key": ""}
    )

print("\n".join(f"  OK  {line}" for line in ok))
print(f"\n{len(ok)} checks passed")
