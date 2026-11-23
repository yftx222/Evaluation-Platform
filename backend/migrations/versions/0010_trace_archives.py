"""Keep the identity of sealed traces taken out of online service."""

from alembic import op

revision = "0010_trace_archives"
down_revision = "0009_trace_capabilities"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE trace_archives (
        trace_id uuid PRIMARY KEY REFERENCES traces,
        archived_at timestamptz NOT NULL DEFAULT clock_timestamp())""")
    op.execute("""CREATE FUNCTION verify_trace_archive() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN IF NOT EXISTS (SELECT 1 FROM traces WHERE id=NEW.trace_id AND status<>'OPEN')
        THEN RAISE EXCEPTION 'open trace cannot be archived' USING ERRCODE='23514'; END IF;
        RETURN NEW; END $$""")
    op.execute("""CREATE TRIGGER closed_only BEFORE INSERT ON trace_archives
        FOR EACH ROW EXECUTE FUNCTION verify_trace_archive()""")


def downgrade() -> None:
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM trace_archives) THEN
        RAISE EXCEPTION 'restore archived traces before downgrade'; END IF; END $$""")
    op.drop_table("trace_archives")
    op.execute("DROP FUNCTION verify_trace_archive()")
