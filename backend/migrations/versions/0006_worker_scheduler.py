"""A shared Case capacity and indexes for scheduling and recovery."""

from alembic import op

revision = "0006_worker_scheduler"
down_revision = "0005_evaluation_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE worker_capacity (
        singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
        slots integer NOT NULL CHECK(slots BETWEEN 1 AND 4096))""")
    op.execute("""CREATE INDEX ix_attempt_run_last ON execution_attempts(run_id,created_at DESC)""")
    op.execute("""CREATE INDEX ix_sched_project ON projects(last_claim_at NULLS FIRST,id)
                  WHERE enabled""")


def downgrade() -> None:
    op.drop_index("ix_sched_project", table_name="projects")
    op.drop_index("ix_attempt_run_last", table_name="execution_attempts")
    op.drop_table("worker_capacity")
