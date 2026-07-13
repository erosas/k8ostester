# The k8ost tool image: python runtime + k8ost + pinned kubectl/helm, so a
# host needs nothing but docker and a kubeconfig. Run it through the
# `k8ost-docker` shim at the repo root (mounts kubeconfig + CWD, allocates a
# TTY for the dashboard, reroutes 127.0.0.1 kubeconfigs via the host gateway).
#
#   docker build -t k8ostester:local k8ostester-core
#   ./k8ost-docker env check
#
# Hardened for a small, auditable footprint:
#   - two stages: curl/tar and the download cruft stay in the builder; the
#     final image carries only the two verified binaries + the python runtime
#   - kubectl/helm downloads are sha256-checked against upstream (supply chain)
#   - base pinned by digest (reproducible; bump deliberately, e.g. via renovate)
#   - runs as a non-root user
#
# Note: kubeconfigs that authenticate via exec plugins (aws/gcloud/az) need
# those binaries too — extend the final stage, e.g.:
#   RUN curl -fsSL ...aws-iam-authenticator... -o /usr/local/bin/aws-iam-authenticator

# ---- builder: fetch + verify kubectl and helm, then throw the toolchain away ----
FROM python:3.14-slim@sha256:b877e50bd90de10af8d82c57a022fc2e0dc731c5320d762a27986facfc3355c1 AS tools

ARG TARGETARCH
ARG KUBECTL_VERSION=v1.36.1
ARG HELM_VERSION=v4.2.2

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /out
RUN set -eux; \
    kbase="https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}"; \
    curl -fsSL "$kbase/kubectl" -o kubectl; \
    curl -fsSL "$kbase/kubectl.sha256" -o kubectl.sha256; \
    echo "$(cat kubectl.sha256)  kubectl" | sha256sum -c -; \
    chmod +x kubectl; \
    htar="helm-${HELM_VERSION}-linux-${TARGETARCH}.tar.gz"; \
    curl -fsSL "https://get.helm.sh/${htar}" -o "$htar"; \
    curl -fsSL "https://get.helm.sh/${htar}.sha256sum" -o "${htar}.sha256sum"; \
    sha256sum -c "${htar}.sha256sum"; \
    tar -xzf "$htar"; \
    mv "linux-${TARGETARCH}/helm" helm; \
    chmod +x helm; \
    rm -rf "$htar" "${htar}.sha256sum" kubectl.sha256 "linux-${TARGETARCH}"

# ---- final: slim runtime, no curl/apt lists, non-root ----
FROM python:3.14-slim@sha256:b877e50bd90de10af8d82c57a022fc2e0dc731c5320d762a27986facfc3355c1

COPY --from=tools /out/kubectl /out/helm /usr/local/bin/

COPY . /src
RUN pip install --no-cache-dir /src \
    && rm -rf /src \
    && useradd --create-home --uid 10001 k8ost

USER 10001
WORKDIR /work
ENTRYPOINT ["k8ost"]
