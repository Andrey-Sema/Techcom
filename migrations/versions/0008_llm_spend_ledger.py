"""Share the daily LLM budget across processes and restarts.

The in-process budget only sees its own calls. Every worker now also
reserves the worst-case cost of an attempt here before the provider is
called, and settles the actual cost afterwards (OWASP LLM10). The
check and the reservation are one conditional ``UPDATE`` on the day's
row, so concurrent workers cannot overshoot the limit together.

A reservation that is never settled (a crash mid-call) stays charged
to its day: the money may have been spent, so it is never released.
The day is the UTC date of the database clock.

Revision ID: 0008
Revises: 0007
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

_DDL = """
SET LOCAL lock_timeout = '5s';
CREATE TABLE llm_spend_days (
    day date PRIMARY KEY,
    spent_usd numeric(14, 6) NOT NULL DEFAULT 0 CHECK (spent_usd >= 0),
    reserved_usd numeric(14, 6) NOT NULL DEFAULT 0
        CHECK (reserved_usd >= 0)
);
CREATE TABLE llm_spend_reservations (
    id uuid PRIMARY KEY DEFAULT uuidv7(),
    day date NOT NULL REFERENCES llm_spend_days(day),
    amount_usd numeric(14, 6) NOT NULL CHECK (amount_usd >= 0)
);
CREATE FUNCTION reserve_llm_spend(p_amount numeric, p_limit numeric)
RETURNS uuid LANGUAGE plpgsql STRICT SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE v_day date; v_id uuid;
BEGIN
    IF p_amount < 0 OR p_amount > 1000 OR p_limit < 0 OR p_limit > 100000
    THEN
        RAISE EXCEPTION 'invalid spend bound';
    END IF;
    v_day := (clock_timestamp() AT TIME ZONE 'UTC')::date;
    INSERT INTO public.llm_spend_days (day) VALUES (v_day)
        ON CONFLICT (day) DO NOTHING;
    UPDATE public.llm_spend_days
        SET reserved_usd = reserved_usd + p_amount
        WHERE day = v_day
            AND spent_usd + reserved_usd + p_amount <= p_limit;
    IF NOT FOUND THEN RETURN NULL; END IF;
    INSERT INTO public.llm_spend_reservations (day, amount_usd)
        VALUES (v_day, p_amount) RETURNING id INTO v_id;
    RETURN v_id;
END $$;
CREATE FUNCTION settle_llm_spend(p_id uuid, p_cost numeric)
RETURNS boolean LANGUAGE plpgsql STRICT SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE v_day date; v_amount numeric;
BEGIN
    IF p_cost < 0 OR p_cost > 1000 THEN
        RAISE EXCEPTION 'invalid spend';
    END IF;
    DELETE FROM public.llm_spend_reservations WHERE id = p_id
        RETURNING day, amount_usd INTO v_day, v_amount;
    IF NOT FOUND THEN RETURN false; END IF;
    -- A reported overrun is charged in full and closes admission.
    UPDATE public.llm_spend_days
        SET reserved_usd = reserved_usd - v_amount,
            spent_usd = spent_usd + p_cost
        WHERE day = v_day;
    RETURN true;
END $$;
REVOKE ALL ON llm_spend_days, llm_spend_reservations
    FROM PUBLIC, followup_app, followup_maintenance;
REVOKE ALL ON FUNCTION reserve_llm_spend(numeric, numeric) FROM PUBLIC;
REVOKE ALL ON FUNCTION settle_llm_spend(uuid, numeric) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION reserve_llm_spend(numeric, numeric)
    TO followup_app;
GRANT EXECUTE ON FUNCTION settle_llm_spend(uuid, numeric) TO followup_app;
"""


def upgrade() -> None:
    """Create the ledger behind two narrow definer functions."""
    op.execute(_DDL)


def downgrade() -> None:
    """Drop the ledger; processes fall back to their own budgets."""
    op.execute("""
    DROP FUNCTION settle_llm_spend(uuid, numeric);
    DROP FUNCTION reserve_llm_spend(numeric, numeric);
    DROP TABLE llm_spend_reservations;
    DROP TABLE llm_spend_days;
    """)
