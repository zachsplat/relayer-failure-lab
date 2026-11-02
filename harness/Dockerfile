# Build from the repository root: docker build -f harness/Dockerfile .
# Tools image for the lab: the fault-injecting RPC proxy, the webhook receiver and
# the scenario drivers. The Relayer itself always runs from the official image.
FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
COPY harness/requirements.txt /lab/requirements.txt
RUN pip install --no-cache-dir --disable-pip-version-check --root-user-action=ignore -r /lab/requirements.txt
COPY harness/*.py /lab/
COPY contracts/AuthToken.json /lab/AuthToken.json
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /work
