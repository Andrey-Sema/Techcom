"""Scope quiet decisions by their task project.

A ``no_action`` decision has no recipient, so tasks without project
authority (unknown or noncanonical project) scoped it to the empty
string, while their actions used the recipient's project label. A Done
snapshot then neither retired pending reminders nor shared their source
watermark. The task label now fills in only where the recipient project
is absent, so actionable scopes and notification slots are unchanged.

Revision ID: 0011
Revises: 0010
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

_SCOPE = "public.decision_scope({source}, {data})"
_LEGACY_SCOPE = (
    "coalesce({source}, {data} #>> '{{decision,target,project}}', '')"
)

_FUNCTIONS = """
CREATE OR REPLACE FUNCTION supersede_prior_actions() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF NEW.outcome = 'stale_suppressed' THEN RETURN NEW; END IF;
    UPDATE public.actions AS a SET superseded = true
    FROM public.decisions AS d
    WHERE a.decision_id = d.id AND d.task_id = NEW.task_id
        AND {old_scope} = {new_scope}
        AND (d.state_fingerprint <> NEW.state_fingerprint
            OR NEW.data #>> '{{decision,kind}}' = 'no_action')
        AND NOT a.superseded
        AND a.status NOT IN ('sent', 'dead');
    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION accept_source_version() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    v_project text := {new_scope};
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
        (v_updated IS NULL AND v_old.fingerprint <> NEW.state_fingerprint)
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


def _functions(scope: str) -> str:
    return _FUNCTIONS.format(
        old_scope=scope.format(source="d.source_project", data="d.data"),
        new_scope=scope.format(source="NEW.source_project", data="NEW.data"),
    )


def upgrade() -> None:
    """Scope recipient-less decisions by the task label."""
    op.execute("""
    SET LOCAL lock_timeout = '5s';
    CREATE FUNCTION decision_scope(p_source text, p_data jsonb)
    RETURNS text LANGUAGE sql IMMUTABLE AS $$
        SELECT coalesce(p_source, p_data #>> '{decision,target,project}',
            p_data ->> 'task_project', '')
    $$;
    REVOKE ALL ON FUNCTION decision_scope(text, jsonb) FROM PUBLIC;
    """)
    op.execute(_functions(_SCOPE))


def downgrade() -> None:
    """Restore the recipient-only scope of revision 0010."""
    op.execute(_functions(_LEGACY_SCOPE))
    op.execute("DROP FUNCTION decision_scope(text, jsonb);")
