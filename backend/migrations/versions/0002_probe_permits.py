"""Shared permits for live connection probes; attempt linkage arrives with the runner."""

from alembic import op

revision = "0002_probe_permits"
down_revision = "0001_projects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE call_leases (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        created_at timestamptz NOT NULL DEFAULT now(),
        quota_key text NOT NULL, expires_at timestamptz NOT NULL)""")
    op.create_index("ix_call_leases_quota", "call_leases", ["quota_key", "expires_at"])


def downgrade() -> None:
    op.drop_table("call_leases")
