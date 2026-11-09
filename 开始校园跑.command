#!/bin/zsh
set -u
cd -- "${0:A:h}" || exit 1
if [[ ! -x "$PWD/.lab/gps-env/bin/python" ]]; then
  print "请先按照 README.md 创建 Python 环境并安装依赖。"
  read 'runtime_finish?按回车关闭窗口。'
  exit 1
fi
"$PWD/.lab/gps-env/bin/python" -B emulator_route.py \
  --route .lab/routes/neusoft-track-route.json \
  --distance 1050 --pace 250 --cadence 175 --guided
result=$?
printf '\n按回车关闭这个窗口。\n'
read -r reply
exit "$result"
