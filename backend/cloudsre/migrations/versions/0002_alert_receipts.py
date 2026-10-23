import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade():
    op.create_table(
        "alert_receipts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "incident_id", sa.String(36), sa.ForeignKey("incidents.id"), nullable=False
        ),
        sa.Column("alert_status", sa.String(16), nullable=False),
        sa.Column("received_at", sa.String(40), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
    )


def downgrade():
    raise RuntimeError("Destructive downgrade unsupported")
