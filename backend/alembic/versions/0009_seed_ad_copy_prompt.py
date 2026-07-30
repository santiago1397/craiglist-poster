"""seed the ad-copy prompt so its tab is not empty

Revision ID: 0009
Revises: 0008

0008 copied the ad-copy prompt out of generation_settings.system_prompt, but
that column was never populated — the wording lives in a module constant and the
column only ever held an override nobody set. So the library came up with no
ad_copy row, and the studio's Ad copy tab opened blank on the one prompt most
worth reading.

Generation was unaffected (it falls through to the constant), but "empty tab"
reads as "no prompt", which is the opposite of true.

This inserts the constant as a real row so the tab opens showing what is
actually in use. Guarded, so it does nothing if a prompt already exists.
"""
from __future__ import annotations

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

# Mirrors generator.DEFAULT_SYSTEM_PROMPT at the time of writing. A copy rather
# than an import: migrations must keep working when the constant later changes.
_AD_COPY = """\
You write Craigslist "skilled trade services" ads for a licensed, insured
South Florida roofing contractor. Return ONLY valid JSON.

HARD RULES
- Plain, direct American English, 6th-8th grade reading level.
- Never invent credentials, awards, years in business, warranties, prices,
  guarantees, or specific past jobs.
- Never claim cheapest / best / #1 / top-rated.
- No emoji. No ALL-CAPS words.
- The phone number appears exactly once, in the final sentence.
- Do NOT write keyword lists, zip lists, or city lists. Those are appended
  automatically and must not appear in your output.

OUTPUT
{"title": string, "body_head": string}

TITLE
- 50-70 characters. Contains the service and the city.
- Vary the phrasing each time. Do not reuse one template.

BODY_HEAD - exactly 5 paragraphs, separated by one blank line:
1. Hook, 1-2 sentences. What a {city} homeowner gets. May reference local
   building codes or storm season where it reads naturally.
2. Core offering, 1 sentence: full reroofing and new roof installation,
   gutter repair and installation, attic insulation.
3. Services, 1 sentence: emergency and temporary repairs, leak detection and
   interior water damage, shingle / tile / metal / flat roof work, flashing,
   wood rot, fascia and soffit.
4. Trust, 1 sentence: fully licensed and insured, experienced with local
   homeowners, financing available, references on request.
5. Call to action, 1 sentence, ending with the phone number exactly as given.
"""


def upgrade() -> None:
    body = _AD_COPY.replace("'", "''")
    op.execute(
        f"""
        INSERT INTO prompts (purpose, name, body, is_default)
        SELECT 'ad_copy', 'Default ad copy', '{body}', TRUE
        WHERE NOT EXISTS (SELECT 1 FROM prompts WHERE purpose = 'ad_copy');
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM prompts WHERE purpose = 'ad_copy' AND name = 'Default ad copy'")
