"""Archive a reusable plan without modifying its frozen configuration."""

from alembic import op

revision = "0007_task_archive"
down_revision = "0006_worker_scheduler"
branch_labels = None
depends_on = None


def guard(columns: str) -> None:
    op.execute(f"""CREATE OR REPLACE FUNCTION guard_evaluation_tasks() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
        IF TG_OP='DELETE' OR (to_jsonb(NEW)-ARRAY[{columns}]) IS DISTINCT FROM
        (to_jsonb(OLD)-ARRAY[{columns}]) THEN
        RAISE EXCEPTION 'frozen evaluation data' USING ERRCODE='23514'; END IF;
        RETURN NEW; END $$""")


def upgrade() -> None:
    op.execute("ALTER TABLE evaluation_tasks ADD archived_at timestamptz")
    guard("'name','description','archived_at'")


def downgrade() -> None:
    guard("'name','description'")
    op.execute("ALTER TABLE evaluation_tasks DROP COLUMN archived_at")
