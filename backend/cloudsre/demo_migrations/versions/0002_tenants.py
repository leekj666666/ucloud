"""Keep existing demo data; add explicit tenant ownership for verification fixtures."""

import sqlalchemy as sa
from alembic import op

revision = "demo0002"
down_revision = "demo0001"


def upgrade():
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("purpose", sa.String(40), nullable=False),
    )
    op.execute("INSERT INTO tenants (id,purpose) VALUES ('demo','business')")
    for table in ("stock", "reservations", "orders"):
        op.add_column(
            table,
            sa.Column(
                "tenant_id", sa.String(80), nullable=False, server_default="demo"
            ),
        )
        op.create_index("ix_" + table + "_tenant_id", table, ["tenant_id"])


def downgrade():
    raise RuntimeError("Tenant ownership cannot be removed automatically")
