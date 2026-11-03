#!/usr/bin/env bash
# Package the add-on into kelma.ankiaddon, and (optionally) symlink it into your
# Anki addons folder for development.
#
#   ./build.sh           # build kelma.ankiaddon in dist/
#   ./build.sh dev       # symlink src/ into the Anki addons21 folder as "kelma"
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/src"
DIST="$HERE/dist"

addons_dir() {
  case "$(uname -s)" in
    Darwin) echo "$HOME/Library/Application Support/Anki2/addons21" ;;
    Linux)  echo "${XDG_DATA_HOME:-$HOME/.local/share}/Anki2/addons21" ;;
    *)      echo "$APPDATA/Anki2/addons21" ;;
  esac
}

if [[ "${1:-}" == "dev" ]]; then
  DEST="$(addons_dir)/kelma"
  mkdir -p "$(addons_dir)"
  rm -rf "$DEST"
  ln -s "$SRC" "$DEST"
  echo "Symlinked $SRC -> $DEST"
  echo "Restart Anki to load the add-on."
  exit 0
fi

mkdir -p "$DIST"
OUT="$DIST/kelma.ankiaddon"
rm -f "$OUT"
# .ankiaddon is a zip of the package *contents* (no top-level folder).
# Exclude meta.json — it's Anki's per-install file (auth hkeys, deck routing);
# Anki recreates it on install, and shipping the dev's would leak credentials.
( cd "$SRC" && zip -r -q "$OUT" . -x '*.pyc' -x '__pycache__/*' -x 'meta.json' )
echo "Built $OUT"
