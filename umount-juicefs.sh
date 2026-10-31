#!/usr/bin/env bash
set -Eeuo pipefail

MOUNTPOINT="/mnt/juicefs"
STEAMAPPS_DIR="$MOUNTPOINT/steam/steamapps"
BIND_DIRS=(temp downloading shadercache compatdata)
MODE_STATE="/run/juicefs-mount-mode"

if [[ $(id -u) -ne 0 ]]; then
  echo "需要 root 权限运行。" >&2
  exit 1
fi

if ! mountpoint -q "$MOUNTPOINT"; then
  echo "JuiceFS 未挂载：$MOUNTPOINT"
  exit 0
fi

if pgrep -x steam >/dev/null 2>&1 || pgrep -x wineserver >/dev/null 2>&1; then
  echo "Steam/Wine 仍在运行，请先正常退出再卸载。" >&2
  exit 1
fi

for name in "${BIND_DIRS[@]}"; do
  target="$STEAMAPPS_DIR/$name"
  if mountpoint -q "$target"; then
    echo "卸载绑定挂载：$target"
    umount "$target"
  fi
done

sync
echo "等待 writeback 暂存数据上传完成并卸载：$MOUNTPOINT"
juicefs umount --flush "$MOUNTPOINT"

if mountpoint -q "$MOUNTPOINT"; then
  echo "卸载失败，挂载点仍存在：$MOUNTPOINT" >&2
  exit 1
fi

if [[ -e "$MODE_STATE" ]]; then
  unlink "$MODE_STATE"
fi

echo "已安全卸载：$MOUNTPOINT"
