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
# Stays on 3.12 (not the tool's 3.14): psycopg[binary] ships cp312 wheels, so
# the build is a fast wheel install rather than a from-source compile.
# Base pinned by digest; runs as a non-root user. Nothing but python + psycopg,
# so the footprint is minimal already.
FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf
RUN pip install --no-cache-dir 'psycopg[binary]==3.2.*' \
    && useradd --create-home --uid 10001 loadgen
USER 10001