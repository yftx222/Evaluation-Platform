"""Frozen evaluation plans, durable runs, cases and provenance."""

from alembic import op

revision = "0005_evaluation_runs"
down_revision = "0004_evaluators"
branch_labels = None
depends_on = None

RUN = {
    "PENDING": ("QUEUED", "FAILED", "CANCEL_REQUESTED"),
    "QUEUED": ("RUNNING", "PAUSE_REQUESTED", "CANCEL_REQUESTED", "FAILED"),
    "RUNNING": ("COMPLETED", "PARTIAL_FAILED", "FAILED", "PAUSE_REQUESTED", "CANCEL_REQUESTED"),
    "PAUSE_REQUESTED": ("PAUSED", "COMPLETED", "PARTIAL_FAILED", "FAILED", "CANCEL_REQUESTED"),
    "PAUSED": ("QUEUED", "CANCEL_REQUESTED"),
    "CANCEL_REQUESTED": ("CANCELLED",),
}
CASE = {
    "PENDING": ("READY", "CANCELLED"),
    "READY": ("RUNNING", "CANCELLED"),
    "RUNNING": ("SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED", "UNKNOWN_DELIVERY", "RETRY_WAIT"),
    "RETRY_WAIT": ("READY", "TIMED_OUT", "CANCELLED"),
}
ATTEMPT = {
    "CREATED": ("RUNNING", "CANCELLED", "ABANDONED", "FAILED", "TIMED_OUT"),
    "RUNNING": ("SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED", "ABANDONED", "UNKNOWN_DELIVERY"),
}


def protect(table: str, mutable: str, transitions: dict[str, tuple[str, ...]]) -> None:
    """An allowlist of mutable columns also protects future snapshot fields by default."""
    columns = ",".join(repr(s) for s in mutable.split())
    branches = " ".join(
        f"WHEN '{source}' THEN NEW.status IN ({','.join(map(repr, targets))})"
        for source, targets in transitions.items()
    )
    status_guard = ""
    if transitions:
        active = ",".join(map(repr, transitions))
        status_guard = f"""IF OLD.status NOT IN ({active}) AND NEW IS DISTINCT FROM OLD THEN
            RAISE EXCEPTION 'terminal record' USING ERRCODE='23514'; END IF;
            IF NEW.status <> OLD.status AND NOT
            (CASE OLD.status {branches} ELSE false END) THEN
            RAISE EXCEPTION 'invalid transition' USING ERRCODE='23514'; END IF;"""
    op.execute(f"""CREATE FUNCTION guard_{table}() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
        IF TG_OP='DELETE' OR (to_jsonb(NEW)-ARRAY[{columns}]) IS DISTINCT FROM
            (to_jsonb(OLD)-ARRAY[{columns}]) THEN
            RAISE EXCEPTION 'frozen evaluation data' USING ERRCODE='23514'; END IF;
        {status_guard}
        RETURN NEW; END $$""")
    op.execute(f"""CREATE TRIGGER guard BEFORE UPDATE OR DELETE ON {table}
                   FOR EACH ROW EXECUTE FUNCTION guard_{table}()""")


