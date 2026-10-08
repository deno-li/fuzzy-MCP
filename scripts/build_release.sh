#!/usr/bin/env sh
# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT
#
# Bygg leveranspaket för IT-drift. Exempel:
#   scripts/build_release.sh --platform linux                    # Python 3.12 (standard för Linux)
#   scripts/build_release.sh --platform windows                  # Python 3.14 (standard för Windows)
# Kräver uv (https://docs.astral.sh/uv/). Se scripts/build_release.py för detaljer.
set -eu
cd "$(dirname "$0")/.."
exec uv run --no-project --python 3.12 scripts/build_release.py "$@"
