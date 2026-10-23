"""Retire unsent actions when a newer task snapshot makes them stale.

Unknown and in-flight operations still need reconciliation: a write may
already have committed. Their superseded bit prevents a later retry from
releasing an obsolete notification. Identity tombstones remain intact.

Revision ID: 0009
Revises: 0008
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

_DDL = """
SET LOCAL lock_timeout = '5s';
ALTER TABLE actions ADD COLUMN superseded boolean NOT NULL DEFAULT false;
CREATE FUNCTION retire_superseded_action() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    NEW.superseded := OLD.superseded OR NEW.superseded;
    IF NEW.superseded AND NEW.status IN ('pending', 'held') THEN
        NEW.status := 'dead';
        NEW.last_error := 'source_state_superseded';
        NEW.terminal_at := clock_timestamp();
        NEW.lease_token := NULL;
        NEW.lease_until := NULL;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER actions_superseded BEFORE UPDATE OF status, superseded
    ON actions FOR EACH ROW EXECUTE FUNCTION retire_superseded_action();
REVOKE ALL ON FUNCTION retire_superseded_action() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION retire_superseded_action() TO followup_app;

CREATE FUNCTION supersede_prior_actions() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
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
CREATE TRIGGER decisions_supersede AFTER INSERT ON decisions
    FOR EACH ROW EXECUTE FUNCTION supersede_prior_actions();
REVOKE ALL ON FUNCTION supersede_prior_actions() FROM PUBLIC;

-- Also protect queues already populated before this upgrade.
UPDATE actions AS a SET superseded = true
FROM decisions AS d
WHERE a.decision_id = d.id AND a.status NOT IN ('sent', 'dead')
    AND EXISTS (
        SELECT 1 FROM decisions AS newer
        WHERE newer.task_id = d.task_id AND newer.id > d.id
            AND coalesce(newer.source_project,
                newer.data #>> '{decision,target,project}', '') =
                coalesce(d.source_project,
                d.data #>> '{decision,target,project}', '')
            AND (newer.state_fingerprint <> d.state_fingerprint
                OR newer.data #>> '{decision,kind}' = 'no_action')
    );
"""


def upgrade() -> None:
    """Cancel obsolete unsent work without losing uncertain receipts."""
    op.execute(_DDL)


def downgrade() -> None:
    """Do not discard a fence while ambiguous stale sends survive."""
    op.execute("""
    DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM actions
            WHERE superseded AND status IN ('unknown', 'in_flight')) THEN
            RAISE EXCEPTION 'superseded uncertain actions prevent downgrade';
        END IF;
    END $$;
    DROP TRIGGER decisions_supersede ON decisions;
    DROP FUNCTION supersede_prior_actions();
    DROP TRIGGER actions_superseded ON actions;
    DROP FUNCTION retire_superseded_action();
    ALTER TABLE actions DROP COLUMN superseded;
    """)
