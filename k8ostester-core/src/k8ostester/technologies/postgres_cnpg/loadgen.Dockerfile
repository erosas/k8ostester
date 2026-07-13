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
# Shares the tool's python:3.14-slim base (one base image to mirror).
# psycopg[binary] ships cp314 wheels, so this is a fast wheel install, not a
# from-source compile. Base pinned by digest; runs as a non-root user. Nothing
# but python + psycopg, so the footprint is minimal already.
FROM python:3.14-slim@sha256:b877e50bd90de10af8d82c57a022fc2e0dc731c5320d762a27986facfc3355c1
RUN pip install --no-cache-dir 'psycopg[binary]==3.2.*' \
    && useradd --create-home --uid 10001 loadgen
USER 10001