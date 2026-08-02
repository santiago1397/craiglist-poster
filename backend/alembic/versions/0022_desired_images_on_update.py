"""let a desired state be re-keyed without orphaning its images

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-02

`post_desired_images` references `post_desired_state(post_id)` with ON DELETE
CASCADE and nothing for updates. Deleting a desired state has always taken its
images with it, which is right; changing its `post_id` was simply never done.

0022 makes it necessary. Merging the two rows a listing can acquire — one keyed
by the base62 token from the posting URL, one by Craigslist's numeric id — moves
the desired state onto the surviving row, and the images referencing it are left
pointing at an id that no longer exists. Postgres refuses, correctly, and the
whole merge rolls back.

ON UPDATE CASCADE is the smallest thing that makes a re-key legal, and it
matches the intent already expressed by the delete rule: these images belong to
that desired state, wherever it goes.
"""
from __future__ import annotations

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE post_desired_images
            DROP CONSTRAINT IF EXISTS post_desired_images_post_id_fkey;
        ALTER TABLE post_desired_images
            ADD CONSTRAINT post_desired_images_post_id_fkey
            FOREIGN KEY (post_id) REFERENCES post_desired_state(post_id)
            ON DELETE CASCADE ON UPDATE CASCADE;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE post_desired_images
            DROP CONSTRAINT IF EXISTS post_desired_images_post_id_fkey;
        ALTER TABLE post_desired_images
            ADD CONSTRAINT post_desired_images_post_id_fkey
            FOREIGN KEY (post_id) REFERENCES post_desired_state(post_id)
            ON DELETE CASCADE;
        """
    )
