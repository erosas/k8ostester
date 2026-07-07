# Prebuilt loadgen image for clusters without PyPI/Docker Hub egress (D12):
# python + psycopg baked in — the loadgen script itself still ships per-run via
# ConfigMap, so this image changes only when the psycopg pin does.
#
#   docker build -f loadgen.Dockerfile -t <your-repo>/k8ost-loadgen:3.2 .
#   docker push <your-repo>/k8ost-loadgen:3.2
#
# then in the experiment:
#   load:
#     image: <your-repo>/k8ost-loadgen:3.2
#     pull_secret: <secret-name>          # if the repo needs auth
FROM python:3.12-slim
RUN pip install --no-cache-dir 'psycopg[binary]==3.2.*'