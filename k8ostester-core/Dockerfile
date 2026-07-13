# The k8ost tool image: python runtime + k8ost + pinned kubectl/helm, so a
# host needs nothing but docker and a kubeconfig. Run it through the
# `k8ost-docker` shim at the repo root (mounts kubeconfig + CWD, allocates a
# TTY for the dashboard, reroutes 127.0.0.1 kubeconfigs via the host gateway).
#
#   docker build -t k8os-tester:local k8ostester-core
#   ./k8ost-docker env check
#
# Built on Chainguard's Wolfi (glibc, so python wheels/psycopg work) for a
# minimal, low-CVE base that is rebuilt continuously — OS-package CVEs stay at
# ~0 without us patching. Hardened further:
#   - two stages: curl/tar and download cruft stay in the builder; the final
#     image carries only the two verified binaries + the python runtime
#   - kubectl/helm downloads are sha256-checked against upstream (supply chain)
#   - base pinned by digest (reproducible; bumped frequently by Dependabot)
#   - runs as a non-root user
#
# Note: kubeconfigs that authenticate via exec plugins (aws/gcloud/az) need
# those binaries too — extend the final stage (`apk add ...` on Wolfi).

# ---- builder: fetch + verify kubectl and helm, then throw the toolchain away ----
FROM cgr.dev/chainguard/wolfi-base@sha256:02dab76bd852a70556b5b2002195c8a5fdab77d323c433bf6642aab080489795 AS tools

ARG TARGETARCH
ARG KUBECTL_VERSION=v1.36.2
ARG HELM_VERSION=v4.2.3

RUN apk add --no-cache curl

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
    chmod +x helm

# ---- final: minimal Wolfi runtime, non-root ----
FROM cgr.dev/chainguard/wolfi-base@sha256:02dab76bd852a70556b5b2002195c8a5fdab77d323c433bf6642aab080489795

RUN apk add --no-cache python-3.14 py3.14-pip ca-certificates-bundle

COPY --from=tools /out/kubectl /out/helm /usr/local/bin/

COPY . /src
RUN pip install --break-system-packages --no-cache-dir /src \
    && rm -rf /src \
    && adduser -D -u 10001 k8ost

USER 10001
WORKDIR /work
ENTRYPOINT ["k8ost"]
