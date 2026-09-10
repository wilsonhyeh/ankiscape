#!/bin/sh
# Double-click macOS launcher: isolated Anki on the PROD backend.
# For Wilson's real-inbox delivery smoke test (register with a real email,
# OTP from ankiscape@ankiscape.xyz, sync, recovery). Refuses to run when the
# packaged artifact is a dev build; refuses while any Anki process is alive.
# Synthetic data is NOT seeded here — this is a real-user walkthrough in an
# isolated profile (personal Anki untouched).
cd "/Users/wilsonyeh/Documents/AnkiScape" || exit 1
exec python3 dev.py launch --scenario prod-smoke
