# Build from the agent-hub root; credentials are runtime inputs only.
FROM python:3.12-slim-bookworm AS release
COPY deploy/grok-worker/build_native.py /build_native.py
RUN test "$(dpkg --print-architecture)" = amd64 && python /build_native.py --output /verified

FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libstdc++6 git \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 worker \
    && useradd --uid 10001 --gid worker --no-create-home --shell /usr/sbin/nologin worker \
    && install -d -o worker -g worker -m 0700 /home/worker /workspace/default \
    && install -d -o root -g root -m 0755 /opt/runcrew/app/agent_hub /run/config
COPY --from=release --chown=root:root /verified/ /opt/runcrew/grok/
COPY --chown=root:root agent_hub/*.py /opt/runcrew/app/agent_hub/
COPY --chown=root:root deploy/grok-worker/entrypoint.py /opt/runcrew/entrypoint.py
COPY --chown=root:root deploy/grok-worker/requirements.toml /etc/grok/requirements.toml
RUN chmod -R go-w /opt/runcrew
ENV HOME=/home/worker PATH=/usr/local/bin:/usr/bin:/bin LANG=C.UTF-8 TMPDIR=/tmp
WORKDIR /workspace/default
USER 10001:10001
RUN python -I -B /opt/runcrew/entrypoint.py --native-check
ENTRYPOINT ["python", "-I", "-B", "/opt/runcrew/entrypoint.py"]
CMD ["--check"]

