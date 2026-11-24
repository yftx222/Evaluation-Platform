"""Durable quality workflows scoped to a project and environment."""

from alembic import op

revision = "0013_quality_workflows"
down_revision = "0012_production_traces"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE evaluator_versions ADD rubrics jsonb")
    op.execute("""CREATE TABLE case_proposals (
        id uuid PRIMARY KEY, project_id uuid NOT NULL REFERENCES projects, environment text NOT
        NULL,
        subject text NOT NULL, dataset_id uuid NOT NULL REFERENCES datasets, draft_id uuid NOT NULL
        REFERENCES dataset_versions, evaluation_case_id uuid NOT NULL, case_key text NOT NULL,
        case_id uuid NOT NULL, review_id uuid NOT NULL, digest text NOT NULL, evidence_refs
        jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now())""")
    op.execute("""CREATE TABLE regression_batches (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), project_id uuid NOT NULL REFERENCES projects,
        environment text NOT NULL, task_id uuid NOT NULL REFERENCES evaluation_tasks,
        trigger_key text NOT NULL, request_digest text NOT NULL, report jsonb,
        baseline_id uuid REFERENCES regression_batches,
        created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(project_id,environment,
        trigger_key))""")
    op.execute("""CREATE TABLE regression_trials (
        batch_id uuid NOT NULL REFERENCES regression_batches, trial smallint NOT NULL
        CHECK(trial BETWEEN 1 AND 3),
        run_id uuid NOT NULL UNIQUE REFERENCES evaluation_runs, PRIMARY KEY(batch_id,trial))""")
    op.execute("""CREATE TABLE regression_results (
        batch_id uuid NOT NULL REFERENCES regression_batches, id uuid NOT NULL REFERENCES
        evaluation_cases,
        case_key text NOT NULL, result jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT
        now(),
        PRIMARY KEY(batch_id,id), UNIQUE(batch_id,case_key))""")
    op.execute("""CREATE TABLE quality_outbox (
        id uuid PRIMARY KEY, project_id uuid NOT NULL REFERENCES projects, event jsonb NOT NULL,
        state text NOT NULL DEFAULT 'PENDING' CHECK(state IN ('PENDING','SENDING','DELIVERED',
        'FAILED')),
        attempts integer NOT NULL DEFAULT 0, next_at timestamptz NOT NULL DEFAULT now(),
        lease uuid, receipt jsonb, last_error text, created_at timestamptz NOT NULL DEFAULT
        now())""")
    op.execute(
        """CREATE INDEX quality_delivery_due ON quality_outbox(next_at) WHERE state IN
            ('PENDING','SENDING')"""
    )
    op.execute(
        "CREATE INDEX regression_unfinished ON regression_batches(created_at) WHERE report IS NULL"
    )


def downgrade() -> None:
    for table in (
        "quality_outbox",
        "regression_results",
        "regression_trials",
        "regression_batches",
        "case_proposals",
    ):
        op.drop_table(table)
    op.drop_column("evaluator_versions", "rubrics")
