"""Store production evidence independently from evaluation attempts."""

from alembic import op

revision = "0012_production_traces"
down_revision = "0011_trace_allocation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("""CREATE TABLE production_traces (
        id uuid PRIMARY KEY, project_id uuid NOT NULL REFERENCES projects,
        environment text NOT NULL, agent_run_id text NOT NULL, root jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT
        now(),
        deadline timestamptz NOT NULL, retained_until timestamptz, pinned boolean NOT NULL
        DEFAULT false,
        completeness text NOT NULL DEFAULT 'OPEN' CHECK(completeness IN ('OPEN','COMPLETE',
        'PARTIAL','MISSING')),
        availability text NOT NULL DEFAULT 'AVAILABLE' CHECK(availability IN ('AVAILABLE',
        'ARCHIVED','EXPIRED','DELETED')),
        event_count integer NOT NULL DEFAULT 0, last_sequence bigint NOT NULL DEFAULT 0,
        finalize_digest text, UNIQUE(project_id,environment,agent_run_id))""")
    op.execute(
        """CREATE INDEX production_trace_lookup ON production_traces(project_id,environment,
            created_at,id)"""
    )
    op.execute("""CREATE INDEX production_trace_conversation ON production_traces
        (project_id,environment,(root->>'conversation_id'))""")
    op.execute(
        """CREATE INDEX production_trace_deadline ON production_traces(deadline) WHERE
            completeness='OPEN'"""
    )
    op.execute("""CREATE TABLE production_trace_events (
        trace_id uuid NOT NULL REFERENCES production_traces ON DELETE CASCADE, sequence bigint
        NOT NULL CHECK(sequence>0),
        event_key text NOT NULL, parent_event_key text, event_type text NOT NULL, name text NOT
        NULL, status text NOT NULL,
        digest text NOT NULL, metadata jsonb NOT NULL, payload bytea NOT NULL, key_version text
        NOT NULL,
        PRIMARY KEY(trace_id,sequence), UNIQUE(trace_id,event_key))""")
    op.execute("""CREATE TABLE production_trace_cold_events
        (LIKE production_trace_events INCLUDING DEFAULTS,
         PRIMARY KEY(trace_id,sequence), FOREIGN KEY(trace_id) REFERENCES production_traces)""")


def downgrade() -> None:
    op.drop_table("production_trace_cold_events")
    op.drop_table("production_trace_events")
    op.drop_table("production_traces")
