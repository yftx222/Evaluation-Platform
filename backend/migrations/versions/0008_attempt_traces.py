"""One trace per new adapter attempt, sealed alongside its execution lifecycle."""

from alembic import op

revision = "0008_attempt_traces"
down_revision = "0007_task_archive"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE evaluation_runs ADD CONSTRAINT run_project_pair UNIQUE(id,project_id)")
    op.execute("""CREATE TABLE traces (
        id uuid PRIMARY KEY, project_id uuid NOT NULL, run_id uuid NOT NULL,
        execution_id uuid NOT NULL, attempt_id uuid NOT NULL UNIQUE,
        source_type text NOT NULL DEFAULT 'EVALUATION_INTERNAL'
            CHECK(source_type='EVALUATION_INTERNAL'), source_name text NOT NULL,
        status text NOT NULL DEFAULT 'OPEN'
            CHECK(status IN ('OPEN','COMPLETE','PARTIAL','MISSING')),
        close_reason text, started_at timestamptz NOT NULL, ended_at timestamptz,
        last_event_at timestamptz, event_count bigint NOT NULL DEFAULT 0 CHECK(event_count>=0),
        schema_version text NOT NULL DEFAULT '1.0', created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CHECK((status='OPEN')=(ended_at IS NULL)), CHECK(status<>'MISSING' OR event_count=0),
        CHECK(status NOT IN ('COMPLETE','PARTIAL') OR event_count>0),
        UNIQUE(id,attempt_id),
        FOREIGN KEY(run_id,project_id) REFERENCES evaluation_runs(id,project_id),
        FOREIGN KEY(execution_id,run_id) REFERENCES case_executions(id,run_id),
        FOREIGN KEY(attempt_id,execution_id) REFERENCES execution_attempts(id,execution_id))""")
    op.execute("""ALTER TABLE execution_attempts ADD CONSTRAINT attempt_main_trace
        FOREIGN KEY(trace_id,id) REFERENCES traces(id,attempt_id) DEFERRABLE INITIALLY DEFERRED""")
    op.execute("CREATE INDEX ix_trace_project ON traces(project_id,created_at)")
    op.execute("CREATE INDEX ix_trace_run ON traces(run_id,execution_id)")
    op.execute("""CREATE TABLE trace_events (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), trace_id uuid NOT NULL REFERENCES traces,
        event_key text NOT NULL, sequence bigint NOT NULL CHECK(sequence>=1),
        parent_event_key text, parent_event_id uuid, event_type text NOT NULL
        CHECK(event_type IN ('AGENT_STEP','MODEL_CALL','TOOL_CALL','RETRIEVAL','RERANK',
                             'GUARDRAIL','FINAL_OUTPUT','CUSTOM')),
        name text NOT NULL, status text NOT NULL
        CHECK(status IN ('STARTED','SUCCEEDED','FAILED','CANCELLED','UNKNOWN')),
        started_at timestamptz NOT NULL, ended_at timestamptz, duration_ms bigint,
        input jsonb NOT NULL, output jsonb NOT NULL, attributes jsonb NOT NULL,
        resource jsonb NOT NULL,
        schema_version text NOT NULL, received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        content_digest text NOT NULL, CHECK(event_key IS DISTINCT FROM parent_event_key),
        CHECK(ended_at>=started_at), CHECK(duration_ms BETWEEN 0 AND 604800000),
        UNIQUE(trace_id,event_key), UNIQUE(trace_id,sequence), UNIQUE(trace_id,id),
        FOREIGN KEY(trace_id,parent_event_id) REFERENCES trace_events(trace_id,id))""")
    op.execute("""CREATE FUNCTION protect_trace_event() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN IF TG_OP='DELETE' OR OLD.parent_event_id IS NOT NULL OR NEW.parent_event_id IS NULL
          OR (to_jsonb(NEW)-'parent_event_id')<>(to_jsonb(OLD)-'parent_event_id')
          OR NOT EXISTS(SELECT 1 FROM trace_events p WHERE p.trace_id=NEW.trace_id
              AND p.id=NEW.parent_event_id AND p.event_key=NEW.parent_event_key)
          THEN RAISE EXCEPTION 'immutable trace event' USING ERRCODE='23514'; END IF;
        RETURN NEW; END $$""")
    op.execute("""CREATE TRIGGER protect_event BEFORE UPDATE OR DELETE ON trace_events
        FOR EACH ROW EXECUTE FUNCTION protect_trace_event()""")
    op.execute("""CREATE TRIGGER frozen_trace BEFORE UPDATE ON traces FOR EACH ROW
        WHEN (OLD.status<>'OPEN') EXECUTE FUNCTION reject_revision_change()""")
    op.execute("CREATE INDEX ix_trace_type ON trace_events(trace_id,event_type,sequence)")
    op.execute("""CREATE INDEX ix_pending_parent ON trace_events(trace_id,parent_event_key)
        WHERE parent_event_key IS NOT NULL AND parent_event_id IS NULL""")
    op.execute("""CREATE FUNCTION allocate_trace_id() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN NEW.trace_id=gen_random_uuid(); RETURN NEW; END $$""")
    op.execute("""CREATE TRIGGER allocate_trace BEFORE INSERT ON execution_attempts
        FOR EACH ROW WHEN (NEW.stage='ADAPTER') EXECUTE FUNCTION allocate_trace_id()""")
    op.execute("""CREATE FUNCTION open_attempt_trace() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
        INSERT INTO traces(id,project_id,run_id,execution_id,attempt_id,source_name,started_at)
        SELECT NEW.trace_id,r.project_id,r.id,NEW.execution_id,NEW.id,
            r.config_snapshot->'project_version'->>'slug',NEW.created_at
            FROM evaluation_runs r WHERE r.id=NEW.run_id;
        RETURN NEW; END $$""")
    op.execute("""CREATE TRIGGER open_trace AFTER INSERT ON execution_attempts
        FOR EACH ROW WHEN (NEW.trace_id IS NOT NULL) EXECUTE FUNCTION open_attempt_trace()""")
    op.execute("""CREATE FUNCTION seal_open_trace(t uuid, why text) RETURNS void LANGUAGE sql AS $$
        UPDATE traces SET status=CASE WHEN event_count=0 THEN 'MISSING' ELSE 'PARTIAL' END,
        ended_at=clock_timestamp(), updated_at=clock_timestamp(),close_reason=why
        WHERE id=t AND status='OPEN' $$""")
    op.execute("""CREATE FUNCTION close_attempt_trace() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN PERFORM seal_open_trace(NEW.trace_id,'WINDOW_CLOSED'); RETURN NEW; END $$""")
    op.execute("""CREATE TRIGGER close_trace AFTER UPDATE ON execution_attempts
        FOR EACH ROW WHEN (NEW.trace_id IS NOT NULL AND
        (NEW.status NOT IN ('RUNNING','CREATED') OR
        (OLD.trace_verifier IS NOT NULL AND NEW.trace_verifier IS NULL)))
        EXECUTE FUNCTION close_attempt_trace()""")
    op.execute("""CREATE FUNCTION expire_attempt_trace() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN PERFORM seal_open_trace(a.trace_id,'CREDENTIAL_EXPIRED') FROM execution_attempts a
        WHERE a.execution_id=NEW.id AND a.epoch=NEW.lease_epoch
        AND a.trace_expires_at<=clock_timestamp(); RETURN NEW; END $$""")
    op.execute("""CREATE TRIGGER expire_trace AFTER UPDATE OF lease_expires_at ON case_executions
        FOR EACH ROW WHEN (NEW.stage='ADAPTER' AND NEW.status='RUNNING')
        EXECUTE FUNCTION expire_attempt_trace()""")


def downgrade() -> None:
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM traces) THEN
        RAISE EXCEPTION 'preserve trace history; restore a pre-upgrade backup to roll back';
        END IF; END $$""")
    for trigger, table in [
        ("expire_trace", "case_executions"),
        ("close_trace", "execution_attempts"),
        ("open_trace", "execution_attempts"),
        ("allocate_trace", "execution_attempts"),
    ]:
        op.execute(f"DROP TRIGGER {trigger} ON {table}")
    for function in [
        "expire_attempt_trace()",
        "close_attempt_trace()",
        "seal_open_trace(uuid,text)",
        "open_attempt_trace()",
        "allocate_trace_id()",
    ]:
        op.execute(f"DROP FUNCTION {function}")
    op.execute("ALTER TABLE execution_attempts DROP CONSTRAINT attempt_main_trace")
    op.drop_table("trace_events")
    op.execute("DROP FUNCTION protect_trace_event()")
    op.drop_table("traces")
    op.execute("ALTER TABLE evaluation_runs DROP CONSTRAINT run_project_pair")
