"""Plugin audits become component audits — themes too, and hand-entered rows.

Three additions to `plugin_audits`:

* `component_type` ("plugin" | "theme"), because a plugin and a theme can
  legitimately share a slug. It is part of a component's identity, which is
  why the new unique constraint includes it.
* `source` ("wordpress" | "manual"). Reading `/wp/v2/plugins` and
  `/wp/v2/themes` needs an Application Password, so a site without one could
  never be audited at all. Those components can now be entered by hand, and a
  WordPress-sourced run must reconcile only its OWN rows — deleting something
  a person typed because WordPress didn't report it would be a data loss bug.
* `is_active`, nullable: NULL means "not known", which is the honest answer
  for a manual entry where nobody said.

Existing rows are all plugins read from WordPress, so the backfill defaults
are exactly right for them.

Revision ID: 0009_component_audits
Revises: 0008_ai_guidance
"""
from alembic import op

revision: str = "0009_component_audits"
down_revision: str | None = "0008_ai_guidance"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # IF NOT EXISTS throughout: 0001_baseline builds the schema with
    # `Base.metadata.create_all` from the CURRENT models, so on a fresh
    # database these columns and the constraint already exist by the time this
    # runs. Unguarded DDL aborts the whole chain — which is exactly how
    # `alembic upgrade head` came to fail from empty.
    op.execute(
        "ALTER TABLE plugin_audits "
        "ADD COLUMN IF NOT EXISTS component_type VARCHAR(10) NOT NULL DEFAULT 'plugin'"
    )
    op.execute(
        "ALTER TABLE plugin_audits "
        "ADD COLUMN IF NOT EXISTS source VARCHAR(20) NOT NULL DEFAULT 'wordpress'"
    )
    op.execute("ALTER TABLE plugin_audits ADD COLUMN IF NOT EXISTS is_active BOOLEAN")

    # There was never a uniqueness guarantee here, and the auditor keyed its
    # in-memory map by slug alone — so any duplicate rows that accumulated
    # were already being ignored. Keep the most recently audited one.
    op.execute(
        """
        DELETE FROM plugin_audits
        WHERE id IN (
            SELECT id FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY site_id, component_type, plugin_slug
                           ORDER BY audited_at DESC, id DESC
                       ) AS rn
                FROM plugin_audits
            ) ranked
            WHERE rn > 1
        )
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'uq_plugin_audits_site_type_slug'
            ) THEN
                ALTER TABLE plugin_audits
                    ADD CONSTRAINT uq_plugin_audits_site_type_slug
                    UNIQUE (site_id, component_type, plugin_slug);
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.drop_constraint("uq_plugin_audits_site_type_slug", "plugin_audits", type_="unique")
    op.drop_column("plugin_audits", "is_active")
    op.drop_column("plugin_audits", "source")
    op.drop_column("plugin_audits", "component_type")
