"""Preserve identities, provenance and deployment-wide admission.

Revision ID: 0004
Revises: 0003
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_DDL = """
SET LOCAL lock_timeout = '5s';
ALTER TABLE decisions ADD COLUMN source_project text;
ALTER TABLE decisions ADD COLUMN source_position integer
    CHECK (source_position >= 0);
CREATE INDEX decisions_project_idx ON decisions(source_project, id);
CREATE INDEX decisions_run_position_idx
    ON decisions(run_id, source_position, id);
ALTER TABLE actions ADD COLUMN terminal_at timestamptz;
UPDATE actions SET terminal_at = coalesce(sent_at, clock_timestamp())
    WHERE status IN ('sent', 'dead');
ALTER TABLE actions ADD CONSTRAINT actions_terminal_check CHECK
    ((status IN ('sent', 'dead')) = (terminal_at IS NOT NULL));
CREATE INDEX actions_terminal_idx ON actions(terminal_at)
    WHERE status IN ('sent', 'dead');
CREATE INDEX actions_decision_idx ON actions(decision_id, id);
-- Older writers do not supply terminal_at during a rolling deployment.
CREATE FUNCTION stamp_terminal_transition() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF NEW.status IN ('sent', 'dead') THEN
        NEW.terminal_at := coalesce(NEW.terminal_at, clock_timestamp());
    ELSE
        NEW.terminal_at := NULL;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER actions_terminal_time BEFORE UPDATE OF status, terminal_at
    ON actions FOR EACH ROW EXECUTE FUNCTION stamp_terminal_transition();
REVOKE ALL ON FUNCTION stamp_terminal_transition() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION stamp_terminal_transition() TO followup_app;

-- The table lock covers the backfill-to-trigger interval. Payload pruning
-- never removes these two opaque identifiers or recreates their messages.
LOCK TABLE actions IN SHARE ROW EXCLUSIVE MODE;
CREATE TABLE action_keys (
    idempotency_key text PRIMARY KEY,
    operation_id uuid NOT NULL UNIQUE
);
INSERT INTO action_keys SELECT idempotency_key, operation_id FROM actions;
-- Surviving decision receipts can also recover an identity whose old
-- action payload was already deleted before this migration.
INSERT INTO action_keys
    SELECT DISTINCT data->'identity'->>'idempotency_key',
        (data->'identity'->>'operation_id')::uuid
    FROM decisions
    WHERE data->'identity'->>'idempotency_key' IS NOT NULL
        AND data->'identity'->>'operation_id' IS NOT NULL
    ON CONFLICT DO NOTHING;
CREATE FUNCTION preserve_action_identity() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    INSERT INTO public.action_keys VALUES
        (NEW.idempotency_key, NEW.operation_id) ON CONFLICT DO NOTHING;
    IF NOT FOUND THEN RETURN NULL; END IF;
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION preserve_action_identity() FROM PUBLIC;
CREATE TRIGGER actions_identity BEFORE INSERT ON actions
    FOR EACH ROW EXECUTE FUNCTION preserve_action_identity();
REVOKE ALL ON action_keys FROM PUBLIC, followup_app, followup_maintenance;
GRANT SELECT ON action_keys TO followup_app;

CREATE TABLE api_rate_buckets (
    namespace text NOT NULL CHECK (namespace IN ('requests', 'failures')),
    actor_hash text NOT NULL CHECK (actor_hash ~ '^[a-f0-9]{64}$'),
    reset_at timestamptz NOT NULL,
    used integer NOT NULL CHECK (used > 0),
    PRIMARY KEY (namespace, actor_hash)
);
CREATE INDEX api_rate_expiry_idx ON api_rate_buckets(namespace, reset_at);
CREATE FUNCTION admit_api_request(
    p_namespace text, p_actor text, p_capacity integer,
    p_period double precision, p_subjects integer
) RETURNS boolean LANGUAGE plpgsql STRICT SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE v_now timestamptz; v_used integer;
BEGIN
    IF current_setting('transaction_isolation') <> 'read committed'
       OR p_namespace NOT IN ('requests', 'failures')
       OR p_actor !~ '^[a-f0-9]{64}$'
       OR p_capacity NOT BETWEEN 1 AND 1000000
       OR NOT (p_period > 0 AND p_period <= 86400)
       OR p_subjects NOT BETWEEN 1 AND 100000 THEN
        RAISE EXCEPTION 'invalid admission configuration';
    END IF;
    -- One bounded namespace lock also protects the cardinality check.
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'followup:api:' || p_namespace, 0));
    v_now := clock_timestamp();
    DELETE FROM public.api_rate_buckets
        WHERE namespace = p_namespace AND reset_at <= v_now;
    IF NOT EXISTS (SELECT FROM public.api_rate_buckets
        WHERE namespace = p_namespace AND actor_hash = p_actor)
       AND (SELECT count(*) FROM public.api_rate_buckets
        WHERE namespace = p_namespace) >= p_subjects THEN
        RETURN false;
    END IF;
    INSERT INTO public.api_rate_buckets AS b VALUES
        (p_namespace, p_actor, v_now + p_period * interval '1 second', 1)
    ON CONFLICT (namespace, actor_hash) DO UPDATE SET used = b.used + 1
        WHERE b.used < p_capacity
    RETURNING used INTO v_used;
    RETURN v_used IS NOT NULL;
END $$;
REVOKE ALL ON api_rate_buckets FROM PUBLIC, followup_app,
    followup_maintenance;
