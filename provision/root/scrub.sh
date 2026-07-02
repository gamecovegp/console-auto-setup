#!/system/bin/sh
# scrub.sh — run AS ROOT at Lock (before un-root). Clears usage traces + saved game states so the unit
# ships factory-fresh: Android recents, per-emulator recent-ROM/MRU lists (USAGE_TRACES), savestates +
# in-game saves (SAVE_STATES), and the launcher's last-played. ADDITIVE — every step WARNs on failure and
# the script always exits 0, so a scrub miss never blocks or fails a seal.
DIR="$(cd "$(dirname "$0")" && pwd)"; . "$DIR/lib-root.sh"
is_root || { echo "scrub: not root — skipping (a seal must never be blocked by the scrub)"; exit 0; }
scrub_traces
# WiFi — leave NO saved network on a shipped unit (units may provision ONLINE via @wifi, but must ship
# offline with no shop PSK). ALWAYS run, independent of @wifi: a unit that never got wifi is a harmless
# no-op. Additive — WARN only, never blocks the seal.
strip_wifi || warn "scrub: wifi strip reported networks still present — verify this unit ships with no saved wifi"
# launcher last-played: null out lastOpenedTimestamp in GAME_INFO (whichever app currently owns HOME).
LP="$(home_launcher)"; DB="/data/data/$LP/databases/GAME_INFO"
if [ -n "$LP" ] && [ -f "$DB" ] && command -v sqlite3 >/dev/null 2>&1; then
  am force-stop "$LP" 2>/dev/null
  sqlite3 "$DB" "UPDATE game SET lastOpenedTimestamp=NULL;" 2>/dev/null \
    && ok "scrub: launcher last-played cleared" \
    || warn "scrub: GAME_INFO update skipped (no sqlite3 on device or schema differs)"
fi
ok "scrub.sh done"
exit 0
