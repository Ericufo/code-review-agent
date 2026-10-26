FROM python:3.13-slim-bookworm@sha256:e32be0e55814a08977501ca863271d9bd02443710db8f2afaf8372e2c4508530

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt requirements-api.txt ./
RUN python -m pip install --no-cache-dir -r requirements-api.txt && python -m pip check
COPY *.py alembic.ini ./
COPY review_api/ ./review_api/
COPY migrations/ ./migrations/
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=5s --timeout=5s --start-period=30s --retries=12 CMD ["python", "-m", "review_api.healthcheck"]
CMD ["python", "-m", "review_api.container", "api"]
