#!/bin/bash
# Run any command as the puma service account, with its env loaded.
exec sudo -u puma bash -c '
  set -a
  source /etc/puma/puma.env
  set +a
  exec /var/www/puma/.venv/bin/"$@"
' bash "$@"
