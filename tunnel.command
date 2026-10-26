#!/bin/bash
# 더블클릭하면 팀뽑기 서버를 인터넷(휴대폰 데이터 포함)에서 열 수 있게 됩니다.
# start.command(서버)를 먼저 켜 두세요. 끄기는 이 창에서 Ctrl + C (끄면 인터넷 주소도 사라짐)
# 인터넷 주소가 끊기면(맥 잠자기·네트워크 변경 등) 알아서 새 주소로 다시 연결합니다. QR 은 새 주소로 자동으로 바뀝니다.
cd "$(dirname "$0")"
PORT="${1:-8023}"
CF="$(command -v cloudflared || echo /opt/homebrew/bin/cloudflared)"
LOG="$(mktemp -t qe_tunnel)"
CFPID=""
cleanup() { [ -n "$CFPID" ] && kill "$CFPID" 2>/dev/null; rm -f public_url.txt "$LOG"; }
trap 'cleanup; echo; echo "인터넷 접속을 껐습니다."; exit 0' INT TERM HUP
trap cleanup EXIT
caffeinate -i -w $$ &   # 이 창이 켜져 있는 동안 맥이 저절로 잠들지 않게 (덮개를 닫으면 잠듦)

alive() {  # 인터넷 주소로 서버까지 실제로 닿는지
  # 맥 자체 DNS 로 새 주소를 너무 일찍 물으면 '없는 주소'로 30분간 기억해 버려서, 바깥 DNS 로 직접 찾아 확인한다
  local host ip code
  host=${1#https://}; host=${host%%/*}
  ip=$(dig +short +time=3 +tries=1 "$host" @1.1.1.1 2>/dev/null | grep -E '^[0-9]+(\.[0-9]+){3}$' | head -1)
  [ -z "$ip" ] && ip=$(dig +short +time=3 +tries=1 "$host" @8.8.8.8 2>/dev/null | grep -E '^[0-9]+(\.[0-9]+){3}$' | head -1)
  [ -z "$ip" ] && ip=$(dig +short +time=3 +tries=1 "$host" 2>/dev/null | grep -E '^[0-9]+(\.[0-9]+){3}$' | head -1)
  [ -z "$ip" ] && return 1
  code=$(curl -s --noproxy '*' --resolve "$host:443:$ip" -o /dev/null -w "%{http_code}" --max-time 15 "${1}api/info")
  [ "$code" = "200" ]
}
local_up() { curl -s --noproxy '*' -o /dev/null --max-time 5 "http://localhost:$PORT/api/info"; }

while true; do
  rm -f public_url.txt; : > "$LOG"
  echo "[$(date +%H:%M:%S)] 인터넷 주소를 받는 중... (10초 정도)"
  "$CF" tunnel --no-autoupdate --url "http://localhost:$PORT" > "$LOG" 2>&1 &
  CFPID=$!
  URL=""
  for _ in $(seq 1 60); do
    sleep 1
    URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$LOG" | head -1)
    [ -n "$URL" ] && break
    kill -0 "$CFPID" 2>/dev/null || break
  done
  if [ -z "$URL" ]; then
    echo "[$(date +%H:%M:%S)] 주소를 못 받았어요(인터넷 연결 확인). 20초 뒤 다시 시도합니다."
    kill "$CFPID" 2>/dev/null; wait "$CFPID" 2>/dev/null; CFPID=""; sleep 20; continue
  fi
  URL="$URL/"
  ok=""
  for _ in $(seq 1 40); do alive "$URL" && { ok=1; break; }; sleep 3; done   # 새 주소가 퍼질 때까지 대기 (확인 전엔 QR 에 안 올림)
  if [ -z "$ok" ]; then
    echo "[$(date +%H:%M:%S)] 새 주소가 2분 안에 열리지 않았어요. 다시 받습니다."
    kill "$CFPID" 2>/dev/null; wait "$CFPID" 2>/dev/null; CFPID=""; sleep 5; continue
  fi
  echo "$URL" > public_url.txt
  echo
  echo "=================================================="
  echo "  인터넷 주소: $URL"
  echo "  팀뽑기 화면 [📱 휴대폰 접속 QR] 에 이 주소가 QR 로 뜹니다"
  echo "  이 창을 닫거나 Ctrl + C 하면 인터넷 접속이 끊깁니다"
  echo "=================================================="
  echo
  fails=0
  while kill -0 "$CFPID" 2>/dev/null; do   # 30초마다 확인, 3번 연속 안 닿으면 새 주소로 다시 연결
    sleep 30
    if ! local_up; then echo "[$(date +%H:%M:%S)] 서버가 꺼져 있어요 — start.command 를 켜 주세요"; fails=0; continue; fi
    if alive "$URL"; then fails=0; else fails=$((fails+1)); echo "[$(date +%H:%M:%S)] 인터넷 주소 응답 없음 ($fails/3)"; fi
    [ "$fails" -ge 3 ] && break
  done
  echo "[$(date +%H:%M:%S)] 인터넷 주소가 끊겼어요. 새 주소로 다시 연결합니다 (QR 은 자동으로 바뀜)"
  kill "$CFPID" 2>/dev/null; wait "$CFPID" 2>/dev/null; CFPID=""
  sleep 2
done
