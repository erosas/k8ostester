# The k8ost tool image: python runtime + k8ost + pinned kubectl/helm, so a
# host needs nothing but docker and a kubeconfig. Run it through the
# `k8ost-docker` shim at the repo root (mounts kubeconfig + CWD, allocates a
# TTY for the dashboard, reroutes 127.0.0.1 kubeconfigs via the host gateway).
#
#   docker build -t k8ostester:local k8ostester-core
#   ./k8ost-docker env check
#
# Note: kubeconfigs that authenticate via exec plugins (aws/gcloud/az) need
# those binaries too — extend this image, e.g.:
#   RUN curl -fsSL ...aws-iam-authenticator... -o /usr/local/bin/aws-iam-authenticator
FROM python:3.14-slim

ARG TARGETARCH
ARG KUBECTL_VERSION=v1.36.1
ARG HELM_VERSION=v4.2.2

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl" \
      -o /usr/local/bin/kubectl && chmod +x /usr/local/bin/kubectl \
    && curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-${TARGETARCH}.tar.gz" \
      | tar -xz -C /tmp \
    && mv "/tmp/linux-${TARGETARCH}/helm" /usr/local/bin/helm \
    && rm -rf "/tmp/linux-${TARGETARCH}"

COPY . /src
RUN pip install --no-cache-dir /src && rm -rf /src

WORKDIR /work
ENTRYPOINT ["k8ost"]
