FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY src/requirements*.txt /tmp/requirements/
RUN pip install --no-cache-dir -r /tmp/requirements/requirements-service.txt
COPY src /app/src
COPY deployment /app/deployment
RUN useradd --uid 10001 --create-home appuser && mkdir -p /state && chown appuser:appuser /state
USER appuser
EXPOSE 8082
CMD ["gunicorn", "--bind", "0.0.0.0:8082", "--workers", "1", "--threads", "4", "--access-logfile", "-", "--error-logfile", "-", "src.service.api:app"]
