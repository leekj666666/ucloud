import sqlalchemy as sa
from alembic import op

revision = "demo0001"
down_revision = None


def upgrade():
    op.create_table(
        "stock",
        sa.Column("sku", sa.String(80), primary_key=True),
        sa.Column("available", sa.Integer(), nullable=False),
        sa.CheckConstraint("available >= 0"),
    )
    op.create_table(
        "reservations",
        sa.Column("key", sa.String(200), primary_key=True),
        sa.Column("sku", sa.String(80), sa.ForeignKey("stock.sku"), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.CheckConstraint("quantity > 0"),
    )
    op.create_table(
        "orders",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("key", sa.String(200), unique=True, nullable=False),
        sa.Column("sku", sa.String(80), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("trace_id", sa.String(36), nullable=False),
        sa.CheckConstraint("quantity > 0"),
    )
    op.execute("INSERT INTO stock VALUES ('demo-sku',100000),('verifier-sku',100000)")


def downgrade():
    raise RuntimeError(
        "Restore demo database backup rather than dropping business data"
    )
