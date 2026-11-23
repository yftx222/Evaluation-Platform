"""Immutable evaluator snapshots and their ordered dependency edges."""

from alembic import op

revision = "0004_evaluators"
down_revision = "0003_dataset_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE evaluators (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), slug text NOT NULL UNIQUE,
        name text NOT NULL, kind text NOT NULL CHECK(kind IN
        ('rule','code','llm_judge','business','composite')), enabled boolean NOT NULL DEFAULT true,
        created_at timestamptz NOT NULL DEFAULT now())""")
    op.create_index("ix_evaluators_kind_enabled", "evaluators", ["kind", "enabled"])
    op.execute("""CREATE TABLE evaluator_versions (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        evaluator_id uuid NOT NULL REFERENCES evaluators,
        version text NOT NULL, name text NOT NULL, impl_key text NOT NULL, build text NOT NULL,
        config jsonb NOT NULL, prompt_text text, prompt_version text, provider text, model_id text,
        model_revision text, rubric_version text, revision_unverified boolean NOT NULL,
        digest text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE(evaluator_id,version))""")
    op.create_index("ix_evaluator_digest", "evaluator_versions", ["digest"])
    op.execute("""CREATE TABLE composite_members (
        parent_id uuid NOT NULL REFERENCES evaluator_versions DEFERRABLE INITIALLY DEFERRED,
        child_id uuid NOT NULL REFERENCES evaluator_versions,
        position int NOT NULL CHECK(position>=0),
        weight numeric NOT NULL CHECK(weight>0), PRIMARY KEY(parent_id,child_id),
        UNIQUE(parent_id,position), CHECK(parent_id<>child_id))""")
    op.create_index("ix_composite_child", "composite_members", ["child_id"])
    op.execute("""CREATE FUNCTION frozen_evaluator() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP='INSERT' THEN
        IF NOT EXISTS (SELECT 1 FROM evaluator_versions WHERE id=NEW.parent_id)
        THEN RETURN NEW; END IF;
      END IF;
      RAISE EXCEPTION 'published evaluator is immutable' USING ERRCODE='23514';
    END $$""")
    op.execute("""CREATE TRIGGER freeze_version BEFORE UPDATE OR DELETE ON evaluator_versions
        FOR EACH ROW EXECUTE FUNCTION frozen_evaluator()""")
    op.execute("""CREATE TRIGGER freeze_members BEFORE INSERT OR UPDATE OR DELETE
        ON composite_members
        FOR EACH ROW EXECUTE FUNCTION frozen_evaluator()""")


def downgrade() -> None:
    op.drop_table("composite_members")
    op.drop_table("evaluator_versions")
    op.drop_table("evaluators")
    op.execute("DROP FUNCTION frozen_evaluator()")
