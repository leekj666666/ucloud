"""Adopt the validated D0 schema, preserving all incident rows."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "incidents" in inspector.get_table_names():
        expected = {
            "id",
            "dedup_key",
            "title",
            "service",
            "status",
            "created_at",
            "diagnosis",
        }
        if {c["name"] for c in inspector.get_columns("incidents")} != expected:
            raise RuntimeError("Existing incidents schema differs from D0")
        unique = any(
            c["column_names"] == ["dedup_key"]
            for c in inspector.get_unique_constraints("incidents")
        )
        if not unique and op.get_bind().dialect.name == "sqlite":
            for row in op.get_bind().exec_driver_sql("PRAGMA index_list(incidents)"):
                if row[2]:
                    name = op.get_bind().dialect.identifier_preparer.quote(row[1])
                    columns = [
                        c[2]
                        for c in op.get_bind().exec_driver_sql(
                            f"PRAGMA index_info({name})"
                        )
                    ]
                    unique = unique or columns == ["dedup_key"]
        if not unique:
            raise RuntimeError("Missing dedup constraint")
        return
    op.create_table(
        "incidents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("dedup_key", sa.String(200), nullable=False, unique=True),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("service", sa.String(100), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("diagnosis", sa.Text(), nullable=True),
    )


def downgrade():
    raise RuntimeError("Destructive downgrade unsupported; restore backup")
