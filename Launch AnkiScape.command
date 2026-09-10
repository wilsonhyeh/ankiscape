#!/bin/sh
# Double-click macOS launcher: production rehearsal in an isolated Anki profile.
# Installs the packaged prod artifact (the real release bits) against the
# production backend — what an installed user actually gets. Never touches
# your personal Anki, account, or data. Synthetic dev scenarios stay on
# `python3 dev.py launch --scenario <fresh|midgame|endgame|classic-upgrade>`.
cd "/Users/wilsonyeh/Documents/AnkiScape" || exit 1
DEFAULT_CHOICE="New user - fresh install"
if [ -f ".dev/anki/user/dev-user/collection.anki2" ]; then
  DEFAULT_CHOICE="Resume last session"
fi
CHOICE="$(osascript - "$DEFAULT_CHOICE" <<'APPLESCRIPT' 2>/dev/null
on run argv
  choose from list {"New user - fresh install", "Upgrade from Classic 2.0.2", "Resume last session"} with title "AnkiScape production rehearsal" with prompt "Resume keeps your test profile. New user and Upgrade replace it. All use the production backend:" default items {item 1 of argv}
end run
APPLESCRIPT
)"
case "$CHOICE" in
  "New user - fresh install")
    SCENARIO=user-new ;;
  "Upgrade from Classic 2.0.2")
    SCENARIO=user-upgrade ;;
  "Resume last session")
    SCENARIO=user-resume ;;
  *)
    echo "cancelled"
    exit 0 ;;
esac
python3 dev.py launch --scenario "$SCENARIO"
rc=$?
if [ "$rc" -ne 0 ]; then
  echo
  printf 'AnkiScape launcher exited %s. Press Return to close.\n' "$rc"
  read -r _
fi
exit "$rc"
