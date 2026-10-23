"""Deduplicate source states independently of language-model decisions.

Revision ID: 0006
Revises: 0005
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

_DDL = """
SET LOCAL lock_timeout = '5s';
LOCK TABLE actions IN SHARE ROW EXCLUSIVE MODE;
-- Opaque pre-upgrade tombstones cannot identify a source state once both
-- payload and decision have expired. Refuse that upgrade rather than
-- silently re-notifying those states. Operators must restore their source
-- receipt inventory before retrying this migration.
DO $$ BEGIN
    IF EXISTS (
        SELECT 1 FROM action_keys AS k
        WHERE NOT EXISTS (
            SELECT 1 FROM decisions AS d
            WHERE d.data->'identity'->>'idempotency_key' = k.idempotency_key
        )
    ) THEN
        RAISE EXCEPTION
            'source-state inventory required for retained identities';
    END IF;
END $$;
-- The stage keeps time-driven escalation possible: an unchanged task
-- that crosses the escalation threshold still reaches its manager once.
-- It comes from the policy baseline, never from the model's choice.
CREATE FUNCTION notification_stage(data jsonb) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE coalesce(
            data #>> '{baseline,kind}', data #>> '{decision,kind}'
        ) WHEN 'escalate' THEN 'escalate' ELSE 'notify' END
$$;
CREATE TABLE notification_slots (
    task_id text NOT NULL,
    project text NOT NULL,
    state_fingerprint text NOT NULL,
    stage text NOT NULL CHECK (stage IN ('notify', 'escalate')),
    PRIMARY KEY (task_id, project, state_fingerprint, stage)
);
-- Existing receipts protect deployments upgrading with a populated outbox,
-- including messages whose payload was pruned but receipt still survives.
INSERT INTO notification_slots
    SELECT DISTINCT task_id,
        coalesce(source_project, data #>> '{decision,target,project}', ''),
        state_fingerprint,
        notification_stage(data)
    FROM decisions
    WHERE outcome IN ('enqueued', 'duplicate_suppressed')
        AND data->'identity'->>'idempotency_key' IS NOT NULL
    ON CONFLICT DO NOTHING;
REVOKE ALL ON notification_slots
    FROM PUBLIC, followup_app, followup_maintenance;

-- Replace the existing trigger function so older writers also obey the
-- source-state boundary during a rolling deployment. Both tombstones are
-- transactional with the decision and action, and neither is prunable.
CREATE OR REPLACE FUNCTION preserve_action_identity() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE v_task text; v_project text; v_state text; v_stage text;
    v_new_slot boolean;
BEGIN
    SELECT task_id,
        coalesce(source_project, data #>> '{decision,target,project}', ''),
        state_fingerprint,
        public.notification_stage(data)
    INTO STRICT v_task, v_project, v_state, v_stage
    FROM public.decisions WHERE id = NEW.decision_id;
    INSERT INTO public.notification_slots
        VALUES (v_task, v_project, v_state, v_stage) ON CONFLICT DO NOTHING;
    v_new_slot := FOUND;
    INSERT INTO public.action_keys VALUES
        (NEW.idempotency_key, NEW.operation_id) ON CONFLICT DO NOTHING;
    IF NOT FOUND OR NOT v_new_slot THEN RETURN NULL; END IF;
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION preserve_action_identity() FROM PUBLIC;
REVOKE ALL ON FUNCTION notification_stage(jsonb) FROM PUBLIC;
"""


def upgrade() -> None:
    """Backfill and atomically enforce a durable notification slot."""
    op.execute(_DDL)


def downgrade() -> None:
    """Allow rollback only before any notification slot was occupied."""
    op.execute("""
    DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM notification_slots) THEN
            RAISE EXCEPTION 'retained identities prevent downgrade';
        END IF;
    END $$;
    CREATE OR REPLACE FUNCTION preserve_action_identity() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = pg_catalog, public, pg_temp AS $$
    BEGIN
        INSERT INTO public.action_keys VALUES
            (NEW.idempotency_key, NEW.operation_id) ON CONFLICT DO NOTHING;
        IF NOT FOUND THEN RETURN NULL; END IF;
        RETURN NEW;
    END $$;
    DROP TABLE notification_slots;
    DROP FUNCTION notification_stage(jsonb);
    """)
