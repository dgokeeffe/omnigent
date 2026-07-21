"""merge host_permissions and user_id-unification heads

Revision ID: 5aca26f83866
Revises: b3c1a2d4e5f6, zz1a2b3c4d5e6
Create Date: 2026-07-21 22:37:49.656744
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "5aca26f83866"
down_revision: str | None = ("b3c1a2d4e5f6", "zz1a2b3c4d5e6")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
