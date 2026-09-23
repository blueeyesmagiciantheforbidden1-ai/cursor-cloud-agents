# Build context MUST be this directory, never a credential/source parent.
# Caller must supply an audited Linux amd64 Python 3.12 base with git/bash and
# certificates, pinned by manifest digest. No mutable default or apt downloads.
ARG WORKER_BASE
FROM ${WORKER_BASE}
ARG WORKER_BASE
USER root
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /opt/runcrew/codex-package
COPY . .
RUN python -c "import re,sys; assert re.fullmatch(r'[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}',sys.argv[1]), 'digest-pinned base required'" "${WORKER_BASE}"
RUN python build_native.py --destination /opt/runcrew/codex
RUN python -m unittest discover -s . -p "test_*.py" -v
RUN mkdir -p /workspace/default /run/codex-private && chown 10001:10001 /workspace/default /run/codex-private
USER 10001:10001
WORKDIR /workspace/default
ENTRYPOINT ["python", "/opt/runcrew/codex-package/package_status.py"]
