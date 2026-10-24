"""Durable incident ownership, immutable action intents and atomic audit."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"


def upgrade():
    op.create_table(
        "task_ownership",
        sa.Column(
            "incident_id",
            sa.String(36),
            sa.ForeignKey("incidents.id"),
            primary_key=True,
        ),
        sa.Column("owner", sa.String(64), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("resource_key", sa.String(200), nullable=False),
        sa.Column("heartbeat_at", sa.String(40), nullable=False),
    )
    op.create_table(
        "workflow_audit",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "incident_id", sa.String(36), sa.ForeignKey("incidents.id"), nullable=False
        ),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("owner", sa.String(64), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("at", sa.String(40), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
    )
    op.create_table(
        "action_intents",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "incident_id", sa.String(36), sa.ForeignKey("incidents.id"), nullable=False
        ),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("spec_hash", sa.String(64), nullable=False),
        sa.Column("spec", sa.Text(), nullable=False),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
    )
    op.create_index("ix_action_incident", "action_intents", ["incident_id"])
    op.create_index("ix_audit_incident", "workflow_audit", ["incident_id"])


def downgrade():
    raise RuntimeError("Destructive downgrade unsupported")
