#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
VERSION=0.8.17
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

build_product() {
  cp -a "$ROOT/extension" "$STAGE/kurukin-product"
  rm -rf "$STAGE/kurukin-product/tests" "$STAGE/kurukin-product/docs"
  (cd "$STAGE/kurukin-product" && python3 -m zipfile -c "$ROOT/kurukin-extension-$VERSION.zip" .)
}

build_admin() {
  cp -a "$ROOT/extension" "$STAGE/kurukin-admin"
  rm -rf "$STAGE/kurukin-admin/tests" "$STAGE/kurukin-admin/docs"
  sed -i \
    -e 's/"name": "Kurukin Intelligence"/"name": "Kurukin Intelligence ADMIN"/' \
    -e 's/"short_name": "Kurukin"/"short_name": "Kurukin Admin"/' \
    -e 's/"description": "/"description": "ADMIN DEBUG — /' \
    "$STAGE/kurukin-admin/manifest.json"
  sed -i 's/ADMIN_DEBUG_MODE:false/ADMIN_DEBUG_MODE:true/' "$STAGE/kurukin-admin/panel/config.js"
  (cd "$STAGE/kurukin-admin" && python3 -m zipfile -c "$ROOT/kurukin-extension-$VERSION-admin-debug.zip" .)
}

rm -f "$ROOT/kurukin-extension-$VERSION.zip" "$ROOT/kurukin-extension-$VERSION-admin-debug.zip" "$ROOT/kurukin-extension.zip"
build_product
build_admin
cp "$ROOT/kurukin-extension-$VERSION.zip" "$ROOT/kurukin-extension.zip"
