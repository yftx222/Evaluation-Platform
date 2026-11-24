"""Allocate a trace only when the immutable Run snapshot enables collection."""

from alembic import op

revision = "0011_trace_allocation"
down_revision = "0010_trace_archives"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE OR REPLACE FUNCTION allocate_trace_id() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          SELECT CASE WHEN coalesce(config_snapshot #>> '{project_version,trace_capabilities,mode}',
                                    'callback') <> 'disabled' THEN gen_random_uuid() END
            INTO STRICT NEW.trace_id FROM evaluation_runs WHERE id=NEW.run_id;
          RETURN NEW;
        END $$""")


def downgrade() -> None:
    op.execute("""CREATE OR REPLACE FUNCTION allocate_trace_id() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN NEW.trace_id=gen_random_uuid(); RETURN NEW; END $$""")
