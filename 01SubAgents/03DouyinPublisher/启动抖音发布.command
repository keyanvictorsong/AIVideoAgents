#!/bin/zsh
set -e
task_dir="${0:A:h}"
cd "$task_dir"
if [[ -x "$task_dir/../../.venv/bin/python" ]]; then
  task_python="$task_dir/../../.venv/bin/python"
elif [[ -x "$task_dir/.venv/bin/python" ]]; then
  task_python="$task_dir/.venv/bin/python"
else
  print '首次使用请按总目录 README.md 安装依赖。'
  read '?按回车关闭。'
  exit 1
fi
export PYTHONPATH="$task_dir${PYTHONPATH:+:$PYTHONPATH}"
exec "$task_python" -m douyin_agent.wizard
