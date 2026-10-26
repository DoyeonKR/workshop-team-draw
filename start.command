#!/bin/bash
# 더블클릭하면 팀뽑기 서버가 켜집니다. 종료는 이 창에서 Ctrl + C
cd "$(dirname "$0")"
python3 server.py "$@"
echo
read -n 1 -s -r -p "아무 키나 누르면 창을 닫습니다..."
