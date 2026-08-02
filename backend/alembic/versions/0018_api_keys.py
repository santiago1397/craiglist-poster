"""agent API keys

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-01

A read surface for AI agents needs a credential, and none of the three we
already have fits.

`machine_tokens` is the closest, and using it would have been a mistake.
`require_machine_token` returns `machine`, and `/queue/claim` makes an
authorization decision with that string against each account's
`allowed_machine` binding. That column is an *identity*, not a label. Putting a
row in it that is not a machine puts a non-machine into the one table where a
machine name is trusted, and any later code path that forgets to check a scope
becomes an agent claiming drafts as if it were the posting desktop. A separate
table means the queue endpoints cannot see these rows at all, whatever anyone
writes next.

`INGEST_BEARER_TOKEN` is a single static env value — rotating it is a redeploy,
and it cannot be handed to one agent and revoked from that agent alone.

Same storage shape as `machine_tokens` otherwise, because that part is sound:
`<row id>.<secret>`, argon2 over the secret, id makes verification one indexed
lookup rather than a hash comparison against every live row.

`scope` is 'read' or 'post'. Two keys rather than one scoped key is deliberate.
Read requests are allowed to carry the key in the query string — many agent
fetch tools cannot set headers at all, and excluding them defeats the purpose —
which means a read key will end up in an access log or a shell history sooner
or later. When it does, the answer should be "rotate it, nothing was at risk".
The posting scope is header-only (enforced in `security.py`, not here), so it
never travels by the leaky path.
"""
from __future__ import annotations

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS api_keys (
            id          BIGSERIAL PRIMARY KEY,
            label       TEXT NOT NULL DEFAULT '',
            scope       TEXT NOT NULL DEFAULT 'read',
            token_hash  TEXT NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_seen_at TIMESTAMPTZ,
            revoked_at  TIMESTAMPTZ,
            CONSTRAINT api_keys_scope_check CHECK (scope IN ('read', 'post'))
        );

        CREATE INDEX IF NOT EXISTS idx_api_keys_live
            ON api_keys (id) WHERE revoked_at IS NULL;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS api_keys;")
