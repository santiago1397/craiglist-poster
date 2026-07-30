"""prompt library: named prompts per purpose, one default each

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-30

Prompts lived in three places: two columns on generation_settings for ad copy,
one for the keyword tail, and a module constant for images that nothing could
write to. This gives them one home.

`purpose` separates cover images from photos deliberately. A cover has the phone
number composited across its lower third, so it wants an uncluttered bottom of
frame; a photo has nothing drawn on it and can be as busy as it likes. One
prompt cannot serve both without compromising one of them.

Exactly one default per purpose, enforced by a partial unique index rather than
application code — two defaults would make generation silently non-deterministic.

No version history (a deliberate choice): images already record the prompt text
that produced them in images.prompt, which answers "why does this one look
good?" without a history UI to maintain.
"""
from __future__ import annotations

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

# Kept in sync with images.DEFAULT_IMAGE_PROMPT / generator.DEFAULT_SYSTEM_PROMPT
# so a fresh install has something sensible to edit rather than a blank box.
_PHOTO = (
    "Professional photograph of a well-maintained {kind} on a single-family "
    "home in {city}, South Florida. Bright natural daylight, clear blue sky, "
    "palm trees, realistic residential architecture. Sharp focus, no text, "
    "no watermarks, no people."
)
_COVER = (
    "Professional wide photograph of a well-maintained {kind} on a "
    "single-family home in {city}, South Florida. Bright natural daylight, "
    "clear blue sky. Compose with the roof in the upper two thirds and keep "
    "the lower third of the frame simple and uncluttered - open sky, plain "
    "wall or lawn - leaving clear space for a caption. Sharp focus, no text, "
    "no watermarks, no people."
)


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE prompts (
            id          BIGSERIAL PRIMARY KEY,
            purpose     TEXT NOT NULL,
            name        TEXT NOT NULL,
            body        TEXT NOT NULL,
            is_default  BOOLEAN NOT NULL DEFAULT FALSE,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT prompts_purpose_check CHECK (
                purpose IN ('cover_image','photo_image','ad_copy','keyword_tail')
            )
        );
        CREATE INDEX idx_prompts_purpose ON prompts(purpose, name);
        -- One default per purpose. Enforced here rather than in application
        -- code, because two defaults would make generation silently pick
        -- whichever row came back first.
        CREATE UNIQUE INDEX idx_prompts_one_default ON prompts(purpose)
            WHERE is_default;
        """
    )

    op.execute(
        """
        ALTER TABLE generation_settings
            ADD COLUMN image_kinds JSONB NOT NULL DEFAULT
                '["metal roof","tile roof","shingle roof","flat roof",
                  "newly replaced roof","clay tile roof"]'::jsonb;
        """
    )

    # Test renders are not part of the stack until you keep one, so they need a
    # status the pickers ignore.
    op.execute(
        """
        ALTER TABLE images DROP CONSTRAINT IF EXISTS images_status_check;
        ALTER TABLE images ADD CONSTRAINT images_status_check
            CHECK (status IN ('pending','approved','rejected','test'));
        """
    )

    # op.execute takes no bind parameters — its second argument is execution
    # options — so the literals are escaped and inlined.
    def q(s: str) -> str:
        return "'" + s.replace("'", "''") + "'"

    op.execute(
        f"""
        INSERT INTO prompts (purpose, name, body, is_default) VALUES
          ('photo_image', 'Default photo', {q(_PHOTO)}, TRUE),
          ('cover_image', 'Default cover', {q(_COVER)}, TRUE);
        """
    )
    # Carry the ad-copy prompts across from generation_settings so nothing is
    # lost and the library opens already populated.
    op.execute(
        """
        INSERT INTO prompts (purpose, name, body, is_default)
        SELECT 'ad_copy', 'Default ad copy', system_prompt, TRUE
        FROM generation_settings WHERE COALESCE(system_prompt,'') <> '';
        INSERT INTO prompts (purpose, name, body, is_default)
        SELECT 'keyword_tail', 'Keyword tail', tail_template, TRUE
        FROM generation_settings WHERE COALESCE(tail_template,'') <> '';
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE images DROP CONSTRAINT IF EXISTS images_status_check;
        ALTER TABLE images ADD CONSTRAINT images_status_check
            CHECK (status IN ('pending','approved','rejected'));
        """
    )
    op.execute("ALTER TABLE generation_settings DROP COLUMN IF EXISTS image_kinds")
    op.execute("DROP TABLE IF EXISTS prompts")
