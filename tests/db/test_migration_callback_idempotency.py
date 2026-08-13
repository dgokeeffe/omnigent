"""Upgrade/downgrade coverage for durable callback idempotency records."""

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config

_PRIOR = "g8b9c0d1e2f3"
_REVISION = "h1c2d3e4f5a6"


def test_callback_idempotency_migration_roundtrip(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'callback-idempotency.db'}"
    engine = sa.create_engine(uri)
    config = _build_alembic_config(uri)
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, _PRIOR)
            assert "callback_idempotency" not in sa.inspect(connection).get_table_names()
            command.upgrade(config, _REVISION)
            assert "callback_idempotency" in sa.inspect(connection).get_table_names()
            columns = {
                column["name"]
                for column in sa.inspect(connection).get_columns("callback_idempotency")
            }
            assert "pending_input_effects" in columns
            indexes = {
                index["name"]: index["column_names"]
                for index in sa.inspect(connection).get_indexes("callback_idempotency")
            }
            assert indexes["ix_callback_idempotency_created_at"] == ["created_at"]
            command.downgrade(config, _PRIOR)
            assert "callback_idempotency" not in sa.inspect(connection).get_table_names()
    finally:
        engine.dispose()
