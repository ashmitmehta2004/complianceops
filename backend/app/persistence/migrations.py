"""Schema evolution for the SQLite prototype.

`create_all` only creates missing tables, so columns/indexes added after a database was first
created are applied here. Every step is additive and idempotent (inspect, then ALTER), which
keeps existing data intact and makes running it on every startup safe. This is deliberately not
Alembic: there is one dialect and only additive changes. Destructive changes would need a real
migration tool.
"""

from sqlalchemy import Engine, inspect, text

# table -> column -> SQL type, for columns added after the first release.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "approvals": {"decided_by": "VARCHAR", "comment": "TEXT", "run_id": "VARCHAR"},
    "agent_runs": {
        "outcome": "VARCHAR",
        "vendor_id": "VARCHAR",
        "review_id": "VARCHAR",
        "approval_id": "VARCHAR",
        "summary": "TEXT",
        "model_summary": "TEXT",
    },
}

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_approvals_run_id ON approvals (run_id)",
    "CREATE INDEX IF NOT EXISTS ix_agent_runs_vendor_id ON agent_runs (vendor_id)",
    "CREATE INDEX IF NOT EXISTS ix_agent_runs_approval_id ON agent_runs (approval_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_one_pending_approval_per_review "
    "ON approvals (review_id) WHERE decision = 'PENDING'",
)


def upgrade_schema(engine: Engine) -> list[str]:
    """Apply missing additive changes. Returns a description of what was changed."""
    applied: list[str] = []
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            if not inspector.has_table(table):
                continue
            existing = {c["name"] for c in inspector.get_columns(table)}
            for name, sql_type in columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))
                    applied.append(f"{table}.{name}")
        for statement in _INDEXES:
            conn.execute(text(statement))
    return applied
