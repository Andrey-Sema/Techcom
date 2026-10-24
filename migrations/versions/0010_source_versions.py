"""Fence provably obsolete source snapshots before outbox mutations.

Revision ID: 0010
Revises: 0009
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

_DDL = """
SET LOCAL lock_timeout = '5s';
ALTER TABLE decisions DROP CONSTRAINT decisions_outcome_check;
-- Existing rows satisfy the narrower old check, so skip the full-table
-- scan under ACCESS EXCLUSIVE; new and updated rows are still checked.
ALTER TABLE decisions ADD CONSTRAINT decisions_outcome_check
    CHECK (outcome IN (
        'none','enqueued','duplicate_suppressed','stale_suppressed'))
    NOT VALID;
-- Non-prunable, payload-free watermark, including quiet decisions.
-- Older receipts never stored last_update: their source order is unknown
-- until the first versioned observation after upgrade, not fabricated.
CREATE TABLE source_versions (
    task_id text NOT NULL, project text NOT NULL,
    updated_at timestamptz, date_only boolean NOT NULL,
    fingerprint text NOT NULL,
    PRIMARY KEY (task_id, project)
);
REVOKE ALL ON source_versions FROM PUBLIC, followup_app, followup_maintenance;

CREATE FUNCTION accept_source_version() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    v_project text := coalesce(NEW.source_project,
        NEW.data #>> '{decision,target,project}', '');
    v_updated timestamptz :=
        (NEW.data #>> '{source_version,updated_at}')::timestamptz;
    v_date boolean := coalesce(
        (NEW.data #>> '{source_version,date_only}')::boolean, false);
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
REVOKE ALL ON FUNCTION accept_source_version() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION accept_source_version() TO followup_app;
CREATE TRIGGER decisions_source_version BEFORE INSERT ON decisions
    FOR EACH ROW EXECUTE FUNCTION accept_source_version();

CREATE OR REPLACE FUNCTION supersede_prior_actions() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF NEW.outcome = 'stale_suppressed' THEN RETURN NEW; END IF;
    UPDATE public.actions AS a SET superseded = true
    FROM public.decisions AS d
    WHERE a.decision_id = d.id AND d.task_id = NEW.task_id
        AND coalesce(d.source_project,
            d.data #>> '{decision,target,project}', '') =
            coalesce(NEW.source_project,
            NEW.data #>> '{decision,target,project}', '')
        AND (d.state_fingerprint <> NEW.state_fingerprint
            OR NEW.data #>> '{decision,kind}' = 'no_action')
        AND NOT a.superseded
        AND a.status NOT IN ('sent', 'dead');
    RETURN NEW;
END $$;
-- Fence older application writers that still try to enqueue a stale receipt.
CREATE FUNCTION reject_stale_action() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF EXISTS (SELECT FROM public.decisions
        WHERE id = NEW.decision_id AND outcome = 'stale_suppressed') THEN
        RETURN NULL;
    END IF;
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION reject_stale_action() FROM PUBLIC;
-- Trigger names execute alphabetically: reject before identity tombstones.
CREATE TRIGGER actions_00_source_version BEFORE INSERT ON actions
    FOR EACH ROW EXECUTE FUNCTION reject_stale_action();
"""


def upgrade() -> None:
    """Install a transactional source watermark."""
    op.execute(_DDL)


def downgrade() -> None:
    """Refuse to silently discard a populated ordering guarantee."""
    op.execute("""
    DO $$ BEGIN
        IF EXISTS (SELECT FROM source_versions) THEN
            RAISE EXCEPTION 'retained source versions prevent downgrade';
        END IF;
    END $$;
    DROP TRIGGER actions_00_source_version ON actions;
    DROP FUNCTION reject_stale_action();
    DROP TRIGGER decisions_source_version ON decisions;
    DROP FUNCTION accept_source_version();
    DROP TABLE source_versions;
    ALTER TABLE decisions DROP CONSTRAINT decisions_outcome_check;
    ALTER TABLE decisions ADD CONSTRAINT decisions_outcome_check
        CHECK (outcome IN ('none','enqueued','duplicate_suppressed'));
    """)
