"""Add bounded durable callback idempotency records.

Revision ID: h1c2d3e4f5a6
Revises: g8b9c0d1e2f3
Create Date: 2026-08-13 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "h1c2d3e4f5a6"
down_revision: str | None = "g8b9c0d1e2f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "callback_idempotency",
        sa.Column("workspace_id", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("key_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("conversation_id", sa.LargeBinary(length=16), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("actor_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("payload_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column(
            "pending_input_effects",
            sa.Text(),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("item_id", sa.LargeBinary(length=16), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "key_digest"),
    )
    op.create_index(
        "ix_callback_idempotency_created_at",
        "callback_idempotency",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_callback_idempotency_created_at",
        table_name="callback_idempotency",
    )
    op.drop_table("callback_idempotency")
