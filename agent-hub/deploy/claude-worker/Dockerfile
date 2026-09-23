# Build from the agent-hub source root. No provider credential is a build input.
FROM python:3.12-slim-bookworm AS release
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates gpg gpgv \
    && rm -rf /var/lib/apt/lists/*
COPY deploy/claude-worker/build_profile.py /build_profile.py
RUN test "$(dpkg --print-architecture)" = amd64 \
    && python /build_profile.py --output /verified

FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libstdc++6 git \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 worker \
    && useradd --uid 10001 --gid worker --no-create-home --shell /usr/sbin/nologin worker \
    && install -d -o worker -g worker -m 0700 /home/worker /home/worker/.claude \
    && install -d -o root -g root -m 0755 /opt/runcrew/app/agent_hub /workspace /run/config \
    && install -d -o worker -g worker -m 0700 /workspace/default
COPY --from=release --chown=root:root /verified/ /opt/runcrew/claude/
# Worker imports only the Python standard library; Firestore/auth libraries belong to the hub.
COPY --chown=root:root agent_hub/*.py /opt/runcrew/app/agent_hub/
COPY --chown=root:root deploy/claude-worker/entrypoint.py /opt/runcrew/entrypoint.py
COPY --chown=root:root deploy/claude-worker/eligibility_probe.py /opt/runcrew/eligibility_probe.py
COPY --chown=root:root deploy/claude-worker/room_review.py /opt/runcrew/room_review.py
RUN chmod -R go-w /opt/runcrew \
    && chmod 0555 /opt/runcrew/claude/claude \
    && chmod 0444 /opt/runcrew/claude/runtime-profile.json
ENV HOME=/home/worker \
    CLAUDE_CONFIG_DIR=/home/worker/.claude \
    PATH=/usr/local/bin:/usr/bin:/bin \
    LANG=C.UTF-8 \
    TMPDIR=/tmp \
    DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
WORKDIR /workspace/default
USER 10001:10001
RUN python -I -B /opt/runcrew/entrypoint.py --check
ENTRYPOINT ["python", "-I", "-B", "/opt/runcrew/entrypoint.py"]
