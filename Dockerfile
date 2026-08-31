# One artifact: the API and the built SPA in a single image, served from one
# origin. That removes CORS, the second deploy target, and the `/api`
# prefix-strip failure mode — the rewrite in frontend/vite.config.ts is
# dev-server only, so a split deployment has to reimplement it somewhere and
# 404s completely if it is missed.

FROM node:22-slim AS frontend
WORKDIR /app/frontend
# Copied separately so a source-only change does not re-run npm ci.
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
# `tsc -b && vite build` — the real typecheck for frontend code, so a type
# error fails the image build rather than shipping.
RUN npm run build


# 3.14-slim matches the dev venv (3.14.3), not pyproject's `>=3.11` floor.
# The floor is what the code claims to support; 3.14 is what the suite has
# actually been run against, and an image should not be the first place a
# version difference shows up.
FROM python:3.14-slim
WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

COPY pyproject.toml ./
# alembic.ini is needed by the release command, which runs `alembic upgrade
# head` before a new version goes live. The migrations themselves live under
# arbys/db/migrations and arrive with the package; without this file alembic
# cannot find them and the release fails with "No config file found".
COPY alembic.ini ./
COPY arbys/ ./arbys/
# `pip install -e .` against pyproject, NOT requirements.txt — that file listed
# pandas, numpy and requests, none of which this project uses. It has been
# deleted rather than regenerated.
RUN pip install --no-cache-dir -e .

COPY --from=frontend /app/frontend/dist ./frontend/dist

EXPOSE 8000

# --workers 1 is already the default; stated because it is load-bearing. Each
# worker would hold its own quote book and its own auto-trader, so a second one
# is a second bot inside a single machine — the exact thing the advisory lock
# and the single-machine fly.toml exist to prevent, arriving by a route neither
# of them can see.
CMD ["uvicorn", "arbys.backend.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
