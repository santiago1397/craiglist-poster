# Switching the model providers

The text generator and the image generator each became a choice rather than a
constant. This records what was decided and, where it matters, what was rejected
and why — the rejected options are the ones that come back as suggestions later.

---

## What actually changed

Before, both generators were welded to MiniMax. `generator.call_model()` read
`get_settings().minimax_api_key` directly, and `images.generate_images()` called
`build_provider("minimax", ...)` with the name hardcoded. The provider seam
existed for images and was never used; for text it did not exist at all.

Now `generation_settings` carries `text_provider` and `image_provider`, plus a
JSONB blob per generator holding one config entry per known provider. Selecting
a provider is a dropdown; its model, endpoint, cost and API key travel with it.

---

## Decisions

### 1. Keys live in the database, encrypted

Rejected: environment variables. They would have meant a container restart to
add a provider, and this had to be a runtime switch.

The cost is that this database now holds a secret that can be read back.
Everything else in it — machine tokens, the admin password, agent API keys — is
an argon2 hash, because nothing ever needs the plaintext. A provider key is
different: it goes upstream verbatim on every call.

### 2. The encryption key is derived from `jwt_secret`

Rejected: a dedicated `SECRETS_KEY`. Deliberately coupled instead, accepting
that rotating the JWT secret invalidates every stored provider key.

This is survivable only because of decision 7. Recovery is re-pasting two API
keys, or falling back to the environment — not data loss.

**`jwt_secret` cannot be handed to Fernet directly.** Fernet wants 32
urlsafe-base64-encoded bytes; `jwt_secret` is an arbitrary string of length ≥32
and Fernet raises on it. `services/secrets.py` bridges the two with a sha256.
Removing that step breaks decryption for every key already stored.

**Failure is at use, not at boot.** A dashboard that refuses to start because one
provider key will not decrypt would convert a cosmetic problem into a total
outage — posting, the queue, Diagnostics and the `/agent` API do not need
generation to work, and draft generation already falls back to workbook copy by
design. So a `DecryptionError` surfaces as `configured: false` with an
explanation, and generation degrades exactly as it does for any other bad key.

### 3. Config is per-provider, not a single set of fields

Rejected: one `model` / `api_base` / `cost` triple that you retype on each
switch. That is not a switch, it is a trap — select OpenAI, forget `api_base`,
and you POST OpenAI's payload at `api.minimax.io`.

The argument that settled it was **cost**. `image_cost_usd` is stamped onto every
`images.cost_usd` row at generation time and totalled per key by `key_usage()`.
That total is the *entire* control on agent image generation, which ships
deliberately uncapped with visibility as the safeguard. A cost that does not
travel with the provider means the guardrail silently under-reports by an order
of magnitude the moment you switch. Cost lives *inside* the provider config.

### 4. Aspect is normalized; everything else provider-specific is opaque

