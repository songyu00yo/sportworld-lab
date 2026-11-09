#!/bin/zsh
set -u
cd -- "${0:A:h}" || exit 1
if [[ ! -x "$PWD/.lab/gps-env/bin/python" ]]; then
  print "请先按照 README.md 创建 Python 环境并安装依赖。"
  read 'runtime_finish?按回车关闭窗口。'
  exit 1
fi
"$PWD/.lab/gps-env/bin/python" -B runtime_manager.py start
result=$?
if (( result != 0 )); then
  print '\n启动未完成。请保留上面的提示。'
  read 'runtime_finish?按回车关闭窗口。'
fi
exit "$result"
