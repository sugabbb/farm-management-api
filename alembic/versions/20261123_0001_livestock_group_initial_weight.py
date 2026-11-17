"""Add livestock_groups.initial_weight_g (weight of one Lele fish at stocking, in grams).

It is the starting point of a group's first real ADG interval (stocking -> first sampling). Existing
groups have no value; it is entered through the livestock group API, which then recomputes the
group's ``lele_sampling_results``.

Revision ID: 20261123_0001
Revises: 20261122_0001
Create Date: 2026-11-23
"""

from alembic import op

revision = "20261123_0001"
down_revision = "20261122_0001"
branch_labels = None
depends_on = None

TABLE = "livestock_groups"
CONSTRAINT = "ck_livestock_groups_initial_weight"


def upgrade() -> None:
    # NOTE: the initial migration builds livestock_groups from the ORM metadata, so on a brand new
    # database the column and its check already exist by the time this revision runs, while older
    # databases do not have them. Add them idempotently to serve both.
    op.execute(f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS initial_weight_g NUMERIC(12, 3)")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{CONSTRAINT}') THEN
                ALTER TABLE {TABLE} ADD CONSTRAINT {CONSTRAINT}
                    CHECK (initial_weight_g IS NULL OR (species = 'LELE' AND initial_weight_g > 0));
            END IF;
        END $$
        """
    )


def downgrade() -> None:
    op.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS {CONSTRAINT}")
    op.execute(f"ALTER TABLE {TABLE} DROP COLUMN IF EXISTS initial_weight_g")
