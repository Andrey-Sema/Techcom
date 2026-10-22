# Multi-stage build: "runtime" is the default and the only image that is
# ever published; "mock" exists for compose demos and the smoke job.
# Base images are pinned by digest; Dependabot proposes updates.
ARG PYTHON_IMAGE=python:3.14-slim-trixie@sha256:0514bc76712d64f8bb48adcb95b1ab0d88c69fee95d62fe4c18ee948bee9b1e1
ARG UV_IMAGE=astral/uv:0.12.23@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21

FROM ${UV_IMAGE} AS uv

# ---------- builder: resolve and install locked dependencies ----------
FROM ${PYTHON_IMAGE} AS builder
COPY --from=uv /uv /uvx /bin/
# An optional extra CA bundle for TLS-intercepting proxies is mounted as
# a build secret ("ca_bundle"); it never lands in a layer. uv ignores the
# variable when the file is absent.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    SSL_CERT_FILE=/run/secrets/ca_bundle
WORKDIR /src
# Dependencies first: this layer stays cached until uv.lock changes.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=secret,id=ca_bundle,required=false \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=mocks/pyproject.toml,target=mocks/pyproject.toml \
    uv sync --locked --no-dev --no-install-workspace
COPY . .
# Install only the product package: no mocks, no dev tools.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=secret,id=ca_bundle,required=false \
    uv sync --locked --no-dev --no-editable --package techcom-followup

# ---------- mock: demo and test receiver only, never deployed ----------
FROM builder AS mock-builder
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=secret,id=ca_bundle,required=false \
    uv sync --locked --no-dev --no-editable --package techcom-mocks

FROM ${PYTHON_IMAGE} AS mock
RUN python -m pip uninstall --yes --no-cache-dir pip \
 && groupadd --system --gid 10002 mock \
 && useradd --system --uid 10002 --gid mock \
        --no-create-home --shell /usr/sbin/nologin mock \
 && install --directory --owner mock --group mock --mode 0700 /var/lib/mock
COPY --from=mock-builder /opt/venv /opt/venv
COPY fixtures /app/fixtures
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FOLLOWUP_MOCK_FIXTURE=/app/fixtures/tasks.json \
    FOLLOWUP_MOCK_JOURNAL=/var/lib/mock/delivery_log.jsonl
WORKDIR /app
USER 10002:10002
VOLUME ["/var/lib/mock"]
EXPOSE 8081
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8081/v1/tasks', timeout=2)"]
# Inside the container the mock listens on all interfaces; compose and
# the tests publish the port on 127.0.0.1 only. One worker per journal.
ENTRYPOINT ["uvicorn", "techcom_mocks.app:create_app", "--factory", \
            "--host", "0.0.0.0", "--port", "8081", "--workers", "1"]

# ---------- runtime (last stage, so it is the default target) ----------
# No compiler, no uv, no source tree.
FROM ${PYTHON_IMAGE} AS runtime
# The base image's pip is unused at runtime and vendors its own
# (often vulnerable) copies of urllib3 and msgpack: remove it.
RUN python -m pip uninstall --yes --no-cache-dir pip \
 && groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app \
        --no-create-home --shell /usr/sbin/nologin app
# Root owns every file, so the service user cannot modify its own code.
COPY --from=builder /opt/venv /opt/venv
COPY alembic.ini /app/alembic.ini
COPY migrations /app/migrations
COPY config /app/config
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PYTHONBREAKPOINT=0
WORKDIR /app
USER 10001:10001
# The "followup" entry point and its HEALTHCHECK arrive with the CLI
# (M6a) and the REST API (M6b); until then the image runs migrations:
#   docker run --rm -e FOLLOWUP_DATABASE_URL=... IMAGE alembic upgrade head
CMD ["alembic", "current"]