`aspect` stays a shared concept because it is genuine shared intent — you always
want a landscape roof photo, whoever draws it. Levers that do not generalize
(OpenAI's `quality`, JSON mode for text) live in an `options` blob passed
through untouched.

Rejected: pure normalization, which cannot express `quality` — the dominant cost
dial, and therefore the one thing decision 3 says has to be right.

`n` was already vestigial: `generate_images()` has always looped one image at a
time and called `provider.generate(..., n=1)`. Nothing depends on batch support,
which is why a provider without it costs nothing.

### 5. 4:3 is an output contract, not a request parameter

**OpenAI's image model does not offer 4:3.** Its sizes are 1024×1024, 1536×1024,
1024×1536. The nearest landscape is 3:2.

4:3 is not a preference. Craigslist's own largest display variant is
`_1200x900.jpg` — which `images.py` already hardcodes as `_LARGEST_VARIANT` —
and 1200×900 is exactly 4:3. Hand the site 3:2 and it crops or letterboxes,
including the CTA band Pillow composited onto the bottom third of the cover.

So the OpenAI adapter requests 1536×1024 and **center-crops to 1365×1024**
before returning. The adapter's contract is "return 4:3 bytes"; how it gets
there is its own business. A future Gemini adapter satisfies the same contract
by whatever route it needs, and no caller learns a third behaviour.

**The crop happens before `_store`.** Storage is content-addressed, so the
sha256 must be of the cropped bytes. Cropping after storing yields rows whose
digest does not match their file.

### 6. Text gets config, not adapters

`call_model()` already POSTs to `api_base + "/chat/completions"` with a bearer
token and reads `choices[0].message.content`. That is the OpenAI wire format;
MiniMax merely speaks it. So switching text providers needed **no adapter
written at all**.

One `openai_compatible` adapter covers MiniMax, OpenAI, DeepSeek, Groq, Together
and Gemini's compatibility endpoint. Rejected: two byte-identical classes named
`MiniMaxText` and `OpenAIText`, kept in sync forever for no benefit.

Worth knowing: `_salvage()` exists because MiniMax returns literal control
characters inside JSON strings, which caused roughly a third of generations to
fall back. OpenAI's `response_format: {"type": "json_object"}` makes that class
of failure disappear, and lives in `options`. `_extract_json` / `_salvage` stay
as the universal net for providers that ignore it.

### 7. The environment remains a permanent fallback

Resolution order is: stored key → `{provider}_api_key` from the environment →
not configured. Nothing is ever written back.

Rejected: adopting the env var into the database on first read. That gives a GET
a write path, and worse, the adopted row then *shadows* the env var — so if
`jwt_secret` later rotates and the row will not decrypt, a perfectly good
environment variable sits there being ignored.

**This is the disaster recovery for decision 2.** Rotating the JWT secret bricks
the stored keys; with an env fallback the fix is to clear the stored value and
carry on, and it works even when the dashboard login is what you were rotating
the secret to repair.

### 8. One image provider for both stacks

Covers (~8/day) and photos (~166/day) could take different providers — photos are
95% of image volume, so the photo provider *is* the bill. Deferred to a later
session, and safe to defer because `image_topup_enabled` ships false: photos are
only generated when someone presses Generate.

The storage shape is per-provider, so splitting cover and photo later needs no
migration. One control ships; the single-provider assumption is not baked in
below the settings layer.

### 9. Ciphertext never leaves the service layer

`get_generation_settings()` returns config with every key replaced by
`{configured, hint}`. Plaintext is reachable only through
`active_provider_config()`, called by `call_model()` and `generate_images()`.

This matters because `GET /settings/generation` returns the settings row
wholesale. A key field added to that row serializes straight to the browser —
useless without `jwt_secret`, but it lands in devtools, in any exported HAR, and
in the response cache. Same category as a key in a query string, which this
project already refuses to make.

`tests/test_provider_keys.py` asserts the response body contains neither the
ciphertext nor the plaintext. That test, not this paragraph, is what holds the
line.

### 10. A provider with no key is refused at save time

`PUT /settings/generation` returns 422 if the resulting config has no resolvable
key — validated against the **merged** state, exactly as `photos_min` /
`photos_max` already are, so a key supplied in the same request counts.

The deciding argument is text, not images. A bad image provider announces itself
within seconds: you press Generate and get zero images and an error. **A bad text
provider is silent by design** — `build_draft()` catches `GenerationError` and
falls back to workbook copy so the queue keeps filling. Drafts look normal,
Review looks healthy, ads publish, and the only trace is `last_source` on a row
nobody watches. Weeks of stale repeated copy is the documented cause of ghosting.

### 11. Prompts are not coupled to providers

The prompt library already holds many named prompts per purpose with a partial
unique index enforcing one default. Keep `cover — minimax` and `cover — openai`
side by side and flip `is_default` by hand.

Rejected: provider-tagged prompts. It automates a two-click action performed
maybe twice a year, and the failure mode is self-announcing — you look at the
images. `get_default_body()` is a single chokepoint if that ever changes.

The prompt studio already renders against the *active* provider, at real cost,
into `status='test'` — invisible to every picker and purged after two hours. That
is the evaluation path, and it needed no new code.

### 12. Nano banana is not built

The seam accommodates it: adapter dispatch, 4:3-as-output-contract, per-provider
config with its own cost. The adapter is not written.

Gemini's response shape (inline data parts on candidates) and its aspect handling
both want a live key and real responses to get right. Written blind, it would
look finished and fail on first use, in a path that spends money. It is about an
hour's work against a seam already shaped for it.

---

## Where this bites

**Verify the OpenAI cost before you leave it running.** The migration seeds
`image_providers.openai.cost_usd` with a deliberately conservative placeholder,
because seeding it low would silently under-report agent spend — the exact
failure decision 3 exists to prevent. Over-reporting is annoying; under-reporting
is the one that costs money quietly. Set it to the real figure for the quality
tier you settle on.

**`get_settings()` is `@lru_cache`d.** The environment fallbacks in decision 7 do
not refresh without a container restart. That is fine — they are the break-glass
path, not the routine one — but do not expect an edited `.env.prod` to take
effect on its own.

**Restoring the database elsewhere carries ciphertext, not the key.** `pg_dump`
holds the encrypted provider keys; `jwt_secret` lives in `.env.prod`. A restore
onto a fresh host without carrying that secret across gives you a dashboard that
looks perfectly healthy and quietly generates nothing but fallback copy. The env
fallback covers you if the env var is set on the new host.
