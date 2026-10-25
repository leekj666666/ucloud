"""Durable operator requests, immutable plan decisions and human audit."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"


def upgrade():
    op.create_table(
        "incident_controls",
        sa.Column(
            "incident_id",
            sa.String(36),
            sa.ForeignKey("incidents.id"),
            primary_key=True,
        ),
        sa.Column("namespace", sa.String(64), nullable=False),
        sa.Column("execution_mode", sa.String(16), nullable=False),
        sa.Column("requested_by", sa.String(100), nullable=False),
        sa.Column("requested_at", sa.String(40), nullable=False),
        sa.Column("stop_requested_at", sa.String(40)),
        sa.Column("stop_reason", sa.Text()),
    )
    op.create_table(
        "plan_decisions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "incident_id", sa.String(36), sa.ForeignKey("incidents.id"), nullable=False
        ),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("action_id", sa.String(64), nullable=False, unique=True),
        sa.Column("intent_hash", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
    )
    op.create_table(
        "operator_events",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "incident_id", sa.String(36), sa.ForeignKey("incidents.id"), nullable=False
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("at", sa.String(40), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
    )
    op.create_index("ix_operator_incident", "operator_events", ["incident_id"])


def downgrade():
    raise RuntimeError("Destructive downgrade unsupported")
