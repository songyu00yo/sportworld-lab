#!/bin/sh
set -eu
SPORTWORLD_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
. "$SPORTWORLD_ROOT/.lab/android-env.sh"
SPORTWORLD_PYTHON="$SPORTWORLD_ROOT/.lab/gps-env/bin/python"
if [ ! -x "$SPORTWORLD_PYTHON" ]; then
  printf '%s\n' 'The project Python environment is missing. Complete the Python setup in README.md first.' >&2
  exit 1
fi
case "$#:${1:-}" in
  0:) set -- -no-window ;;
  1:--window) set -- ;;
  *) printf '%s\n' 'Usage: scripts/start_android_tablet.sh [--window]' >&2; exit 2 ;;
esac
# Refuse occupied ports before starting or contacting any adb server.
"$SPORTWORLD_PYTHON" - <<'PY'
import os
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, str(Path(os.environ['ANDROID_LAB_ROOT']).parent.parent))
from emulator_motion import verify_adb_server
for port in (15556, 15557, 15037):
    result = subprocess.run(['lsof', '-nP', f'-iTCP:{port}', '-sTCP:LISTEN', '-Fp'],
                            capture_output=True, text=True, timeout=5)
    if result.returncode not in (0, 1):
        raise SystemExit('Unable to check Android port ownership with lsof.')
    if result.stdout.strip():
        if port != 15037:
            raise SystemExit(f'Port {port} is occupied. Stop the other Android environment first.')
        verify_adb_server()
PY
export XDG_CONFIG_HOME="$ANDROID_LAB_ROOT/xdg-config"
export XDG_CACHE_HOME="$ANDROID_LAB_ROOT/xdg-cache"
export XDG_DATA_HOME="$ANDROID_LAB_ROOT/xdg-data"
export QT_LOGGING_RULES='*.debug=false'
"$ANDROID_HOME/platform-tools/adb" -P "$ANDROID_ADB_SERVER_PORT" start-server
exec "$ANDROID_HOME/emulator/emulator" -avd sportworld_tablet34 \
  -ports 15556,15557 -datadir "$ANDROID_AVD_HOME/sportworld_tablet34.avd" -memory 3072 -cores 2 -gpu swiftshader -skin 1280x640 \
  "$@" -no-audio -camera-back none -camera-front none \
  -no-metrics -crash-report-mode disabled -no-snapshot -no-boot-anim
