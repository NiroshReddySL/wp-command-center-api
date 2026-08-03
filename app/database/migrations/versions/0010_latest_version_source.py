"""Record where a component's "latest version" came from.

`latest_version` alone cannot distinguish three very different situations,
and conflating them made an unauditable component look perfectly current:

* resolved from the WordPress.org directory — trustworthy;
* supplied by the operator, which is the only option for premium and custom
  components the directory has never heard of (Avada, Swift Performance,
  anything built in-house);
* nobody knows, so it was left equal to the installed version — which then
  compared equal and reported "up to date" about a component that had simply
  never been checked.

Existing rows all came from the directory lookup, except manual entries,
which were created with latest_version set equal to installed and are
therefore genuinely unknown until someone says otherwise.

Revision ID: 0010_latest_version_source
Revises: 0009_component_audits
"""
from alembic import op

revision: str = "0010_latest_version_source"
down_revision: str | None = "0009_component_audits"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # IF NOT EXISTS: 0001_baseline builds the schema with create_all from the
    # CURRENT models, so on a fresh database this column already exists.
    op.execute(
        "ALTER TABLE plugin_audits "
        "ADD COLUMN IF NOT EXISTS latest_source VARCHAR(10) NOT NULL DEFAULT 'unknown'"
    )
    # A differing latest is proof that something resolved it upstream: every
    # row is created with latest equal to installed, and only the directory
    # lookup ever changes that. Deliberately NOT gated on `source` — a manual
    # row the auditor has already enriched is just as much a directory result,
    # and excluding those would relabel real answers as "never checked".
    op.execute(
        "UPDATE plugin_audits SET latest_source = 'wporg' "
        "WHERE latest_version <> installed_version"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE plugin_audits DROP COLUMN IF EXISTS latest_source")
