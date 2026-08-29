#!/bin/sh
set -eu

mkdir -p /app/data/images/desktop /app/data/images/mobile /app/data/database /app/data/logs
chown -R appuser:appuser /app/data

exec gosu appuser "$@"
