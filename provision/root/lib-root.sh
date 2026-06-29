# lib-root.sh — shared bits for the ROOT capture/restore toolkit. Everything here runs AS ROOT (su).
# The 11 emulator/frontend packages whose state we clone (settings, key binds, cores, grants, BIOS, keys):
PKGS="dev.eden.eden_emulator com.retroarch.aarch64 org.dolphinemu.dolphinemu com.flycast.emulator \
com.github.stenzek.duckstation xyz.aethersx2.tturnip me.magnum.melonds.nightly org.citra.emu \
org.ppsspp.ppsspp org.mupen64plusae.v3.fzurita org.es_de.frontend gamehub.lite"
# Shared dirs on INTERNAL storage (/storage/emulated/0) that hold emulator state OUTSIDE any app-private
# dir — wiped by a factory reset, so the payload MUST carry them. Citra MMJ keeps its whole state here
# (nand/saves/sysdata/config); RetroArch keeps system/saves/playlists/config here. These are shared media
# storage (not app-UID-owned), so restore is a plain tar extract — FUSE handles ownership.
INTERNAL_DIRS="citra-emu RetroArch ES-DE"
# Which shared internal-storage dir (if any) a package owns — restored only if the app is in the manifest.
internal_for(){ case "$1" in
  org.es_de.frontend) echo "ES-DE";;
  org.citra.emu) echo "citra-emu";;
  com.retroarch.aarch64) echo "RetroArch";;
esac; }
# Manifest = app names (one per line) + "@flag value" lines + "#" comments. Both parsers are pure.
manifest_pkgs(){ sed -e 's/#.*//' "$1" 2>/dev/null | grep -vE '^[[:space:]]*@' | awk 'NF{print $1}'; }
manifest_flag(){ f="$1"; n="$2"; sed -n "s/^@${n}[[:space:]]\{1,\}//p" "$f" 2>/dev/null | awk 'NF{print $1; exit}'; }
# Host/provisioning tools that must NOT be cloned onto shipped units (seal removes them anyway), plus
# Magisk (root provides it per-unit via init_boot). Everything else third-party gets cloned.
EXCLUDE_PKGS="com.termux moe.shizuku.privileged.api com.topjohnwu.magisk"
# Per-app HEAVY data dirs excluded from the captured data.tar — the app + its small config still ship (so it
# stays a tickable option in the list), but a regenerable/reconfigurable bulk payload does NOT bloat the
# golden. GameHub's Wine/Box64 container (files/usr, ~5 GB) is set up on-request per unit, so the golden
# ships GameHub APP-ONLY. Format: "pkg/reldir pkg/reldir …" (member-relative — matches mk_tar's exclude form).
HEAVY_EXCLUDES="gamehub.lite/files/usr gamehub.lite/files/xj_winemu"
# Per-INSTALL identity/state files that must NEVER be cloned from the golden — each unit must mint its own,
# else every device shares the golden's "unique" id and shows the golden's local analytics (recent searches).
# The Companion app self-heals (it binds its device-id to ANDROID_ID and resets analytics on new hardware),
# but ANDROID_ID can be empty on some builds — then the app can't tell it was cloned, so we ALSO strip these
# at the provisioning layer. capture excludes them; restore deletes any an OLD payload still carries.
# Same member-relative form as HEAVY_EXCLUDES ("pkg/reldir-or-file" — matches mk_tar's exclude + the restore rm).
IDENTITY_EXCLUDES="com.gamecove.gamecove_companion/files/device_id.txt com.gamecove.gamecove_companion/files/analytics.json"
# Every user-installed app on the golden, minus the host tools — this is the set capture clones.
user_pkgs(){
  for p in $(pm list packages -3 2>/dev/null | sed 's/^package://'); do
    skip=0; for e in $EXCLUDE_PKGS; do [ "$p" = "$e" ] && skip=1; done
    [ "$skip" = 0 ] && echo "$p"
  done
}
# Device-experience settings to clone (safe subset — NOT identity/provisioning/wifi/account keys).
SET_SYSTEM="screen_off_timeout screen_brightness screen_brightness_mode accelerometer_rotation font_scale haptic_feedback_enabled sound_effects_enabled peak_refresh_rate min_refresh_rate"
SET_GLOBAL="window_animation_scale transition_animation_scale animator_duration_scale stay_on_while_plugged_in"

log(){  printf '   %s\n' "$*"; }
ok(){   printf ' [ok]   %s\n' "$*"; }
warn(){ printf ' [warn] %s\n' "$*"; }
is_root(){ id 2>/dev/null | grep -q 'uid=0'; }
detect_sd(){ for d in /storage/*-*; do [ -d "$d" ] && { echo "$d"; return; }; done; }
app_uid(){ stat -c %u "/data/data/$1" 2>/dev/null; }   # the app's uid on THIS device (differs per unit)
# The package that owns the HOME screen (the launcher). Its private data holds the icon/folder/dock
# layout we clone. Try the brief component form first (pkg/cls), fall back to the packageName= field.
home_launcher(){
  c="$(cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.HOME 2>/dev/null | grep '/' | tail -1)"
  [ -n "$c" ] && { echo "${c%%/*}"; return; }
  cmd package resolve-activity -a android.intent.action.MAIN -c android.intent.category.HOME 2>/dev/null \
    | sed -n 's/.*packageName=\([^ }]*\).*/\1/p' | head -1
}
