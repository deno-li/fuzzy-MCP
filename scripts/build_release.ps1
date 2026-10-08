# SPDX-FileCopyrightText: 2026 Deniz Özer
#
# SPDX-License-Identifier: MIT
#
# Bygg leveranspaket för IT-drift (standard: Windows x64, Python 3.14). Exempel:
#   .\scripts\build_release.ps1
#   .\scripts\build_release.ps1 --platform linux                # Linux x86_64, Python 3.12
# Kräver uv (https://docs.astral.sh/uv/). Se scripts/build_release.py för detaljer.
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
uv run --no-project --python 3.12 scripts/build_release.py @args
exit $LASTEXITCODE
