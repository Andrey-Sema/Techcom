# Test cases for project-invariants.yml ("semgrep --test").
# Each "ruleid" line must be reported, each "ok" line must not.
import datetime
import os
import time
from datetime import date

import httpx
import sqlalchemy as sa
from sqlalchemy import text


def messages(template: str, facts: dict[str, str]) -> None:
    # ruleid: no-str-format-in-messages
    template.format(**facts)
    # ruleid: no-str-format-in-messages
    template.format_map(facts)
    # ruleid: no-str-format-in-messages
    "Hi %s" % facts["name"]
    # ok: no-str-format-in-messages
    template.replace("{name}", facts["name"])


def http(url: str) -> None:
    # ruleid: no-tls-verify-false
    httpx.get(url, verify=False)
    # ok: no-tls-verify-false
    httpx.get(url, verify=True)
    # ruleid: no-follow-redirects
    httpx.get(url, follow_redirects=True)
    # ok: no-follow-redirects
    httpx.get(url, follow_redirects=False)


def clock() -> None:
    # ruleid: no-naive-clock-in-core
    datetime.datetime.now(datetime.UTC)
    # ruleid: no-naive-clock-in-core
    date.today()
    # ruleid: no-naive-clock-in-core
    time.time()
    # ok: no-naive-clock-in-core
    time.monotonic()


def sql(table: str, value: str) -> None:
    # ruleid: no-raw-sql-interpolation
    text(f"SELECT * FROM {table}")
    # ruleid: no-raw-sql-interpolation
    sa.text("SELECT * FROM " + table)
    # ruleid: no-raw-sql-interpolation, no-str-format-in-messages
    text("SELECT * FROM {}".format(table))
    # ok: no-raw-sql-interpolation
    sa.text("SELECT * FROM actions WHERE id = :id").bindparams(id=value)


def logs(log, note: str) -> None:
    # ruleid: no-untrusted-text-in-logs
    log.info("llm.call", notes=note)
    # ruleid: no-untrusted-text-in-logs
    log.warning("llm.invalid", raw_output=note)
    # ok: no-untrusted-text-in-logs
    log.info("llm.call", notes_sha256="ab12", notes_chars=len(note))


def env() -> None:
    # ruleid: no-direct-env-access
    os.environ["FOLLOWUP_OPENAI_API_KEY"]
    # ruleid: no-direct-env-access
    os.getenv("FOLLOWUP_OPENAI_API_KEY")
    # ok: no-direct-env-access
    os.path.join("a", "b")
