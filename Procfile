# Railway / Heroku-style process definition.
# workers MUST be 1: call state (app/call_store.py) is in-process memory for now.
web: gunicorn app.server:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 60

# Railway does not run this automatically -- it only starts the `web` process
# for a service by default. Deploy this as a SECOND Railway service (same repo,
# same env vars) with its Start Command overridden to the line below, so the
# scheduler runs continuously instead of via the (unused) Procfile line here.
worker: python -m app.scheduler run
