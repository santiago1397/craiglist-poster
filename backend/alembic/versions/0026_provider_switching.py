"""switchable text and image providers

Revision ID: 0026
Revises: 0025
Create Date: 2026-08-03

Both generators were welded to MiniMax. The image side had a provider seam that
was never used — `build_provider("minimax", ...)` with the name written in — and
the text side had none at all, reading `MINIMAX_API_KEY` straight out of the
environment. This makes both a runtime choice.

**Why the config is per-provider rather than one set of fields.** A single
`model` / `api_base` / `cost` triple would have to be retyped on every switch,
and switching back would mean retyping what you overwrote. That is not a switch,
it is a trap: pick OpenAI, forget `api_base`, and the OpenAI payload goes to
`api.minimax.io`.

The argument that settled it was cost. `image_cost_usd` is stamped onto every
`images.cost_usd` row at generation and totalled per key by `key_usage()`, and
that total is the *entire* control on agent image generation — it ships
deliberately uncapped, with visibility as the safeguard. A cost that does not
travel with the provider means the guardrail under-reports by an order of
magnitude the moment you switch, silently and forever. So cost lives inside the
provider entry, next to the model it belongs to.

**The old columns are dropped rather than left in place.** Leaving them would
give the active provider two sources of truth, and the loser of that race is
whichever one a future reader happens to trust. The values are copied into the
`minimax` entry first, so nothing is lost; `downgrade()` copies them back.

**`api_key_enc` is absent from the seeded entries on purpose.** No key is
written here. Resolution falls back to `MINIMAX_API_KEY` in the environment, so
generation keeps working across this deploy with nothing typed in — see decision
7 in DESIGN_PROVIDERS.md, which is also the recovery path if `jwt_secret` is
ever rotated and the stored keys stop decrypting.

**The OpenAI cost is a deliberately high placeholder.** Seeding it low would
under-report agent spend, which is the exact failure this design exists to
prevent; over-reporting is merely annoying. Set it to the real figure for
whichever quality tier you settle on. `options.quality` starts at `medium`
because `low` is visibly artifacty on photorealistic exteriors and covers are
the images anyone actually looks at.

The OpenAI image entry stores `aspect` as 4:3 like every other provider even
though OpenAI cannot produce it. 4:3 is an output contract, honoured by the
adapter cropping 1536x1024 down to 1365x1024 — Craigslist's own largest display
variant is `_1200x900.jpg`, which is 4:3, so anything else gets cropped by the
site instead, taking the composited phone number with it.
"""
from __future__ import annotations

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE generation_settings
            ADD COLUMN IF NOT EXISTS text_provider   TEXT  NOT NULL DEFAULT 'minimax',
            ADD COLUMN IF NOT EXISTS image_provider  TEXT  NOT NULL DEFAULT 'minimax',
            ADD COLUMN IF NOT EXISTS text_providers  JSONB NOT NULL DEFAULT '{}'::jsonb,
            ADD COLUMN IF NOT EXISTS image_providers JSONB NOT NULL DEFAULT '{}'::jsonb;

        -- Carry the live values into the minimax entry, then seed an OpenAI
        -- entry so the form has something to render before anything is typed.
        UPDATE generation_settings SET
            text_providers = jsonb_build_object(
                'minimax', jsonb_build_object(
                    'model',       model,
                    'api_base',    api_base,
                    'temperature', temperature,
                    'options',     '{}'::jsonb
                ),
                'openai', jsonb_build_object(
                    'model',       'gpt-4o-mini',
                    'api_base',    'https://api.openai.com/v1',
                    'temperature', temperature,
                    -- The wire shape, not a shorthand: `options` is merged into
                    -- the request payload untouched.
                    'options',     jsonb_build_object(
                                       'response_format',
                                       jsonb_build_object('type', 'json_object')
                                   )
                )
            ),
            image_providers = jsonb_build_object(
                'minimax', jsonb_build_object(
                    'model',    image_model,
                    'api_base', image_api_base,
                    'aspect',   image_aspect,
                    'cost_usd', image_cost_usd,
                    'options',  '{}'::jsonb
                ),
                'openai', jsonb_build_object(
                    'model',    'gpt-image-1',
                    'api_base', 'https://api.openai.com/v1',
                    'aspect',   '4:3',
                    'cost_usd', 0.08,
                    'options',  jsonb_build_object('quality', 'medium', 'size', '1536x1024')
                )
            ),
            updated_at = NOW()
        WHERE singleton;

        ALTER TABLE generation_settings
            DROP COLUMN IF EXISTS model,
            DROP COLUMN IF EXISTS api_base,
            DROP COLUMN IF EXISTS temperature,
            DROP COLUMN IF EXISTS image_model,
            DROP COLUMN IF EXISTS image_api_base,
            DROP COLUMN IF EXISTS image_aspect,
            DROP COLUMN IF EXISTS image_cost_usd;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE generation_settings
            ADD COLUMN IF NOT EXISTS model          TEXT NOT NULL DEFAULT 'MiniMax-Text-01',
            ADD COLUMN IF NOT EXISTS api_base       TEXT NOT NULL DEFAULT 'https://api.minimax.io/v1',
            ADD COLUMN IF NOT EXISTS temperature    DOUBLE PRECISION NOT NULL DEFAULT 0.9,
            ADD COLUMN IF NOT EXISTS image_model    TEXT NOT NULL DEFAULT 'image-01',
            ADD COLUMN IF NOT EXISTS image_api_base TEXT NOT NULL DEFAULT 'https://api.minimax.io/v1',
            ADD COLUMN IF NOT EXISTS image_aspect   TEXT NOT NULL DEFAULT '4:3',
            ADD COLUMN IF NOT EXISTS image_cost_usd NUMERIC(10,5) NOT NULL DEFAULT 0.0035;

        -- Restore from whichever provider was active, not from 'minimax':
        -- downgrading while OpenAI was selected should leave the single-provider
        -- columns describing what was actually running.
        UPDATE generation_settings SET
            model          = COALESCE(text_providers -> text_provider ->> 'model', model),
            api_base       = COALESCE(text_providers -> text_provider ->> 'api_base', api_base),
            temperature    = COALESCE((text_providers -> text_provider ->> 'temperature')::double precision, temperature),
            image_model    = COALESCE(image_providers -> image_provider ->> 'model', image_model),
            image_api_base = COALESCE(image_providers -> image_provider ->> 'api_base', image_api_base),
            image_aspect   = COALESCE(image_providers -> image_provider ->> 'aspect', image_aspect),
            image_cost_usd = COALESCE((image_providers -> image_provider ->> 'cost_usd')::numeric, image_cost_usd),
            updated_at     = NOW()
        WHERE singleton;

        ALTER TABLE generation_settings
            DROP COLUMN IF EXISTS image_providers,
            DROP COLUMN IF EXISTS text_providers,
            DROP COLUMN IF EXISTS image_provider,
            DROP COLUMN IF EXISTS text_provider;
        """
    )