REVOKE ALL ON FUNCTION admit_api_request(text,text,integer,
    double precision,integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admit_api_request(text,text,integer,
    double precision,integer) TO followup_app;

CREATE TABLE recipient_days (
    recipient_hash text NOT NULL,
    day date NOT NULL,
    used integer NOT NULL CHECK (used > 0),
    PRIMARY KEY (recipient_hash, day)
);
CREATE TABLE recipient_admissions (
    operation_id uuid NOT NULL,
    day date NOT NULL,
    recipient_hash text NOT NULL,
    PRIMARY KEY (operation_id, day)
);
CREATE INDEX recipient_days_expiry_idx
    ON recipient_days(day, recipient_hash);
CREATE INDEX recipient_admissions_expiry_idx
    ON recipient_admissions(day, operation_id);
CREATE FUNCTION admit_delivery(
    p_action uuid, p_token uuid, p_now timestamptz, p_limit integer
) RETURNS boolean LANGUAGE plpgsql STRICT SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE v_operation uuid; v_recipient text; v_day date; v_used integer;
BEGIN
    IF current_setting('transaction_isolation') <> 'read committed'
       OR p_limit NOT BETWEEN 1 AND 10000 THEN
        RAISE EXCEPTION 'invalid recipient limit';
    END IF;
    SELECT a.operation_id, encode(sha256(convert_to(jsonb_build_array(
        d.data->'decision'->'target'->>'kind',
        lower(btrim(d.data->'decision'->'target'->>'name'))
    )::text, 'UTF8')), 'hex') INTO v_operation, v_recipient
    FROM public.actions a JOIN public.decisions d ON d.id = a.decision_id
    WHERE a.id = p_action AND a.lease_token = p_token
        AND a.lease_until > p_now AND a.status = 'in_flight'
        AND d.data->'decision'->'target'->>'name' IS NOT NULL
    FOR UPDATE OF a;
    IF NOT FOUND THEN RETURN false; END IF;
    v_day := (clock_timestamp() AT TIME ZONE 'UTC')::date;
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'followup:recipient:' || v_recipient || ':' || v_day::text, 0));
    IF EXISTS (SELECT FROM public.recipient_admissions
        WHERE operation_id = v_operation AND day = v_day
            AND recipient_hash = v_recipient) THEN
        RETURN true;
    END IF;
    INSERT INTO public.recipient_days AS b VALUES (v_recipient, v_day, 1)
    ON CONFLICT (recipient_hash, day) DO UPDATE SET used = b.used + 1
        WHERE b.used < p_limit RETURNING used INTO v_used;
    IF v_used IS NULL THEN RETURN false; END IF;
    INSERT INTO public.recipient_admissions VALUES
        (v_operation, v_day, v_recipient);
    RETURN true;
END $$;
REVOKE ALL ON recipient_days, recipient_admissions FROM PUBLIC,
    followup_app, followup_maintenance;
REVOKE ALL ON FUNCTION admit_delivery(uuid,uuid,timestamptz,integer)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admit_delivery(uuid,uuid,timestamptz,integer)
    TO followup_app;

CREATE FUNCTION prune_admission_history(p_before timestamptz, p_limit integer)
RETURNS integer LANGUAGE plpgsql STRICT SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE v_day date; v_first integer; v_second integer;
BEGIN
    IF p_limit NOT BETWEEN 1 AND 10000 THEN
        RAISE EXCEPTION 'invalid retention bound';
    END IF;
    v_day := (least(p_before, clock_timestamp() - interval '2 days')
        AT TIME ZONE 'UTC')::date;
    DELETE FROM public.recipient_admissions WHERE (operation_id, day) IN (
        SELECT operation_id, day FROM public.recipient_admissions
        WHERE day < v_day ORDER BY day, operation_id LIMIT p_limit
        FOR UPDATE SKIP LOCKED);
    GET DIAGNOSTICS v_first = ROW_COUNT;
    DELETE FROM public.recipient_days WHERE (recipient_hash, day) IN (
        SELECT recipient_hash, day FROM public.recipient_days
        WHERE day < v_day ORDER BY day, recipient_hash LIMIT p_limit
        FOR UPDATE SKIP LOCKED);
    GET DIAGNOSTICS v_second = ROW_COUNT;
    RETURN v_first + v_second;
END $$;
REVOKE ALL ON FUNCTION prune_admission_history(timestamptz,integer)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION prune_admission_history(timestamptz,integer)
    TO followup_maintenance;
"""


def upgrade() -> None:
    """Backfill identity atomically; expose narrow functions."""
    op.execute(_DDL)


def downgrade() -> None:
    """Allow empty rollback; refuse to lose payload-less identities."""
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT FROM action_keys k WHERE NOT EXISTS (
                SELECT FROM actions a
                WHERE a.idempotency_key = k.idempotency_key
            )) THEN RAISE EXCEPTION 'retained identities prevent downgrade';
            END IF;
        END $$;
        DROP TRIGGER actions_identity ON actions;
        DROP FUNCTION preserve_action_identity();
        DROP FUNCTION admit_api_request(text,text,integer,
            double precision,integer);
        DROP FUNCTION admit_delivery(uuid,uuid,timestamptz,integer);
        DROP FUNCTION prune_admission_history(timestamptz,integer);
        DROP TABLE action_keys, api_rate_buckets, recipient_days,
            recipient_admissions;
        DROP INDEX actions_decision_idx;
        DROP TRIGGER actions_terminal_time ON actions;
        DROP FUNCTION stamp_terminal_transition();
        ALTER TABLE actions DROP COLUMN terminal_at;
        ALTER TABLE decisions DROP COLUMN source_position;
        ALTER TABLE decisions DROP COLUMN source_project;
    """)
