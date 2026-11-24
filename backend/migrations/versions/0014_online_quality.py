"""Online inspections, shadow pairs, experiment observations and isolated rescoring."""

from alembic import op

revision = "0014_online_quality"
down_revision = "0013_quality_workflows"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE datasets ADD shadow_project_id uuid REFERENCES projects")
    op.execute("""CREATE TABLE inspection_plans(
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),project_id uuid NOT NULL REFERENCES projects,
        environment text NOT NULL,task_id uuid NOT NULL REFERENCES evaluation_tasks,period_s
        int NOT NULL CHECK(period_s>0),
        threshold numeric NOT NULL,enabled boolean NOT NULL DEFAULT true,next_at timestamptz
        NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now())""")
    op.execute("""CREATE TABLE inspection_windows(plan_id uuid NOT NULL REFERENCES inspection_plans,
        slot timestamptz NOT NULL,run_id uuid NOT NULL UNIQUE REFERENCES evaluation_runs,result
        jsonb,
        PRIMARY KEY(plan_id,slot))""")
    op.execute("""CREATE TABLE shadow_plans(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        project_id uuid NOT NULL REFERENCES projects,environment text NOT NULL,task_id uuid NOT
        NULL REFERENCES evaluation_tasks,
        sample_rate numeric NOT NULL, max_parallel int NOT NULL,timeout_s int NOT NULL,enabled
        boolean NOT NULL DEFAULT true)""")
    op.execute("""CREATE TABLE shadow_pairs(plan_id uuid NOT NULL REFERENCES shadow_plans,
        trace_id uuid NOT NULL REFERENCES production_traces, run_id uuid NOT NULL UNIQUE
        REFERENCES evaluation_runs,
        digest text NOT NULL,decision text,review_ref text,created_at timestamptz NOT NULL
        DEFAULT now(),
        PRIMARY KEY(plan_id,trace_id))""")
    op.execute("""CREATE TABLE quality_experiments(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        project_id uuid NOT NULL REFERENCES projects,environment text NOT NULL,control_id uuid
        NOT NULL REFERENCES project_versions,
        candidate_id uuid NOT NULL REFERENCES project_versions,ratio numeric NOT NULL,salt uuid
        NOT NULL DEFAULT gen_random_uuid(),
        starts_at timestamptz NOT NULL,ends_at timestamptz NOT NULL,max_sessions int NOT NULL,
        enabled boolean NOT NULL DEFAULT true,assigned int NOT NULL DEFAULT 0)""")
    op.execute("""CREATE TABLE experiment_sessions(experiment_id uuid NOT NULL REFERENCES
        quality_experiments,
        conversation_id text NOT NULL,variant text NOT NULL,deployment text NOT NULL,
        PRIMARY KEY(experiment_id,conversation_id))""")
    op.execute("""CREATE TABLE experiment_observations(
        trace_id uuid PRIMARY KEY REFERENCES production_traces, experiment_id uuid NOT NULL,
        conversation_id text NOT NULL,result jsonb NOT NULL,
        FOREIGN KEY(experiment_id,conversation_id) REFERENCES experiment_sessions)""")
    op.execute("""CREATE TABLE quality_rescores(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        execution_id uuid NOT NULL REFERENCES case_executions,version_id uuid NOT NULL
        REFERENCES evaluator_versions,
        request_key text NOT NULL,result jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT
        now(),
        UNIQUE(execution_id,request_key))""")


def downgrade() -> None:
    op.drop_column("datasets", "shadow_project_id")
    for table in (
        "quality_rescores",
        "experiment_observations",
        "experiment_sessions",
        "quality_experiments",
        "shadow_pairs",
        "shadow_plans",
        "inspection_windows",
        "inspection_plans",
    ):
        op.drop_table(table)
