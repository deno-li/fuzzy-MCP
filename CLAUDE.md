<!--
SPDX-FileCopyrightText: 2026 Deniz Özer

SPDX-License-Identifier: CC0-1.0
-->

# Instruktioner för Claude

## Merga pull requests

Squash-merga och lägg underhållarens DCO-signoff som sista rad i squash-commitens meddelande:

```text
Signed-off-by: Deniz Özer <241947278+deno-li@users.noreply.github.com>
```

Underhållaren har bett om att signoffen läggs in vid varje merge som Claude gör åt underhållaren. Den hör bara
hemma i squash-commiten som mergas till `main`, inte i commits på grenar, eftersom den är underhållarens intyg enligt
_Developer Certificate of Origin_ (se [CONTRIBUTING.md](CONTRIBUTING.md#dco--signoff-och-signering-av-en-commit)).
