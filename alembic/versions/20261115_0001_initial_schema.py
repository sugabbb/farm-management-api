"""Create the empty transactional schema.

Revision ID: 20261115_0001
Revises: None
Create Date: 2026-11-15
"""

from alembic import op
from app.db import models  # noqa: F401
from app.db.base import Base

revision = "20261115_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=False)


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), checkfirst=False)
