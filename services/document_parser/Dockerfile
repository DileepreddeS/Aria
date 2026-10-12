# The parse worker's container (SECURITY.md §7). Built by
# `python -m aria_document_parser.image build`, never with the repository as context:
# the builder assembles a context holding this file, requirements.txt and site/ only.
#
# The base is pinned by digest. To move it, pull the new tag, put its digest here and
# rebuild; the fingerprint changes with this file, so the API will insist on the rebuild.
FROM python:3.12-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Exactly the locked versions, verified by hash, wheels only: no package runs a build
# script on its way in.
COPY requirements.txt /build/requirements.txt
RUN python -m pip install --no-deps --require-hashes --only-binary=:all: -r /build/requirements.txt \
    && rm -rf /build

# The two first-party packages, copied rather than built, so no build backend is
# fetched either.
COPY site/ /usr/local/lib/python3.12/site-packages/

# nobody. `docker run` repeats this, and adds no network, a read-only root, no
# capabilities and the memory and process ceilings; see client.docker_run_command.
USER 65534:65534
ENTRYPOINT ["python", "-I", "-m", "aria_document_parser.worker"]
