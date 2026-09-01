#!/bin/sh
set -eu

mkdir -p /app/data/images/desktop /app/data/images/mobile /app/data/database /app/data/logs /app/data/cache/webdav/tmp /app/data/tmp/admin
chown -R appuser:appuser /app/data

exec gosu appuser "$@"
