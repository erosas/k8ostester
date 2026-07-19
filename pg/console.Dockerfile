# The remote-control console, packaged to run in-cluster as a control plane.
#
# The release workflow publishes this as <namespace>/k8os-console. Deploy it with
# pg/deploy/console.yaml (ServiceAccount + RBAC + Deployment + ClusterIP Service),
# then port-forward to reach the UI. It authenticates with the pod's mounted
# ServiceAccount and execs into pods over the API stream, so no kubectl is needed.
#
# Build context is the repo root (it needs both workspace members):
#   docker build -f pg/console.Dockerfile -t <your-repo>/k8os-console:<version> .
#
# Wolfi base (glibc, continuously rebuilt, ~0 OS-package CVEs), digest-pinned
# (Dependabot bumps it), runs as a non-root user.
FROM cgr.dev/chainguard/wolfi-base@sha256:02dab76bd852a70556b5b2002195c8a5fdab77d323c433bf6642aab080489795
RUN apk add --no-cache python-3.14 py3.14-pip && adduser -D -u 10001 k8ost

WORKDIR /app
# install the kernel first (a dependency of pg), then the pg vertical
COPY kernel /app/kernel
COPY pg /app/pg
RUN pip install --break-system-packages --no-cache-dir ./kernel ./pg

USER 10001
EXPOSE 8700
ENTRYPOINT ["k8ost-console"]
