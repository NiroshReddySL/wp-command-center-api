"""Flow category goal steps — mark a step as the conversion event (e.g. a
"thank you" page) so the dashboard can report a real, explicitly-labeled
"leads" count instead of assuming whichever step is last is the goal.

Revision ID: 0006_flow_category_goals
Revises: 0005_flow_categories
"""
from alembic import op

revision: str = "0006_flow_category_goals"
down_revision: str | None = "0005_flow_categories"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE flow_category_steps ADD COLUMN IF NOT EXISTS is_goal BOOLEAN NOT NULL DEFAULT FALSE"
    )
    op.execute(
        "ALTER TABLE flow_category_snapshots ADD COLUMN IF NOT EXISTS goal_step_index INTEGER"
    )
    op.execute(
        "ALTER TABLE flow_category_snapshots ADD COLUMN IF NOT EXISTS leads INTEGER"
    )
    op.execute(
        "ALTER TABLE flow_category_snapshots ADD COLUMN IF NOT EXISTS lead_rate DOUBLE PRECISION"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE flow_category_snapshots DROP COLUMN IF EXISTS lead_rate")
    op.execute("ALTER TABLE flow_category_snapshots DROP COLUMN IF EXISTS leads")
    op.execute("ALTER TABLE flow_category_snapshots DROP COLUMN IF EXISTS goal_step_index")
    op.execute("ALTER TABLE flow_category_steps DROP COLUMN IF EXISTS is_goal")
