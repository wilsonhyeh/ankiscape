#!/bin/sh
# Double-click macOS launcher: pick a scenario, then open the isolated dev Anki.
# Synthetic data only — never touches your personal Anki (the script refuses
# to run while any Anki process is alive).
cd "/Users/wilsonyeh/Documents/AnkiScape" || exit 1
SCENARIO="$(osascript -e 'choose from list {"fresh", "midgame", "endgame", "classic-upgrade"} with title "AnkiScape Dev" with prompt "Isolated playground scenario (synthetic data only):" default items {"midgame"}' 2>/dev/null)"
if [ -z "$SCENARIO" ] || [ "$SCENARIO" = "false" ]; then
  echo "cancelled"
  exit 0
fi
exec python3 dev.py launch --scenario "$SCENARIO"
