#!/system/bin/sh
# setup-rish.sh — run ONCE in TERMUX to install rish (no typing the long block, no unzip/internet).
#   In Termux:   sh /sdcard/provision/setup-rish.sh
# Then:          ./rish        (tap Allow on Shizuku) ;  id   (expect uid=2000(shell))
SRC="$(dirname "$0")"
[ -f "$SRC/rish-files/rish" ] || { echo "rish files not found next to this script ($SRC/rish-files)"; exit 1; }
cp "$SRC/rish-files/rish"            "$HOME/rish"            || { echo "copy failed (storage permission?)"; exit 1; }
cp "$SRC/rish-files/rish_shizuku.dex" "$HOME/rish_shizuku.dex"
chmod +x "$HOME/rish"
chmod 400 "$HOME/rish_shizuku.dex"   # read-only (required on A14+, harmless on A13)
grep -q RISH_APPLICATION_ID "$HOME/.bashrc" 2>/dev/null || echo 'export RISH_APPLICATION_ID=com.termux' >> "$HOME/.bashrc"
export RISH_APPLICATION_ID=com.termux
echo ""
echo "rish installed in $HOME."
echo "NEXT (still in Termux):"
echo "   ./rish            # tap ALLOW on the Shizuku prompt"
echo "   id                # success = uid=2000(shell)"
echo "   sh /storage/<sd>/provision/run.sh citra     # test provision (zero-UI)"
