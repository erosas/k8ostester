# The k8ost tool image: python runtime + k8ost + kubectl/helm, so a host needs
# nothing but docker and a kubeconfig. Run it through the `k8ost-docker` shim at
# the repo root (mounts kubeconfig + CWD, allocates a TTY for the dashboard,
# reroutes 127.0.0.1 kubeconfigs via the host gateway).
#
#   docker build -t k8os-tester:local k8ostester-core
#   ./k8ost-docker env check
#
# Built on Chainguard's Wolfi (glibc, so python wheels/psycopg work) — a minimal,
# continuously-rebuilt base with ~0 OS CVEs. kubectl and helm come from Wolfi's
# apk repo rather than upstream release binaries: Chainguard rebuilds them against
# patched Go/x-net, so the Go-binary CVEs that ship in the upstream builds are
# already fixed (and stay fixed fast). apk verifies the signed packages — a
# stronger supply chain than a manual checksum. Base pinned by digest (bumped
# daily by Dependabot); runs as a non-root user.
#
# Note: kubeconfigs that authenticate via exec plugins (aws/gcloud/az) need those
# binaries too — add them here (e.g. `apk add aws-cli`).
FROM cgr.dev/chainguard/wolfi-base@sha256:02dab76bd852a70556b5b2002195c8a5fdab77d323c433bf6642aab080489795

RUN apk add --no-cache python-3.14 py3.14-pip ca-certificates-bundle kubectl helm

COPY . /src
RUN pip install --break-system-packages --no-cache-dir /src \
    && rm -rf /src \
    && adduser -D -u 10001 k8ost

USER 10001
WORKDIR /work
ENTRYPOINT ["k8ost"]
