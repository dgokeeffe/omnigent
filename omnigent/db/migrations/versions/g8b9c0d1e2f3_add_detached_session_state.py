"""Add first-class detached-session state and host inventory index.

Revision ID: g8b9c0d1e2f3
Revises: f7a8b9c0d1e2
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "g8b9c0d1e2f3"
down_revision: str | None = "f7a8b9c0d1e2"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
        batch_op.add_column(sa.Column("detached_at", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("detached_claim_host_id", Uuid16(), nullable=True))
    op.create_index(
        "ix_conversation_metadata_host_id",
        "omnigent_conversation_metadata",
        ["workspace_id", "host_id", "id"],
        unique=False,
    )
    op.create_index(
        "ix_conversation_metadata_detached_claim_host_id",
        "omnigent_conversation_metadata",
        ["workspace_id", "detached_claim_host_id", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_conversation_metadata_detached_claim_host_id",
        table_name="omnigent_conversation_metadata",
    )
    op.drop_index(
        "ix_conversation_metadata_host_id",
        table_name="omnigent_conversation_metadata",
    )
    with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
        batch_op.drop_column("detached_claim_host_id")
        batch_op.drop_column("detached_at")
