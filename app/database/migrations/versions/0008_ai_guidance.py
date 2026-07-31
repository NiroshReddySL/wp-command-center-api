"""Structured AI guidance per content post — a diagnosis, ready-to-paste
title/meta rewrites and evidence-backed content gaps, stored separately from
the plain-text ai_recommendation so existing consumers of that field keep
rendering valid prose.

Revision ID: 0008_ai_guidance
Revises: 0007_traffic_snapshot_unique
"""
from alembic import op

revision: str = "0008_ai_guidance"
down_revision: str | None = "0007_traffic_snapshot_unique"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE content_posts ADD COLUMN IF NOT EXISTS ai_guidance JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE content_posts DROP COLUMN IF EXISTS ai_guidance")
