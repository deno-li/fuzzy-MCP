# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT
#
# fuzzy-mcp som container: streamable HTTP på port 8000, körs som icke-root, hälsokontroll mot /healthz.
#
#   docker build --build-arg VERSION=0.1.2 --build-arg VCS_REF=$(git rev-parse HEAD) -t fuzzy-mcp:0.1.2 .
#   docker run -d -p 127.0.0.1:8000:8000 -e FUZZY_MCP_ALLOWED_HOSTS=mcp.kommun.se \
#     -v /etc/fuzzy-mcp/token:/run/secrets/fuzzy_mcp_token:ro -e FUZZY_MCP_AUTH_TOKEN_FILE=/run/secrets/fuzzy_mcp_token \
#     --read-only --cap-drop ALL --security-opt no-new-privileges fuzzy-mcp:0.1.2
#
# Utan token startar servern inte (den lyssnar på 0.0.0.0). Tokenfilen ska vara läsbar för uid 10001.
#
# Bakom en proxy med TLS-inspektion: skicka med proxy och en komplett CA-bundle (publika rotcertifikat +
# organisationens CA; den ersätter de inbyggda rotcertifikaten under bygget och hamnar inte i imagen):
#   docker build --build-arg HTTPS_PROXY=http://proxy:8080 --secret id=ca,src=ca-bundle.pem -t fuzzy-mcp:0.1.2 .
# Lås gärna basimagen till en digest i produktion: FROM python:3.12-slim@sha256:<digest>

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE CHANGELOG.md ./
COPY LICENSES ./LICENSES
COPY src ./src
# Bara det här steget använder nätet (PyPI). Beroendena låses med SHA-256 och hämtas som hjul.
RUN --mount=type=secret,id=ca \
    if [ -s /run/secrets/ca ]; then export PIP_CERT=/run/secrets/ca SSL_CERT_FILE=/run/secrets/ca; fi \
 && pip install --no-cache-dir --disable-pip-version-check "uv==0.11.32" \
 && uv build --wheel --out-dir /dist \
 && uv pip compile pyproject.toml --generate-hashes --no-header --quiet -o /dist/requirements.lock \
 && pip download --no-cache-dir --disable-pip-version-check --no-deps --only-binary=:all: \
      -r /dist/requirements.lock -d /dist/wheels

FROM python:${PYTHON_VERSION}-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
# Installation utan nät, med hashkontroll mot låsfilen. Basimagens pip installerar i en venv utan egen pip och
# tas sedan bort (med setuptools, wheel, packaging och ensurepips hjul från äldre basimagar): servern behöver dem inte.
RUN --mount=type=bind,from=build,source=/dist,target=/dist \
    python -m venv --without-pip /opt/fuzzy-mcp \
 && python -m pip --python /opt/fuzzy-mcp/bin/python install --no-cache-dir --disable-pip-version-check \
      --no-index --find-links /dist/wheels --require-hashes -r /dist/requirements.lock \
 && python -m pip --python /opt/fuzzy-mcp/bin/python install --no-cache-dir --disable-pip-version-check \
      --no-index --no-deps /dist/*.whl \
 && python -m pip uninstall --yes --disable-pip-version-check setuptools wheel packaging pip \
 && rm -f /usr/local/lib/python3*/ensurepip/_bundled/*.whl \
 && groupadd --system --gid 10001 fuzzy \
 && useradd --system --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin fuzzy
ENV PATH=/opt/fuzzy-mcp/bin:$PATH \
    FUZZY_MCP_TRANSPORT=streamable-http \
    FUZZY_MCP_HOST=0.0.0.0 \
    FUZZY_MCP_PORT=8000
USER 10001:10001
EXPOSE 8000
# Spårbarhet: build_release.py sätter version och commit (--build-arg VERSION=… VCS_REF=…).
ARG VERSION=dev
ARG VCS_REF=okänd
LABEL org.opencontainers.image.title="fuzzy-mcp" \
      org.opencontainers.image.description="MCP-server för svensk öppen data" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.source="https://github.com/deno-li/fuzzy-MCP" \
      org.opencontainers.image.licenses="MIT AND CC0-1.0"
# Hälsokontrollen går aldrig via en proxy från miljön (HTTPS_PROXY når inte containerns 127.0.0.1).
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import os, sys, urllib.request as u; port = os.environ.get('FUZZY_MCP_PORT', '').strip() or '8000'; r = u.build_opener(u.ProxyHandler({})).open(f'http://127.0.0.1:{port}/healthz', timeout=4); sys.exit(0 if r.status == 200 else 1)"]
ENTRYPOINT ["fuzzy-mcp"]
