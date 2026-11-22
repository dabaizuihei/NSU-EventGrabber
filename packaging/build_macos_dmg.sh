#!/usr/bin/env bash
# 生成带「应用程序」快捷方式的 macOS DMG，支持直接把 .app 拖进 Applications。
set -euo pipefail

APP="${1:?usage: build_macos_dmg.sh <app> <output.dmg> [volume-name]}"
OUT="${2:?usage: build_macos_dmg.sh <app> <output.dmg> [volume-name]}"
VOL="${3:-NSU-EventGrabber}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKGROUND="$SCRIPT_DIR/dmg-background.png"
APP_NAME="$(basename "$APP")"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/nsu-dmg.XXXXXX")"
STAGE="$WORK/stage"
MOUNT=""
RW_DMG="$WORK/read-write.dmg"
mkdir -p "$STAGE"

cleanup() {
  if [[ -n "${MOUNT:-}" ]]; then
    hdiutil detach "$MOUNT" >/dev/null 2>&1 || hdiutil detach "$MOUNT" -force >/dev/null 2>&1 || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

ditto "$APP" "$STAGE/$APP_NAME"
ln -s /Applications "$STAGE/Applications"
mkdir -p "$STAGE/.background"
if [[ -f "$BACKGROUND" ]]; then
  cp "$BACKGROUND" "$STAGE/.background/dmg-background.png"
fi

# GitHub 的 macOS runner 上 hdiutil 偶发 "Resource busy / temporarily unavailable"，
# 对创建、转换做有限重试，避免一次磁盘工具抖动就让整个发布失败。
create_ok=0
for attempt in 1 2 3 4 5; do
  if hdiutil create -volname "$VOL" -srcfolder "$STAGE" -ov -format UDRW "$RW_DMG" >/dev/null; then
    create_ok=1
    break
  fi
  rm -f "$RW_DMG"
  sleep $((attempt * 2))
done
if [[ "$create_ok" != "1" ]]; then
  echo "hdiutil create failed after retries" >&2
  exit 1
fi
ATTACH_OUTPUT="$(hdiutil attach "$RW_DMG" -nobrowse)"
MOUNT="$(printf '%s\n' "$ATTACH_OUTPUT" | awk -F'\t' '/\/Volumes\// {print $NF; exit}')"
if [[ -z "$MOUNT" || ! -d "$MOUNT" ]]; then
  echo "DMG mount failed" >&2
  exit 1
fi
VOLUME_NAME="$(basename "$MOUNT")"

# 写入卷图标，并尽量把两个图标摆成“应用 → 应用程序”的拖拽布局。
cp "$SCRIPT_DIR/NSU-EventGrabber.icns" "$MOUNT/.VolumeIcon.icns" 2>/dev/null || true
if command -v SetFile >/dev/null 2>&1; then
  SetFile -a C "$MOUNT" >/dev/null 2>&1 || true
fi

if command -v osascript >/dev/null 2>&1 && [[ -f "$BACKGROUND" ]]; then
  osascript <<EOF || true
tell application "Finder"
  tell disk "$VOLUME_NAME"
    open
    set current view of container window to icon view
    set toolbar visible of container window to false
    set statusbar visible of container window to false
    set the bounds of container window to {100, 100, 700, 460}
    set viewOptions to the icon view options of container window
    set arrangement of viewOptions to not arranged
    set icon size of viewOptions to 112
    set text size of viewOptions to 13
    set bgFile to POSIX file "$MOUNT/.background/dmg-background.png" as alias
    set background picture of viewOptions to bgFile
    set position of item "$APP_NAME" of container window to {165, 205}
    set position of item "Applications" of container window to {435, 205}
    close
    open
    update without registering applications
    delay 1
    close
  end tell
end tell
EOF
fi

sync
if ! hdiutil detach "$MOUNT" >/dev/null 2>&1; then
  hdiutil detach "$MOUNT" -force >/dev/null 2>&1 || true
fi
MOUNT=""
sleep 2
convert_ok=0
for attempt in 1 2 3 4 5; do
  if hdiutil convert "$RW_DMG" -format UDZO -imagekey zlib-level=9 -ov -o "$OUT" >/dev/null; then
    convert_ok=1
    break
  fi
  rm -f "$OUT"
  sleep $((attempt * 2))
done
if [[ "$convert_ok" != "1" ]]; then
  echo "hdiutil convert failed after retries; retrying with direct compressed image" >&2
  rm -f "$OUT"
  fallback_ok=0
  for attempt in 1 2 3 4 5; do
    if hdiutil create -volname "$VOL" -srcfolder "$STAGE" -ov -format UDZO "$OUT" >/dev/null; then
      fallback_ok=1
      break
    fi
    rm -f "$OUT"
    sleep $((attempt * 2))
  done
  if [[ "$fallback_ok" != "1" ]]; then
    echo "hdiutil create (compressed) failed after retries" >&2
    exit 1
  fi
  echo "DMG created (direct compressed fallback): $OUT"
  exit 0
fi
echo "DMG created: $OUT"
