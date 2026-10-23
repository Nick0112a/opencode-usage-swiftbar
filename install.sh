#!/usr/bin/env bash
# Installs the OpenCode Usage plugin into your SwiftBar plugin folder.
set -euo pipefail

NAME="opencode-usage.5m.py"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

dir="${SWIFTBAR_PLUGINS_PATH:-}"
if [ -z "${dir}" ]; then
  dir="$(defaults read com.ameba.SwiftBar PluginDirectory 2>/dev/null || true)"
fi

if [ -z "${dir}" ] || [ ! -d "${dir}" ]; then
  echo "找不到 SwiftBar 的插件目录。"
  echo "Can't find the SwiftBar plugin folder."
  echo
  echo "先安装 SwiftBar：brew install --cask swiftbar"
  echo "Install SwiftBar first, open it once, then re-run this script."
  exit 1
fi

# SwiftBar uses the file name as the item's identity: the refresh interval comes from it,
# and so does its position in the menu bar. Copying in a second name would therefore add
# a second item instead of updating this one — so overwrite whatever is already there,
# including a renamed interval (.15m.py) or a pre-2.0 file name.
target=""
for f in "${dir}"/opencode-usage.*m.py "${dir}"/opencode-go-usage.*m.py; do
  if [ -f "${f}" ]; then
    target="${f}"
    break
  fi
done
[ -n "${target}" ] || target="${dir}/${NAME}"

cp "${HERE}/${NAME}" "${target}"
chmod +x "${target}"

if ! python3 -c 'import PIL' >/dev/null 2>&1; then
  echo "提示：当前 python3 没有 Pillow。插件会自己另找一个装了的解释器；找不到才退化成文字。"
  echo "Note: this python3 lacks Pillow. The plugin looks for another one that has it."
  echo "      pip3 install pillow"
fi

open -g "swiftbar://refreshallplugins" 2>/dev/null || true
echo "已安装 / Installed: ${target}"
echo "点开菜单栏图标，用「从剪贴板导入 Key / Set key from clipboard」配置 API key。"
echo "Click the menu bar item and use \"Set key from clipboard\" to add your API key."
