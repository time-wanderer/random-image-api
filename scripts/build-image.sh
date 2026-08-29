#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

IMAGE_NAME="${IMAGE_NAME:-random-image-api}"
IMAGE_TAG="${IMAGE_TAG:-local}"
DIST_DIR="${DIST_DIR:-$ROOT_DIR/dist}"
OUTPUT="${OUTPUT:-$DIST_DIR/${IMAGE_NAME}-${IMAGE_TAG}.tar}"

command -v docker >/dev/null 2>&1 || {
  echo "错误：找不到 docker，请先安装 Docker Engine。" >&2
  exit 1
}
mkdir -p "$DIST_DIR"

echo "[1/3] 构建镜像 $IMAGE_NAME:$IMAGE_TAG"
docker build --pull -t "$IMAGE_NAME:$IMAGE_TAG" .

echo "[2/3] 导出镜像 $OUTPUT"
docker save -o "$OUTPUT" "$IMAGE_NAME:$IMAGE_TAG"

echo "[3/3] 校验镜像归档"
tar -tf "$OUTPUT" >/dev/null
sha256sum "$OUTPUT" > "$OUTPUT.sha256"
ls -lh "$OUTPUT" "$OUTPUT.sha256"
echo "加载命令：docker load -i $OUTPUT"
