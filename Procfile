web: gunicorn app.server:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 60
worker: python -m app.scheduler run
