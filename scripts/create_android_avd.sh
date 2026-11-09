#!/bin/sh
set -eu
SPORTWORLD_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
. "$SPORTWORLD_ROOT/.lab/android-env.sh"
SPORTWORLD_PYTHON="$SPORTWORLD_ROOT/.lab/gps-env/bin/python"
if [ ! -x "$SPORTWORLD_PYTHON" ]; then
  printf '%s\n' 'The project Python environment is missing. Complete the Python setup in README.md first.' >&2
  exit 1
fi
if ! command -v java >/dev/null 2>&1 || ! java -version >/dev/null 2>&1; then
  printf '%s\n' 'Install a Java 17 or newer JDK, then repeat bootstrap.py --android.' >&2
  exit 1
fi
# Let the terminal user review and accept Google's licenses interactively.
"$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" --sdk_root="$ANDROID_HOME" --licenses
if [ ! -f "$ANDROID_AVD_HOME/sportworld_tablet34.ini" ]; then
  printf 'no\n' | "$ANDROID_HOME/cmdline-tools/latest/bin/avdmanager" create avd \
    --name sportworld_tablet34 --package 'system-images;android-34;default;arm64-v8a' \
    --device pixel_tablet --path "$ANDROID_AVD_HOME/sportworld_tablet34.avd"
fi
"$SPORTWORLD_PYTHON" - <<'PY'
import os
from pathlib import Path
path = Path(os.environ['ANDROID_AVD_HOME']) / 'sportworld_tablet34.avd/config.ini'
values = dict(line.split('=', 1) for line in path.read_text().splitlines() if '=' in line)
values.update({'hw.audioInput': 'no', 'hw.audioOutput': 'no', 'hw.camera.back': 'none',
               'hw.camera.front': 'none', 'hw.lcd.width': '1280', 'hw.lcd.height': '640',
               'hw.lcd.density': '160', 'hw.ramSize': '3072', 'hw.keyboard': 'yes',
               'disk.dataPartition.size': '4G', 'skin.dynamic': 'yes', 'showDeviceFrame': 'no'})
path.write_text(''.join(f'{key}={value}\n' for key, value in values.items()))
PY
printf '%s\n' 'The isolated tablet AVD is created and remains stopped.'
