"""Shared datasets and immutable sample snapshots."""

from alembic import op

revision = "0003_dataset_versions"
down_revision = "0002_probe_permits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE datasets(
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), created_at timestamptz NOT NULL DEFAULT now(),
      slug text NOT NULL UNIQUE, name text NOT NULL, description text,
      archived boolean NOT NULL DEFAULT false, updated_at timestamptz NOT NULL DEFAULT now())""")
    op.create_index("ix_datasets_archived_name", "datasets", ["archived", "name"])
    op.execute("""CREATE TABLE dataset_versions(
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), created_at timestamptz NOT NULL DEFAULT now(),
      dataset_id uuid NOT NULL REFERENCES datasets ON DELETE RESTRICT, base_version_id uuid,
      version integer NOT NULL CHECK(version>0), state text NOT NULL, digest text,
      published_at timestamptz, UNIQUE(dataset_id,version), UNIQUE(dataset_id,id),
      FOREIGN KEY(dataset_id,base_version_id) REFERENCES dataset_versions(dataset_id,id),
      CHECK((state='DRAFT' AND digest IS NULL AND published_at IS NULL) OR
            (state='PUBLISHED' AND digest IS NOT NULL AND published_at IS NOT NULL)))""")
    op.execute(
        "CREATE UNIQUE INDEX one_dataset_draft ON dataset_versions(dataset_id) WHERE state='DRAFT'"
    )
    op.create_index("ix_dataset_base", "dataset_versions", ["base_version_id"])
    op.execute("""CREATE TABLE evaluation_cases(
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), created_at timestamptz NOT NULL DEFAULT now(),
      dataset_version_id uuid NOT NULL REFERENCES dataset_versions ON DELETE RESTRICT,
      case_key text NOT NULL, input jsonb NOT NULL, expected jsonb NOT NULL, category text,
      difficulty text, tags text[] NOT NULL, split text NOT NULL,
      extensions jsonb NOT NULL, UNIQUE(dataset_version_id,case_key),
      CHECK(split IN ('train','validation','test')))""")
    op.create_index(
        "ix_case_group", "evaluation_cases", ["dataset_version_id", "split", "category"]
    )
    op.create_index("ix_case_tags", "evaluation_cases", ["tags"], postgresql_using="gin")
    op.execute("""CREATE FUNCTION protect_dataset_version() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP <> 'INSERT' AND OLD.state='PUBLISHED' THEN
        RAISE EXCEPTION 'published version is immutable' USING ERRCODE='23514';
      END IF;
      IF TG_OP='DELETE' THEN RETURN OLD; END IF;
      IF TG_OP='INSERT' AND NEW.state <> 'DRAFT' THEN
        RAISE EXCEPTION 'draft required' USING ERRCODE='23514';
      END IF;
      IF NEW.base_version_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM dataset_versions
        WHERE id=NEW.base_version_id AND state='PUBLISHED' AND dataset_id=NEW.dataset_id) THEN
        RAISE EXCEPTION 'invalid base' USING ERRCODE='23514';
      END IF;
      RETURN NEW;
    END $$""")
    op.execute("""CREATE TRIGGER protect_version BEFORE INSERT OR UPDATE OR DELETE
                  ON dataset_versions
                  FOR EACH ROW EXECUTE FUNCTION protect_dataset_version()""")
    op.execute("""CREATE FUNCTION protect_dataset_case() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE vid uuid;
    BEGIN
      IF TG_OP='DELETE' THEN vid=OLD.dataset_version_id; ELSE vid=NEW.dataset_version_id; END IF;
      IF TG_OP='UPDATE' AND OLD.dataset_version_id<>vid THEN
        RAISE EXCEPTION 'cannot move sample' USING ERRCODE='23514';
      END IF;
      PERFORM id FROM dataset_versions WHERE id=vid AND state='DRAFT' FOR UPDATE;
      IF NOT FOUND THEN RAISE EXCEPTION 'draft required' USING ERRCODE='23514'; END IF;
      IF TG_OP='DELETE' THEN RETURN OLD; END IF; RETURN NEW;
    END $$""")
    op.execute("""CREATE TRIGGER protect_case BEFORE INSERT OR UPDATE OR DELETE ON evaluation_cases
                  FOR EACH ROW EXECUTE FUNCTION protect_dataset_case()""")


def downgrade() -> None:
    op.drop_table("evaluation_cases")
    op.execute("DROP FUNCTION protect_dataset_case()")
    op.drop_table("dataset_versions")
    op.execute("DROP FUNCTION protect_dataset_version()")
    op.drop_table("datasets")
