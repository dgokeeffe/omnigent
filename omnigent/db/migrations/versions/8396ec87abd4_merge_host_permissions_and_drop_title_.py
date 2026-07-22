"""merge host-permissions and drop-title_hash heads

Revision ID: 8396ec87abd4
Revises: 5aca26f83866, 72e6dceae14f
Create Date: 2026-07-22 10:33:00.872837
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "8396ec87abd4"
down_revision: str | None = ("5aca26f83866", "72e6dceae14f")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