def upgrade() -> None:
    op.execute("ALTER TABLE project_versions ADD UNIQUE(project_id,id)")
    op.execute("ALTER TABLE evaluation_cases ADD UNIQUE(dataset_version_id,id)")
    op.execute("""CREATE TABLE evaluation_tasks(
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL, project_version_id uuid NOT NULL, dataset_version_id uuid NOT NULL
        REFERENCES dataset_versions, name text NOT NULL, description text,
        config_snapshot jsonb NOT NULL, config_digest text NOT NULL, created_by text NOT NULL,
        FOREIGN KEY(project_id,project_version_id) REFERENCES project_versions(project_id,id),
        UNIQUE(id,project_id,dataset_version_id))""")
    protect("evaluation_tasks", "name description", {})
    op.execute("""CREATE TABLE task_evaluators(
        task_id uuid NOT NULL REFERENCES evaluation_tasks DEFERRABLE INITIALLY DEFERRED,
        evaluator_version_id uuid NOT NULL REFERENCES evaluator_versions,
        position int CHECK(position>=0), PRIMARY KEY(task_id,evaluator_version_id),
        UNIQUE(task_id,position))""")
    op.execute("""CREATE FUNCTION freeze_plan_member() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN IF TG_OP='INSERT' AND NOT EXISTS(SELECT 1 FROM evaluation_tasks WHERE id=NEW.task_id)
        THEN RETURN NEW; END IF;
        RAISE EXCEPTION 'frozen evaluator plan' USING ERRCODE='23514'; END $$""")
    op.execute("""CREATE TRIGGER frozen BEFORE INSERT OR UPDATE OR DELETE ON task_evaluators
        FOR EACH ROW EXECUTE FUNCTION freeze_plan_member()""")
    op.execute("""CREATE TABLE evaluation_runs(
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        created_at timestamptz NOT NULL DEFAULT now(),
        task_id uuid NOT NULL, project_id uuid NOT NULL, dataset_version_id uuid NOT NULL,
        request_key text NOT NULL, config_snapshot jsonb NOT NULL, config_digest text NOT NULL,
        total_cases int NOT NULL CHECK(total_cases>0), status text NOT NULL DEFAULT 'PENDING'
        CHECK(status IN ('PENDING','QUEUED','RUNNING','PAUSE_REQUESTED','PAUSED','CANCEL_REQUESTED',
        'CANCELLED','COMPLETED','PARTIAL_FAILED','FAILED')), started_at timestamptz,
        finished_at timestamptz, cancel_requested_at timestamptz, last_error_code text,
        FOREIGN KEY(task_id,project_id,dataset_version_id)
            REFERENCES evaluation_tasks(id,project_id,dataset_version_id),
        UNIQUE(task_id,request_key), UNIQUE(id,dataset_version_id))""")
    protect(
        "evaluation_runs", "status started_at finished_at cancel_requested_at last_error_code", RUN
    )
    op.execute("""CREATE TABLE case_executions(
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        created_at timestamptz NOT NULL DEFAULT now(),
        run_id uuid NOT NULL, dataset_version_id uuid NOT NULL, case_id uuid NOT NULL,
        status text NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','READY','RUNNING',
         'RETRY_WAIT','SUCCEEDED','FAILED','TIMED_OUT','CANCELLED','UNKNOWN_DELIVERY')),
        stage text NOT NULL DEFAULT 'ADAPTER'
        CHECK(stage IN ('ADAPTER','TRACE','SCORING','FINALIZE')),
        attempt_count int NOT NULL DEFAULT 0 CHECK(attempt_count>=0),
        lease_epoch bigint NOT NULL DEFAULT 0 CHECK(lease_epoch>=0), lease_owner text,
        lease_expires_at timestamptz, next_retry_at timestamptz, prediction jsonb,
        prediction_attempt_id uuid, adapter_error_code text, started_at timestamptz,
        finished_at timestamptz, latency_ms int CHECK(latency_ms>=0),
        CHECK((prediction IS NULL)=(prediction_attempt_id IS NULL)),
        CHECK((lease_owner IS NULL)=(lease_expires_at IS NULL)),
        CHECK((status='RUNNING')=(lease_owner IS NOT NULL)),
        FOREIGN KEY(run_id,dataset_version_id) REFERENCES evaluation_runs(id,dataset_version_id),
        FOREIGN KEY(dataset_version_id,case_id) REFERENCES evaluation_cases(dataset_version_id,id),
        UNIQUE(run_id,case_id), UNIQUE(id,run_id))""")
    protect(
        "case_executions",
        "status stage attempt_count lease_epoch lease_owner lease_expires_at "
        "next_retry_at prediction prediction_attempt_id adapter_error_code "
        "started_at finished_at latency_ms",
        CASE,
    )
    op.execute("""CREATE TABLE execution_attempts(
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        created_at timestamptz NOT NULL DEFAULT now(),
        execution_id uuid NOT NULL, run_id uuid NOT NULL, epoch bigint NOT NULL CHECK(epoch>0),
        lease_owner text NOT NULL, stage text NOT NULL CHECK(stage IN ('ADAPTER','EVALUATOR')),
        evaluator_version_id uuid REFERENCES evaluator_versions,
        attempt_no int NOT NULL CHECK(attempt_no>0), logical_step_key text NOT NULL,
        external_request_key text, request_digest text,
        delivery_state text NOT NULL DEFAULT 'not_sent'
          CHECK(delivery_state IN ('not_sent','sent','unknown')),
        status text NOT NULL DEFAULT 'CREATED' CHECK(status IN ('CREATED','RUNNING','SUCCEEDED',
          'FAILED','TIMED_OUT','ABANDONED','UNKNOWN_DELIVERY','CANCELLED')),
        started_at timestamptz, finished_at timestamptz, latency_ms int CHECK(latency_ms>=0),
        error_code text, sanitized_error text, provider_request_id text, trace_id uuid,
        trace_verifier text, trace_expires_at timestamptz,
        CHECK((stage='EVALUATOR')=(evaluator_version_id IS NOT NULL)),
        CHECK((external_request_key IS NULL)=(request_digest IS NULL)),
        CHECK(stage<>'ADAPTER' OR external_request_key IS NOT NULL),
        FOREIGN KEY(execution_id,run_id) REFERENCES case_executions(id,run_id),
        UNIQUE(execution_id,attempt_no), UNIQUE(id,execution_id),
        UNIQUE(id,execution_id,evaluator_version_id))""")
    protect(
        "execution_attempts",
        "status delivery_state started_at finished_at latency_ms error_code "
        "sanitized_error provider_request_id trace_id trace_verifier trace_expires_at",
        ATTEMPT,
    )
    op.execute("""ALTER TABLE case_executions ADD CONSTRAINT prediction_source
        FOREIGN KEY(prediction_attempt_id,id) REFERENCES execution_attempts(id,execution_id)""")
    op.execute("ALTER TABLE call_leases ADD attempt_id uuid UNIQUE REFERENCES execution_attempts")
    op.execute("""CREATE TABLE evaluation_scores(
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        created_at timestamptz NOT NULL DEFAULT now(),
        execution_id uuid NOT NULL REFERENCES case_executions, evaluator_version_id uuid NOT NULL
        REFERENCES evaluator_versions, created_from_attempt_id uuid NOT NULL, status text NOT NULL,
        score numeric, passed boolean, reason text NOT NULL, details jsonb NOT NULL DEFAULT '{}',
        evidence jsonb NOT NULL DEFAULT '[]', UNIQUE(execution_id,evaluator_version_id),
        FOREIGN KEY(created_from_attempt_id,execution_id,evaluator_version_id)
        REFERENCES execution_attempts(id,execution_id,evaluator_version_id),
        CHECK((status='OK' AND score IS NOT NULL AND score BETWEEN 0 AND 1 AND passed IS NOT NULL)
          OR (status IN ('ERROR','SKIPPED') AND score IS NULL AND passed IS NULL)))""")
    op.execute("""CREATE TRIGGER frozen BEFORE UPDATE OR DELETE ON evaluation_scores
        FOR EACH ROW EXECUTE FUNCTION reject_revision_change()""")
    op.execute("""CREATE FUNCTION validate_product() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE a execution_attempts%ROWTYPE; c case_executions%ROWTYPE;
        BEGIN IF TG_TABLE_NAME='case_executions' THEN
          IF OLD.prediction IS NOT NULL OR NEW.prediction_attempt_id IS NULL THEN
            RAISE EXCEPTION 'immutable prediction' USING ERRCODE='23514'; END IF;
          c=OLD; SELECT * INTO a FROM execution_attempts WHERE id=NEW.prediction_attempt_id;
          IF a.stage IS DISTINCT FROM 'ADAPTER' OR a.status IS DISTINCT FROM 'SUCCEEDED' THEN
            RAISE EXCEPTION 'invalid prediction source' USING ERRCODE='23514'; END IF;
        ELSE
          SELECT * INTO c FROM case_executions WHERE id=NEW.execution_id FOR UPDATE;
          SELECT * INTO a FROM execution_attempts WHERE id=NEW.created_from_attempt_id;
          IF (NEW.status='ERROR' AND a.status NOT IN ('FAILED','TIMED_OUT','UNKNOWN_DELIVERY'))
             OR (NEW.status IN ('OK','SKIPPED') AND a.status<>'SUCCEEDED') THEN
            RAISE EXCEPTION 'invalid score source' USING ERRCODE='23514'; END IF;
        END IF;
        IF c.status<>'RUNNING' OR c.lease_owner IS DISTINCT FROM a.lease_owner
          OR c.lease_epoch<>a.epoch OR c.lease_expires_at<=clock_timestamp() THEN
          RAISE EXCEPTION 'expired execution right' USING ERRCODE='23514'; END IF;
        RETURN NEW; END $$""")
    op.execute("""CREATE TRIGGER validate_prediction BEFORE UPDATE OF
        prediction,prediction_attempt_id
        ON case_executions FOR EACH ROW WHEN (OLD.prediction IS DISTINCT FROM NEW.prediction
        OR OLD.prediction_attempt_id IS DISTINCT FROM NEW.prediction_attempt_id)
        EXECUTE FUNCTION validate_product()""")
    op.execute("""CREATE TRIGGER validate_score BEFORE INSERT ON evaluation_scores
        FOR EACH ROW EXECUTE FUNCTION validate_product()""")
    op.execute("""CREATE FUNCTION validate_attempt() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN IF NOT EXISTS(SELECT 1 FROM case_executions c JOIN evaluation_runs r ON r.id=c.run_id
          WHERE c.id=NEW.execution_id AND r.id=NEW.run_id AND c.status='RUNNING'
          AND c.lease_owner=NEW.lease_owner AND c.lease_epoch=NEW.epoch
          AND c.lease_expires_at>clock_timestamp() AND (TG_OP='UPDATE' OR
          (NEW.stage='ADAPTER' AND c.stage='ADAPTER' AND c.prediction IS NULL) OR
          (NEW.stage='EVALUATOR' AND c.stage='SCORING' AND c.prediction IS NOT NULL))
          AND (NEW.stage='ADAPTER' OR EXISTS
          (SELECT 1 FROM task_evaluators t WHERE t.task_id=r.task_id
           AND t.evaluator_version_id=NEW.evaluator_version_id)) FOR UPDATE OF c)
          THEN RAISE EXCEPTION 'invalid attempt scope' USING ERRCODE='23514'; END IF;
          IF TG_OP='INSERT' AND EXISTS(SELECT 1 FROM execution_attempts old_step
            WHERE old_step.execution_id=NEW.execution_id AND old_step.stage=NEW.stage
            AND old_step.evaluator_version_id IS NOT DISTINCT FROM NEW.evaluator_version_id
            AND (old_step.logical_step_key,old_step.external_request_key,old_step.request_digest)
            IS DISTINCT FROM (NEW.logical_step_key,NEW.external_request_key,NEW.request_digest))
          THEN RAISE EXCEPTION 'logical call changed' USING ERRCODE='23514'; END IF;
          RETURN NEW; END $$""")
    op.execute("""CREATE TRIGGER validate_attempt BEFORE INSERT OR UPDATE ON execution_attempts
        FOR EACH ROW WHEN (NEW.status <> 'ABANDONED') EXECUTE FUNCTION validate_attempt()""")
    op.execute("""CREATE FUNCTION initial_execution_state() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN IF NEW.status<>TG_ARGV[0] THEN
        RAISE EXCEPTION 'invalid initial state' USING ERRCODE='23514'; END IF;
        RETURN NEW; END $$""")
    for table, initial in (
        ("evaluation_runs", "PENDING"),
        ("case_executions", "PENDING"),
        ("execution_attempts", "CREATED"),
    ):
        op.execute(f"""CREATE TRIGGER initial_state BEFORE INSERT ON {table}
            FOR EACH ROW EXECUTE FUNCTION initial_execution_state('{initial}')""")
    for table, index, fields in (
        ("evaluation_tasks", "project", "project_id,created_at,id"),
        ("evaluation_runs", "task", "task_id,created_at,id"),
        ("evaluation_runs", "queue", "status,created_at,id"),
        ("evaluation_runs", "project", "project_id,created_at,id"),
        ("case_executions", "claim", "run_id,status,next_retry_at,id"),
        ("execution_attempts", "step", "execution_id,logical_step_key,attempt_no"),
        ("execution_attempts", "external", "external_request_key"),
        ("evaluation_scores", "version", "evaluator_version_id,status,score"),
    ):
        op.execute(f"CREATE INDEX ix_{table}_{index} ON {table}({fields})")
    op.execute("""CREATE INDEX ix_expiring_case ON case_executions(lease_expires_at)
                  WHERE status='RUNNING'""")


def downgrade() -> None:
    op.execute("ALTER TABLE call_leases DROP COLUMN attempt_id")
    op.execute("ALTER TABLE case_executions DROP CONSTRAINT prediction_source")
    for table in (
        "evaluation_scores",
        "execution_attempts",
        "case_executions",
        "evaluation_runs",
        "task_evaluators",
        "evaluation_tasks",
    ):
        op.drop_table(table)
    op.execute("DROP FUNCTION initial_execution_state()")
    for name in (
        "validate_attempt",
        "validate_product",
        "freeze_plan_member",
        "guard_execution_attempts",
        "guard_case_executions",
        "guard_evaluation_runs",
        "guard_evaluation_tasks",
    ):
        op.execute(f"DROP FUNCTION {name}()")
    op.execute(
        "ALTER TABLE evaluation_cases DROP CONSTRAINT evaluation_cases_dataset_version_id_id_key"
    )
    op.execute("ALTER TABLE project_versions DROP CONSTRAINT project_versions_project_id_id_key")
