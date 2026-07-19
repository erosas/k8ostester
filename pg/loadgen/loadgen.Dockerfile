# Prebuilt loadgen image for clusters without PyPI/Docker Hub egress (D12):
# python + psycopg baked in — the loadgen script itself still ships per-run via
# ConfigMap, so this image changes only when the psycopg pin does.
#
# The release workflow publishes this as <namespace>/k8os-loadgen. To build it
# yourself (or mirror it into a private registry):
#
#   docker build -f loadgen.Dockerfile -t <your-repo>/k8os-loadgen:<version> .
#   docker push <your-repo>/k8os-loadgen:<version>
#
# then point the framework at it — per experiment, or globally via
# K8OST_LOADGEN_IMAGE:
#   load:
#     image: <your-repo>/k8os-loadgen:<version>
#     pull_secret: <secret-name>          # if the repo needs auth
#
# Built on Chainguard's Wolfi (glibc, so psycopg[binary] wheels install
# directly): a minimal, continuously-rebuilt base with ~0 OS-package CVEs.
# Digest-pinned (Dependabot bumps it); runs as a non-root user.
FROM cgr.dev/chainguard/wolfi-base@sha256:02dab76bd852a70556b5b2002195c8a5fdab77d323c433bf6642aab080489795
RUN apk add --no-cache python-3.14 py3.14-pip \
    && pip install --break-system-packages --no-cache-dir 'psycopg[binary]==3.2.*' \
    && adduser -D -u 10001 loadgen
USER 10001
