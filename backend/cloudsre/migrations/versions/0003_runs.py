import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"


def upgrade():
    op.create_table(
        "workflow_runs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "incident_id", sa.String(36), sa.ForeignKey("incidents.id"), nullable=False
        ),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
    )


def downgrade():
    raise RuntimeError("Destructive downgrade unsupported")
