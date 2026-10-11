# syntax=docker/dockerfile:1
# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: CC0-1.0
#
# Utgående proxy för fuzzy-mcp (Squid från Ubuntu 24.04) med squid.conf: bara HTTPS till myndigheternas värdar.
# Byggs och startas av docker-compose.egress.yml (docs/ENEO.md avsnitt 2), som också monterar squid.conf. Körs som
# användaren proxy, utan cache och utan skrivningar till disk. Lås gärna basimagen till en digest i produktion: FROM ubuntu:24.04@sha256:<digest>
FROM ubuntu:24.04
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends squid \
 && rm -rf /var/lib/apt/lists/*
COPY squid.conf /etc/squid/squid.conf
USER proxy
EXPOSE 3128
ENTRYPOINT ["squid", "-N", "-f", "/etc/squid/squid.conf"]
