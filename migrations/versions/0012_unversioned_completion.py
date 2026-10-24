"""Let an unversioned completion retire unsent work.

After a known source version, an observation without a version and
with different facts was always ``stale_suppressed``. When those facts
say the task needs no action (for example Done without
``last_update``), that fence kept the obsolete reminder deliverable.
Suppressing a no-action decision cannot prevent a message; it can only
keep an obsolete one alive. Such decisions are now accepted: they
retire unsent work and record their fingerprint, but never advance the
version watermark. Unversioned changes that ask for an action stay
fenced.

Revision ID: 0012
Revises: 0011
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

_QUIET = "AND NEW.data #>> '{decision,kind}' <> 'no_action'"

_ACCEPT = """
CREATE OR REPLACE FUNCTION accept_source_version() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    v_project text := public.decision_scope(NEW.source_project, NEW.data);
    v_updated timestamptz :=
        (NEW.data #>> '{{source_version,updated_at}}')::timestamptz;
    v_date boolean := coalesce(
        (NEW.data #>> '{{source_version,date_only}}')::boolean, false);
    v_old public.source_versions%ROWTYPE;
    v_day date := (v_updated AT TIME ZONE 'UTC')::date;
    v_old_day date;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'source_version:' ||
        json_build_array(NEW.task_id, v_project)::text, 0));
    -- Replaying a task inside one run must not advance its watermark.
    IF EXISTS (SELECT FROM public.decisions
        WHERE run_id = NEW.run_id AND task_id = NEW.task_id) THEN
        RETURN NEW;
    END IF;
    SELECT * INTO v_old FROM public.source_versions
        WHERE task_id = NEW.task_id AND project = v_project;
    v_old_day := (v_old.updated_at AT TIME ZONE 'UTC')::date;
    IF v_updated IS NULL AND v_old.updated_at IS NULL THEN
        RETURN NEW;
    END IF;
    IF v_old.updated_at IS NOT NULL AND (
        (v_updated IS NULL AND v_old.fingerprint <> NEW.state_fingerprint
            {quiet})
        OR v_day < v_old_day
        OR (v_day = v_old_day AND NOT v_date AND NOT v_old.date_only
            AND v_updated < v_old.updated_at)
    ) THEN
        NEW.outcome := 'stale_suppressed';
        RETURN NEW;
    END IF;
    -- Same-day date-only records have no intra-day authority. Keep the
    -- precise watermark, but allow observed changes at coarse/equal versions.
    IF v_old.updated_at IS NULL OR v_day > v_old_day OR (
        v_day = v_old_day AND NOT v_date
        AND (v_old.date_only OR v_updated > v_old.updated_at)
    ) THEN
        v_old.updated_at := v_updated;
        v_old.date_only := v_date;
    END IF;
    INSERT INTO public.source_versions VALUES
        (NEW.task_id, v_project, v_old.updated_at,
         coalesce(v_old.date_only, false), NEW.state_fingerprint)
    ON CONFLICT (task_id, project) DO UPDATE SET
        updated_at = excluded.updated_at, date_only = excluded.date_only,
        fingerprint = excluded.fingerprint;
    RETURN NEW;
END $$;
"""


def upgrade() -> None:
    """Accept unversioned no-action observations."""
    op.execute(_ACCEPT.format(quiet=_QUIET))


def downgrade() -> None:
    """Fence every unversioned change again, as in revision 0011."""
    op.execute(_ACCEPT.format(quiet=""))
