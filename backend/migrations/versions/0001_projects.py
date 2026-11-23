"""Immutable project catalog baseline."""

from alembic import op

revision = "0001_projects"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE projects (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        created_at timestamptz NOT NULL DEFAULT now(),
        slug text NOT NULL UNIQUE,
        name text NOT NULL,
        kind text NOT NULL CHECK(kind IN ('agent','rag','dataagent','llm','custom')),
        enabled boolean NOT NULL DEFAULT true,
        max_parallel integer NOT NULL CHECK(max_parallel>0),
        target_max_parallel integer NOT NULL CHECK(target_max_parallel>0),
        rpm integer NOT NULL CHECK(rpm>0),
        next_call_at timestamptz,
        last_claim_at timestamptz,
        updated_at timestamptz NOT NULL DEFAULT now()
    )""")
    op.execute("""CREATE TABLE project_members (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects ON DELETE RESTRICT,
        issuer text NOT NULL,
        subject text NOT NULL,
        role text NOT NULL CHECK(role IN ('R','W','A')),
        UNIQUE(project_id,issuer,subject)
    )""")
    op.execute("""CREATE TABLE project_adapters (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects ON DELETE RESTRICT,
        revision integer NOT NULL CHECK(revision>0),
        kind text NOT NULL,
        build text NOT NULL,
        config jsonb NOT NULL,
        secret_ref text,
        secret_version text,
        digest text NOT NULL,
        CHECK((secret_ref IS NULL)=(secret_version IS NULL)),
        CHECK(secret_version <> '' AND lower(secret_version) <> 'latest' AND secret_ref <> ''),
        UNIQUE(project_id,revision),
        UNIQUE(project_id,id)
    )""")
    op.execute("""CREATE TABLE project_versions (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects ON DELETE RESTRICT,
        adapter_id uuid NOT NULL,
        label text NOT NULL,
        deployment_ref text NOT NULL,
        digest text NOT NULL,
        UNIQUE(project_id,label),
        FOREIGN KEY(project_id,adapter_id) REFERENCES project_adapters(project_id,id)
            ON DELETE RESTRICT
    )""")
    op.execute("""CREATE TABLE api_keys (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid NOT NULL REFERENCES projects ON DELETE RESTRICT,
        name text NOT NULL,
        prefix text NOT NULL UNIQUE,
        verifier text NOT NULL,
        created_by text NOT NULL,
        scopes text[] NOT NULL
            CHECK(cardinality(scopes)>0 AND scopes <@ ARRAY['project:read','results:read']::text[]),
        expires_at timestamptz NOT NULL,
        revoked_at timestamptz
    )""")
    op.execute("""CREATE TABLE audit_logs (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        created_at timestamptz NOT NULL DEFAULT now(),
        project_id uuid REFERENCES projects ON DELETE RESTRICT,
        actor text NOT NULL,
        action text NOT NULL,
        resource_type text NOT NULL,
        resource_id uuid NOT NULL,
        request_id text NOT NULL,
        changes jsonb NOT NULL
    )""")
    op.create_index("ix_projects_enabled", "projects", ["enabled", "name"])
    op.create_index(
        "ix_project_members_issuer", "project_members", ["issuer", "subject", "project_id"]
    )
    op.create_index("ix_project_versions_adapter_id", "project_versions", ["adapter_id"])
    op.create_index("ix_api_keys_project_id", "api_keys", ["project_id", "revoked_at"])
    op.create_index("ix_audit_logs_project_id", "audit_logs", ["project_id", "created_at", "id"])
    op.create_index(
        "ix_audit_logs_resource_type", "audit_logs", ["resource_type", "resource_id", "created_at"]
    )
    op.execute("""CREATE FUNCTION reject_revision_change() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'immutable revision' USING ERRCODE = '23514'; END; $$""")
    for table in ("project_adapters", "project_versions"):
        op.execute(
            f"CREATE TRIGGER immutable_revision BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_revision_change()"
        )


def downgrade() -> None:
    for table in (
        "audit_logs",
        "api_keys",
        "project_versions",
        "project_adapters",
        "project_members",
        "projects",
    ):
        op.drop_table(table)
    op.execute("DROP FUNCTION reject_revision_change()")
