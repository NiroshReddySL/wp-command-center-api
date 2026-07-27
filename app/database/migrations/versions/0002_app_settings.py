"""app_settings key/value store for agent toggles and notification prefs.

Uses IF NOT EXISTS because fresh installs already get the table from the
0001 baseline's create_all (the model exists in metadata).

Revision ID: 0002_app_settings
Revises: 0001_baseline
"""
from alembic import op

revision: str = "0002_app_settings"
down_revision: str | None = "0001_baseline"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app_settings (
            key VARCHAR(64) PRIMARY KEY,
            value JSON NOT NULL DEFAULT '{}',
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app_settings")
