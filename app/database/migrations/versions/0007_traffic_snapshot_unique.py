"""Traffic snapshots — one row per (site, date). Duplicate rows had been
accumulating from repeated "Flush & Re-run" clicks (there was no upsert,
just a blind INSERT every run), which silently duplicated dates in the
Daily Snapshots table and made it ambiguous which row a later
estimated-to-real-GA4 upgrade should update. Keeps the best surviving row
per group — real GA4 data over estimated, then the most recently written.

Revision ID: 0007_traffic_snapshot_unique
Revises: 0006_flow_category_goals
"""
from alembic import op

revision: str = "0007_traffic_snapshot_unique"
down_revision: str | None = "0006_flow_category_goals"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM traffic_snapshots
        WHERE id IN (
            SELECT id FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY site_id, date
                           ORDER BY (source = 'ga4') DESC, snapshot_at DESC, id DESC
                       ) AS rn
                FROM traffic_snapshots
            ) ranked
            WHERE rn > 1
        )
        """
    )
    op.create_unique_constraint(
        "uq_traffic_snapshots_site_date", "traffic_snapshots", ["site_id", "date"]
    )
    # The unique constraint's own index covers the same (site_id, date)
    # lookup this plain index existed for — redundant once it exists.
    op.drop_index("idx_traffic_snapshots_site_date", table_name="traffic_snapshots")


def downgrade() -> None:
    op.create_index(
        "idx_traffic_snapshots_site_date", "traffic_snapshots", ["site_id", "date"]
    )
    op.drop_constraint("uq_traffic_snapshots_site_date", "traffic_snapshots", type_="unique")
