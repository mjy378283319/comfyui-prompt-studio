#!/bin/sh
# 启动时补齐内置数据（不覆盖用户文件），并修正 Unraid 挂载卷属主。
set -eu

: "${DATA_DIR:=/app/data}"
: "${MODEL_DIR:=/app/models/wd14}"
: "${REFERENCE_DIR:=/app/reference}"
mkdir -p "$DATA_DIR" "$MODEL_DIR" "$REFERENCE_DIR" 2>/dev/null || true

for f in lexicon.json guide.md presets.json; do
  if [ ! -f "$DATA_DIR/$f" ] && [ -f "/app/seed/$f" ]; then
    cp "/app/seed/$f" "$DATA_DIR/$f"
  fi
done

# Unraid 的 nobody:users = 99:100；chown 失败不阻断启动（例如只读卷）。
chown -R "${PUID:-99}:${PGID:-100}" "$DATA_DIR" "$MODEL_DIR" 2>/dev/null || true

exec "$@"
