#!/bin/bash
# Build a macOS launcher app for the local web GUI into ~/Applications, so the
# calculator can be pinned to the Dock instead of being hunted for in Safari's
# tabs. Clicking it starts the server if needed, then opens the app window.
#
#   ./scripts/macos/install_dock_app.sh
#   ./scripts/macos/install_dock_app.sh --app-name "Daně" --port 8321
set -euo pipefail

APP_NAME="Daňová kalkulačka"
WEBAPP_NAME="IBKR Tax Engine"
BUNDLE_ID="cz.beryko.ibkr-tax.launcher"
HOST="127.0.0.1"
PORT="8321"
DEST_DIR="$HOME/Applications"
FORCE=0

usage() {
    cat <<'USAGE'
Postaví launcher .app pro lokální web GUI do ~/Applications.

Přepínače:
  --app-name NÁZEV      Jméno launcheru (výchozí: "Daňová kalkulačka")
  --webapp-name NÁZEV   Jméno Safari web appu, který má launcher otevírat
                        (výchozí: "IBKR Tax Engine" — jméno z manifestu)
  --port ČÍSLO          Port serveru (výchozí: 8321)
  --dest ADRESÁŘ        Kam bundle nainstalovat (výchozí: ~/Applications)
  --force               Přepsat cíl, i když to není náš launcher
  -h, --help            Tato nápověda
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --app-name) APP_NAME="${2:?--app-name potřebuje hodnotu}"; shift 2 ;;
        --webapp-name) WEBAPP_NAME="${2:?--webapp-name potřebuje hodnotu}"; shift 2 ;;
        --port) PORT="${2:?--port potřebuje hodnotu}"; shift 2 ;;
        --dest) DEST_DIR="${2:?--dest potřebuje hodnotu}"; shift 2 ;;
        --force) FORCE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Neznámý přepínač: $1" >&2; usage >&2; exit 2 ;;
    esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
TEMPLATE="$HERE/launcher.template.sh"
SRC_ICON="$REPO/src/webapp/static/favicon/web-app-manifest-512x512.png"
APP="$DEST_DIR/$APP_NAME.app"

[ -f "$TEMPLATE" ] || { echo "Chybí šablona: $TEMPLATE" >&2; exit 1; }
[ -f "$SRC_ICON" ] || { echo "Chybí zdrojová ikona: $SRC_ICON" >&2; exit 1; }

UV="$(command -v uv || true)"
if [ -z "$UV" ]; then
    echo "uv není v PATH — nainstaluj ho podle https://docs.astral.sh/uv/ a spusť znovu." >&2
    exit 1
fi

case "$APP" in
    *.app) ;;
    *) echo "Cíl musí končit na .app: $APP" >&2; exit 1 ;;
esac

# Never blow away something we didn't build.
if [ -e "$APP" ]; then
    if [ -f "$APP/Contents/MacOS/launcher" ] || [ "$FORCE" = 1 ]; then
        :
    else
        echo "$APP už existuje a nevypadá jako náš launcher." >&2
        echo "Použij --force, nebo zvol jiný --app-name." >&2
        exit 1
    fi
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
BUILD="$TMP/$APP_NAME.app"
mkdir -p "$BUILD/Contents/MacOS" "$BUILD/Contents/Resources"

# LSUIElement: the launcher does its work without stealing focus or flashing a
# second Dock tile — the window the user sees belongs to the web app.
cat >"$BUILD/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>$APP_NAME</string>
    <key>CFBundleDisplayName</key><string>$APP_NAME</string>
    <key>CFBundleExecutable</key><string>launcher</string>
    <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>LSUIElement</key><true/>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

sed -e "s|__REPO__|$REPO|g" \
    -e "s|__UV__|$UV|g" \
    -e "s|__HOST__|$HOST|g" \
    -e "s|__PORT__|$PORT|g" \
    -e "s|__WEBAPP_NAME__|$WEBAPP_NAME|g" \
    "$TEMPLATE" >"$BUILD/Contents/MacOS/launcher"
chmod 755 "$BUILD/Contents/MacOS/launcher"

# Same artwork the web app gets from the manifest, so both Dock tiles match.
ICONSET="$TMP/AppIcon.iconset"
mkdir -p "$ICONSET"
build_icon() { # <pixels> <iconset name>
    sips -s format png -z "$1" "$1" "$SRC_ICON" --out "$ICONSET/$2" >/dev/null
}
build_icon 16 icon_16x16.png
build_icon 32 icon_16x16@2x.png
build_icon 32 icon_32x32.png
build_icon 64 icon_32x32@2x.png
build_icon 128 icon_128x128.png
build_icon 256 icon_128x128@2x.png
build_icon 256 icon_256x256.png
build_icon 512 icon_256x256@2x.png
build_icon 512 icon_512x512.png
iconutil -c icns "$ICONSET" -o "$BUILD/Contents/Resources/AppIcon.icns"

mkdir -p "$DEST_DIR"
rm -rf "$APP"
mv "$BUILD" "$APP"

# Ad-hoc signature + a re-register so the Dock picks up the icon immediately.
codesign --force --sign - "$APP" >/dev/null 2>&1 || true
touch "$APP"
LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
[ -x "$LSREGISTER" ] && "$LSREGISTER" -f "$APP" >/dev/null 2>&1 || true

cat <<DONE

Hotovo: $APP

Zbývá jednorázově vyrobit okno aplikace v Safari:
  1) klikni na "$APP_NAME" (Finder → Aplikace, nebo Spotlight) — nastartuje server
     a otevře http://$HOST:$PORT/ v Safari
  2) v Safari: Soubor → Přidat do Docku…
  3) v dialogu nech jméno "$WEBAPP_NAME" a potvrď

Od té doby launcher otevírá přímo tu web app (vlastní okno, žádné záložky).
Ikonu "$APP_NAME" si přetáhni do Docku — tu mačkej příště.

Log serveru: ~/Library/Logs/ibkr-tax/webapp.log
DONE
