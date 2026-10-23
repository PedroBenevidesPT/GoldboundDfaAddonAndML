# The pipeline worker.
#
# Deliberately a single stage over a slim base: the job is a batch worker, not
# a web app, so there is nothing to build and nothing to serve.  Dependencies
# are installed from requirements.txt rather than from the developer's
# machine, which is the point -- a container that inherits the host's packages
# is not reproducible and is how "works on my machine" gets deployed.

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# libgomp is what catboost links against; without it the trainer dies at
# import time with an opaque "cannot open shared object file".
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .

# The worker must not run as root: it writes to mounted volumes, and a file it
# creates as root is a file the developer cannot edit afterwards.
RUN useradd --create-home --uid 1000 worker \
 && mkdir -p /app/data /app/models /app/logs \
 && chown -R worker:worker /app
USER worker

# Default command is the scheduler in one-shot mode; docker-compose overrides
# it with --loop.  `--once` is the safe default because a container that
# silently loops forever is hard to tell from a hung one.
CMD ["python", "run_pipeline.py", "--once"]
