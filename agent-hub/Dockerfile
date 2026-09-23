FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    HUB_BIND=0.0.0.0 \
    HUB_BACKEND=firestore

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
    && groupadd --gid 10001 hub \
    && useradd --uid 10001 --gid hub --no-create-home --shell /usr/sbin/nologin hub \
    && mkdir /data \
    && chown hub:hub /data
COPY --chown=hub:hub agent_hub /app/agent_hub
USER 10001:10001
EXPOSE 8080
CMD ["python", "-m", "agent_hub.server"]
