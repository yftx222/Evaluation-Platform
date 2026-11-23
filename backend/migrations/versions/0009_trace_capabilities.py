"""Freeze the collection contract at project version publication."""

from alembic import op

revision = "0009_trace_capabilities"
down_revision = "0008_attempt_traces"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""ALTER TABLE project_versions ADD COLUMN trace_capabilities jsonb
        CHECK(jsonb_typeof(trace_capabilities)='object')""")
    op.execute(
        """CREATE INDEX ix_trace_children ON trace_events(trace_id,parent_event_id,sequence)"""
    )
    op.execute("""CREATE INDEX ix_trace_time ON trace_events(trace_id,started_at)""")


def downgrade() -> None:
    op.drop_index("ix_trace_time", table_name="trace_events")
    op.drop_index("ix_trace_children", table_name="trace_events")
    op.drop_column("project_versions", "trace_capabilities")
