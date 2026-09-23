# Live controller image (broker + fleet controller): standard library only, no native CLI, no credential.
# Build context is the repository root:  docker build -f live-image-controller/Dockerfile .
# Same layout and checks as the live-image-controller-live-20260922c pack; sources come from this checkout.
FROM python:3.12-slim-bookworm
RUN groupadd --gid 10001 worker && useradd --uid 10001 --gid worker --no-create-home --shell /usr/sbin/nologin worker && install -d -o root -g root -m 0755 /opt/runcrew/app /run/config
# Only the agent_hub modules the broker and controller import.
COPY --chown=0:0 agent-hub/agent_hub/__init__.py agent-hub/agent_hub/adapters.py agent-hub/agent_hub/billing_policy.py \
    agent-hub/agent_hub/claude_runtime.py agent-hub/agent_hub/cloud_credential_broker.py agent-hub/agent_hub/core.py \
    agent-hub/agent_hub/credential_broker_service.py agent-hub/agent_hub/learning.py agent-hub/agent_hub/model_policy.py \
    agent-hub/agent_hub/model_runtime.py agent-hub/agent_hub/subscription_auth.py agent-hub/agent_hub/worker.py \
    /opt/runcrew/app/agent_hub/
COPY --chown=0:0 live-worker-runtime/cloud_runtime.py live-worker-runtime/dynamic_broker.py live-worker-runtime/fleet_controller.py \
    live-worker-runtime/serve.py live-worker-runtime/test_dynamic_broker.py /opt/runcrew/app/
COPY --chown=0:0 live-worker-runtime/providers/__init__.py /opt/runcrew/app/providers/
COPY --chown=0:0 live-worker-runtime/tests/__init__.py live-worker-runtime/tests/test_dynamic_broker_review.py \
    live-worker-runtime/tests/test_fleet_controller.py /opt/runcrew/app/tests/
RUN chmod -R a-w /opt/runcrew/app && find /opt/runcrew/app -name __pycache__ -prune -exec rm -rf {} +
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
WORKDIR /opt/runcrew/app
USER 10001:10001
RUN python -I -B -m unittest discover -s /opt/runcrew/app/tests -t /opt/runcrew/app -p "test_fleet_controller.py" -q
RUN python -I -B -m unittest discover -s /opt/runcrew/app/tests -t /opt/runcrew/app -p "test_dynamic_broker_review.py" -q
RUN python -I -B -m unittest discover -s /opt/runcrew/app -t /opt/runcrew/app -p "test_dynamic_broker.py" -q
RUN python -I -B /opt/runcrew/app/serve.py --check
ENTRYPOINT ["python","-I","-B","/opt/runcrew/app/serve.py"]
