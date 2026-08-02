"""agent scope: let one key compose a post, and record which key did

Revision ID: 0024
Revises: 0023
Create Date: 2026-08-02

0018 shipped two scopes. `read` answers questions; `post` publishes a draft a
human already approved. Between them there was no way for an agent to *build*
anything — generating an image, writing a draft, attaching a cover and topping
up photos all sat behind the admin cookie. The operator's actual request ("here
is a key, make me a post") was impossible, not because the machinery was
missing but because no credential reached it.

`agent` is that scope: read + compose + publish in one key.

**It is header-only on every verb, reads included** (enforced in `security.py`).
0018 allowed `?key=` for reads and justified the leak explicitly — a read key in
an access log means "rotate it, nothing was at risk". That reasoning does not
survive a key that can also publish: the same leak would hand over a live
posting slot on a real classifieds listing. So the concession stays exactly
where it was earned and nowhere else. An agent that genuinely cannot set headers
gets a `read` key, which is unchanged.

The two `created_by_key_id` columns are not bookkeeping. They are the mechanism
behind the one privilege an agent is granted over the image stack: it may
approve an image **it generated itself**, and nothing else. Without a column
recording who made a row, "its own" is not a question the server can answer, and
the alternative — trusting the caller not to approve someone else's — is not an
access control. NULL means a human made it in the dashboard, which is every row
that exists today.

Attribution on `drafts` earns its keep separately: it is what lets Review show
that a machine wrote this and nobody has read it yet. The publishing gate is
still `reviewed`, which no agent route may write.
"""
from __future__ import annotations

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE api_keys DROP CONSTRAINT IF EXISTS api_keys_scope_check;
        ALTER TABLE api_keys ADD CONSTRAINT api_keys_scope_check
            CHECK (scope IN ('read', 'post', 'agent'));

        -- ON DELETE SET NULL, not CASCADE: revoking or deleting a key must
        -- never delete the images it generated or the drafts it wrote. Those
        -- are real assets that cost money and may already be published; losing
        -- the attribution is an acceptable cost of tidying up a key, losing
        -- the row is not.
        ALTER TABLE images
            ADD COLUMN IF NOT EXISTS created_by_key_id BIGINT
                REFERENCES api_keys(id) ON DELETE SET NULL;

        ALTER TABLE drafts
            ADD COLUMN IF NOT EXISTS created_by_key_id BIGINT
                REFERENCES api_keys(id) ON DELETE SET NULL;

        -- Partial: the overwhelming majority of rows are human-made and NULL,
        -- and every query against these columns asks about the minority.
        CREATE INDEX IF NOT EXISTS idx_images_created_by_key
            ON images (created_by_key_id) WHERE created_by_key_id IS NOT NULL;

        CREATE INDEX IF NOT EXISTS idx_drafts_created_by_key
            ON drafts (created_by_key_id) WHERE created_by_key_id IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS idx_drafts_created_by_key;
        DROP INDEX IF EXISTS idx_images_created_by_key;

        ALTER TABLE drafts DROP COLUMN IF EXISTS created_by_key_id;
        ALTER TABLE images DROP COLUMN IF EXISTS created_by_key_id;

        -- Any key already issued under the new scope would violate the old
        -- constraint. Revoke rather than delete: the row is the audit trail for
        -- whatever that key generated or published while it was live.
        UPDATE api_keys SET revoked_at = COALESCE(revoked_at, NOW())
            WHERE scope = 'agent';
        UPDATE api_keys SET scope = 'read' WHERE scope = 'agent';

        ALTER TABLE api_keys DROP CONSTRAINT IF EXISTS api_keys_scope_check;
        ALTER TABLE api_keys ADD CONSTRAINT api_keys_scope_check
            CHECK (scope IN ('read', 'post'));
        """
    )
