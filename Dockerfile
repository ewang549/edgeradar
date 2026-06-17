# EdgeRadar app image.
#
# Uses uv (https://docs.astral.sh/uv/) — a fast, Rust-based Python package &
# environment manager that replaces pip + venv + pip-tools. We copy the prebuilt
# uv binary from its official image, then install the project into a venv inside
# the container. Dependencies are layered before the source copy so Docker can
# cache them across rebuilds.

FROM python:3.11-slim

# Bring in the uv binary.
COPY --from=ghcr.io/astral-sh/uv:0.4 /uv /usr/local/bin/uv

WORKDIR /app

# IMPORTANT: keep the virtualenv OUTSIDE /app. docker-compose bind-mounts the
# project folder onto /app for live editing, which would otherwise shadow (hide)
# a venv built at /app/.venv and make the `edgeradar` command disappear at runtime.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# Install dependencies first (better layer caching).
COPY pyproject.toml README.md ./
COPY src ./src
RUN uv sync --extra dev

# Source is also bind-mounted in docker-compose for live development.
CMD ["sleep", "infinity"]
