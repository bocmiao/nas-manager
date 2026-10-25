#!/usr/bin/env bash
# 构建 NasManager iOS 包。CI 与本机共用同一套逻辑。
#
#   ./scripts/build-ios.sh              # 无签名 IPA（可用 AltStore / Sideloadly 自签安装）
#   ./scripts/build-ios.sh --adhoc      # Ad-Hoc 签名 IPA（需本机已装证书与描述文件）
#   ./scripts/build-ios.sh --clean      # 先清理构建目录
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IOS_DIR="$ROOT/ios"
SCHEME="NasManager"
PROJECT="NasManager.xcodeproj"
BUILD_DIR="$IOS_DIR/build"
EXPORT_DIR="$BUILD_DIR/export"

MODE="unsigned"
CLEAN=0
for arg in "$@"; do
  case "$arg" in
    --adhoc)  MODE="adhoc" ;;
    --clean)  CLEAN=1 ;;
    -h|--help) sed -n '2,8p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "未知参数: $arg" >&2; exit 2 ;;
  esac
done

echo "==> 环境检查"
command -v xcodebuild >/dev/null || { echo "缺少 xcodebuild（需要 macOS + Xcode）" >&2; exit 1; }
if ! command -v xcodegen >/dev/null; then
  echo "缺少 xcodegen，正在尝试通过 homebrew 安装…"
  command -v brew >/dev/null || { echo "缺少 homebrew，请先安装 xcodegen" >&2; exit 1; }
  brew install xcodegen
fi
xcodebuild -version | head -1
xcodegen --version

[ "$CLEAN" = "1" ] && { echo "==> 清理"; rm -rf "$BUILD_DIR" "$IOS_DIR/$PROJECT"; }

echo "==> 生成工程"
cd "$IOS_DIR"
xcodegen generate

echo "==> 构建（模式: $MODE）"
if [ "$MODE" = "unsigned" ]; then
  xcodebuild \
    -project "$PROJECT" \
    -scheme "$SCHEME" \
    -configuration Release \
    -sdk iphoneos \
    -derivedDataPath "$BUILD_DIR" \
    CODE_SIGN_IDENTITY="" \
    CODE_SIGNING_REQUIRED=NO \
    CODE_SIGNING_ALLOWED=NO \
    build

  APP="$(find "$BUILD_DIR/Build/Products/Release-iphoneos" -maxdepth 1 -name '*.app' | head -1)"
  [ -n "$APP" ] || { echo "构建产物未找到" >&2; exit 1; }

  rm -rf "$EXPORT_DIR"
  mkdir -p "$EXPORT_DIR/Payload"
  cp -R "$APP" "$EXPORT_DIR/Payload/"
  ( cd "$EXPORT_DIR" && zip -qry NasManager-unsigned.ipa Payload )
  IPA="$EXPORT_DIR/NasManager-unsigned.ipa"
else
  : "${APPLE_TEAM_ID:?Ad-Hoc 模式需要设置 APPLE_TEAM_ID}"
  : "${PROFILE_NAME:=NasManager AdHoc}"

  xcodebuild \
    -project "$PROJECT" \
    -scheme "$SCHEME" \
    -configuration Release \
    -sdk iphoneos \
    -derivedDataPath "$BUILD_DIR" \
    -archivePath "$BUILD_DIR/NasManager.xcarchive" \
    DEVELOPMENT_TEAM="$APPLE_TEAM_ID" \
    CODE_SIGN_STYLE=Manual \
    CODE_SIGN_IDENTITY="Apple Distribution" \
    PROVISIONING_PROFILE_SPECIFIER="$PROFILE_NAME" \
    archive

  cat > "$BUILD_DIR/ExportOptions.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>method</key><string>ad-hoc</string>
  <key>signingStyle</key><string>manual</string>
  <key>stripSwiftSymbols</key><true/>
  <key>compileBitcode</key><false/>
</dict>
</plist>
PLIST

  xcodebuild -exportArchive \
    -archivePath "$BUILD_DIR/NasManager.xcarchive" \
    -exportPath "$EXPORT_DIR" \
    -exportOptionsPlist "$BUILD_DIR/ExportOptions.plist" \
    -allowProvisioningUpdates
  IPA="$(find "$EXPORT_DIR" -maxdepth 1 -name '*.ipa' | head -1)"
fi

echo "==> 产物"
ls -lh "$IPA"
echo "    $IPA"
