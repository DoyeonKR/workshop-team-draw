#!/usr/bin/env python3
"""10월23일 QE합동 워크샾 팀뽑기 서버.

Python 3 표준 라이브러리만 사용한다(설치할 것 없음).
    python3 server.py              # 0.0.0.0:8023 으로 실행 (같은 네트워크의 다른 PC·휴대폰에서 접속 가능)
    python3 server.py --port 9000  # 포트 변경
추첨 결과는 같은 폴더의 data.json 에 저장되어 서버를 껐다 켜도 유지된다.
"""
import argparse
import json
import os
import re
import secrets
import socketserver
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data.json")
INDEX = os.path.join(BASE, "index.html")
GAMES = os.path.join(BASE, "games.html")
GAME_PASSWORD = ""  # 게임 진행 화면(/games)·제시어 화면(/word) 비밀번호. 빈 값 = 비밀번호 없이 바로 입장 (2026-10-23 행사 당일 해제, 다시 걸려면 "0000" 등으로)
# 들어갈 때마다(새로고침 포함) 비밀번호를 묻는다:
#   비밀번호 통과 → 1회용 입장권(60초) → 그 입장권으로 화면을 한 번 열면 그 화면 전용 세션 토큰을 심어 줌.
#   새로고침·재입장 때는 입장권이 이미 쓰였으므로 다시 비밀번호 화면. (쿠키를 쓰지 않음)
GAME_TICKETS = {}   # 입장권 → (발급 시각 ms, 화면 "games"|"word")
GAME_SESSIONS = {}  # 화면 세션 토큰 → 발급 시각 ms
TICKET_TTL_MS = 60 * 1000
SESSION_TTL_MS = 24 * 3600 * 1000
WORD = os.path.join(BASE, "word.html")
VOTE = os.path.join(BASE, "vote.html")
MVP = os.path.join(BASE, "mvp.html")
STAY = os.path.join(BASE, "stay.html")
MENU = os.path.join(BASE, "menu.html")  # 2차 메뉴 입력 (휴대폰, 이름 없이 바로 입력)  # 남겠다/집에 가겠다 투표 (휴대폰, 비밀번호 없음)  # 참여왕 투표 (휴대폰, 비밀번호 없음, 투표만 가능)
TIMETABLE = os.path.join(BASE, "timetable.html")  # 전체화면 타임테이블 (비밀번호 없음)  # 특별상 투표 화면 (비밀번호 없음, 투표만 가능)

# 참가자 본명 명단 (닉네임 입력 후 이 중에서 자기 이름을 고른다)
ROSTER = [
    "참가자01", "참가자02", "참가자03",
    "참가자04", "참가자05", "참가자06",
    "참가자07", "참가자08", "참가자09",
    "참가자10", "참가자11", "참가자12",
    "사회자", "참가자13", "참가자14",
    "참가자15", "참가자16",
    "참가자17",
    "참가자18",
]
# 본명이 여기 있으면 무조건 리더팀 (이 5명만 리더팀)
LEADERS = ["참가자01", "참가자04", "참가자07", "참가자03", "참가자02"]
ADMIN = "사회자"  # 사회자. 본명에서 고르면 관리자 화면으로 들어간다 (추첨·총원에는 포함 안 함)
GOAL = 18  # 추첨 총원(사회자 제외)
LEADER_EXTRA = 1  # 고정 리더팀 외에 나머지 사람 중 랜덤으로 리더팀에 들어가는 인원
L_SIZE = len(LEADERS) + LEADER_EXTRA  # 리더팀 인원 (6) → 1·2팀 = 총원 - 리더팀 = 12 (6/6)
TEAMS = ("L", "1", "2")
REVEAL_SEC = 25  # 추첨한 사람의 연출이 끝나기 전에 다른 화면에 결과가 먼저 뜨지 않도록 숨기는 최대 시간
STATE_V = 2

cond = threading.Condition()  # 상태 변경 보호 + 실시간 알림
ADMIN_TOKEN = secrets.token_urlsafe(18)
PORT = 8023


def clean_team_name(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()[:16]


def norm(s):
    return re.sub(r"\s+", "", str(s or "")).lower()


def clean(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()[:20]


def now_ms():
    return int(time.time() * 1000)


def tl(t):
    return "리더팀" if t == "L" else f"{t}팀"


LEADER_KEYS = {norm(n) for n in LEADERS}


def is_leader_name(real):
    return norm(real) in LEADER_KEYS


# ---------------- 게임 (빙고 · 몸으로 말해요 · 캐치마인드 · 최종 랭킹) ----------------
# 진행자 화면 하나에서만 진행 (로그인·화면 구분 없음)
GT = ("L", "1", "2")
RANK_POINTS = (3, 2, 1)  # 게임별 1위 3점 · 2위 2점 · 3위 1점 (공동 순위는 같은 점수)
PRIZES = ("소정의 상품", "무임승차", "식사 셋팅 및 고기굽기")

# 빙고 16칸: (제목, 설명, [(링크 이름, 유튜브 주소), ...]). 칸마다 퀴즈 영상 → 가장 많이 맞힌 팀이 칸을 가져감.
BINGO_DEFAULT = [
    ("90년대 노래", "30곡 · 1초 듣기", [("영상", "https://youtu.be/NsBFp2gvIvM?si=i9b4iKo4e2zN5V7s")]),
    ("00년대 노래", "30곡 · 1초 듣기", [("영상", "https://youtu.be/0U9lrhLX3-Y?si=nIm0gqjbCJzwpsgQ")]),
    ("10년대 노래", "30곡 · 1초 듣기", [("영상", "https://youtu.be/O6sYn9nEUxk?si=Qzl3sFnbKD7eTzVr")]),
    ("20년대 노래", "30곡 · 1초 듣기", [("영상", "https://youtu.be/Wp8_FPicKqE?si=V-fzx3RBwIOvIa_p")]),
    ("가수", "50명", [("영상", "https://www.youtube.com/watch?v=C8mIgOjkvQg")]),
    ("배우", "한국 배우", [("영상", "https://www.youtube.com/watch?v=sL--NaTR0uQ")]),
    ("해외 배우", "50명", [("영상", "https://youtu.be/iSZRYhXqKbA?si=5-klVn_iPL9Spt2N")]),
    ("영화 명대사", "1~3편", [("1편", "https://youtu.be/cSMpfzu5XgE?si=yzzuJwbQqL1xAYS1"),
                          ("2편", "https://youtu.be/qMRoHta3fAQ?si=ucglhLr6FuOkMDUW"),
                          ("3편", "https://youtu.be/ZKOS67jrIrE?si=Sh9F5ahwTME2N9-E")]),
    ("OST", "드라마 · 영화", [("드라마", "https://youtu.be/13UJ36vNfC4?si=gBx7m3DCIJ9MvIuj"),
                           ("영화 1편", "https://youtu.be/6vw1hDzOlRs?si=AgQMhiECwXPV-Z6Y"),
                           ("영화 2편", "https://youtu.be/-An5boKQ2MY?si=-Uf11QKKqQhpHqEq")]),
    ("애니메이션 노래", "", [("영상", "https://youtu.be/es4Wesa_ISQ?si=ufJ1epu9UxjCdQXH")]),
    ("속담", "100개", [("영상", "https://youtu.be/hNvaczCNsXU?si=cuOOQdsIRZixdo9p")]),
    ("이목구비 인물", "100명", [("영상", "https://youtu.be/tTY82IpGM_E?si=U1yG9LXIjxPgTdna")]),
    ("상식 퀴즈", "30개", [("영상", "https://www.youtube.com/watch?v=DPGQnk61GQM")]),
    ("수도 맞추기", "100개", [("영상", "https://www.youtube.com/watch?v=-dCKzwtQfks&list=PLrtGzBLErdrnMYiYKpvIkDMurFERE9Bvj&index=2")]),
    ("로고 맞추기", "100개", [("영상", "https://www.youtube.com/watch?v=LxWWhJgsMec&t=633s")]),
    ("국기 맞추기", "100개", [("영상", "https://www.youtube.com/watch?v=dcsAhWu8D1E")]),
]
# 2026-10-19 oEmbed 확인: 아래 영상은 올린 사람이 '퍼가기'를 막아 페이지 안 재생 불가 → 팝업 창으로 연다
NO_EMBED = {"NsBFp2gvIvM", "0U9lrhLX3-Y", "O6sYn9nEUxk", "Wp8_FPicKqE", "6vw1hDzOlRs", "-An5boKQ2MY",
            "es4Wesa_ISQ", "dcsAhWu8D1E"}

# 제시어는 모두 명사(사물·동물·음식·탈것·직업·장소·운동 종목). 동작·행동 표현(…하기, …기, 하품 등)과 뜻이 여러 개인 말(배·말·소·게)은 넣지 않는다 (2026-10-23)
CHARADES_DEFAULT = {
    "L": ["양궁", "비행기", "사자", "제기", "피아노", "낚시", "소방관", "김밥", "태권도", "우산",
          "펭귄", "자전거", "마술사", "라면", "볼링", "거울", "코끼리", "요가", "택시", "노래방",
          "캥거루", "미용사", "축구", "돛단배", "바이올린", "스키", "지휘자", "떡볶이", "도끼", "휴대폰",
          "문어", "오토바이", "가수", "아이스크림", "줄자", "망치", "독수리", "확성기", "소방차", "캠핑",
          "닭", "우체부", "배드민턴", "장구", "트럼펫", "썰매", "화가", "수박", "씨름", "카메라",
          "개구리", "잠수함", "군인", "솜사탕", "스케이트보드", "빗자루", "나무늘보", "트라이앵글", "엘리베이터", "요요",
          "코뿔소", "거미", "달팽이", "얼룩말", "다람쥐", "홍학", "표범", "오징어", "타조", "치타",
          "스노보드", "비치발리볼", "봅슬레이", "사격", "럭비", "트램펄린", "카약", "방패", "허들", "검도",
          "농부", "목수", "웨이터", "치과의사", "DJ", "피에로", "우주비행사", "해녀", "유튜버", "QA 테스터",
          "망토", "하이힐", "오리발", "인라인스케이트", "불도저", "손수레", "샌드백", "방패연", "북극곰", "도마뱀",
          "전갈", "매미", "병아리", "침팬지", "하이에나", "경찰차", "오리배", "킥보드", "기관사", "비빔밥",
          "군고구마", "콜라", "꽈배기", "장갑", "마스크", "프린터", "고데기", "풍선", "생일 케이크", "열쇠"],
    "1": ["수영", "기린", "헬리콥터", "탐정", "기타", "등산", "경찰", "피자", "권투", "선풍기",
          "원숭이", "스케이트", "요리사", "햄버거", "골프", "손목시계", "고릴라", "지하철", "조랑말", "청소기",
          "토끼", "의사", "야구", "면도기", "드럼", "서핑", "사진사", "짜장면", "펜싱", "냉장고",
          "오리", "로켓", "아나운서", "팝콘", "팔씨름", "가위", "부엉이", "빨래집게", "구급차", "청진기",
          "돼지", "택배기사", "탁구", "무전기", "하모니카", "승마", "조각가", "바나나", "유도", "텔레비전",
          "거북이", "하프", "선생님", "핫도그", "훌라후프", "다리미", "박쥐", "지팡이", "회전목마", "마라톤",
          "하마", "나비", "앵무새", "판다", "고래", "두더지", "늑대", "꽃게", "백조", "여우",
          "다이빙", "컬링", "카누", "핸드볼", "체조", "물총", "스카이다이빙", "투구", "투포환", "족구",
          "어부", "배관공", "계산원", "수의사", "래퍼", "경호원", "카레이서", "스님", "프로게이머", "모델",
          "우비", "수경", "튜브", "지게차", "사다리차", "정글짐", "아령", "펠리컨", "스컹크", "미끄럼틀",
          "가오리", "사마귀", "잠자리", "칠면조", "알파카", "포클레인", "유모차", "인력거", "짬뽕", "삼계탕",
          "소주", "츄러스", "돗자리", "목도리", "리모컨", "체중계", "손전등", "폭죽", "선물 상자", "시계탑"],
    "2": ["농구", "고양이", "버스", "그네", "첼로", "모닥불", "간호사", "치킨", "레슬링", "전자레인지",
          "뱀", "기차", "기자", "호떡", "림보", "노트북", "상어", "열기구", "판사", "탕후루",
          "배구", "강아지", "트럭", "젖소", "플루트", "낙하산", "제빵사", "삼겹살", "손톱깎이", "세탁기",
          "공룡", "요트", "개그맨", "붕어빵", "국자", "망원경", "고슴도치", "우주선", "주사기", "에어컨",
          "쌍안경", "경비원", "테니스", "아코디언", "탬버린", "번지점프", "발레리나", "컵케이크", "부메랑", "전화기",
          "새총", "비행사", "당구", "가면", "실로폰", "롤러코스터", "마라토너", "도넛", "팽이", "선글라스",
          "공작새", "모기", "낙타", "코알라", "해파리", "악어", "미어캣", "가재", "수달", "너구리",
          "역도", "피겨스케이팅", "암벽등반", "아이스하키", "리듬체조", "롤러스케이트", "사이클", "슬리퍼", "구명조끼", "수상스키",
          "광부", "바리스타", "환경미화원", "약사", "저글러", "선장", "뗏목", "산타클로스", "개발자", "무용수",
          "트랙터", "캠핑카", "철봉", "러닝머신", "물개", "카멜레온", "해마", "메뚜기", "개미", "시소",
          "딱따구리", "순록", "승무원", "정원사", "대장장이", "크레인", "휠체어", "옥수수", "냉면", "계란말이",
          "맥주", "랍스터", "모자", "안경", "마우스", "드라이기", "나침반", "촛불", "자물쇠", "우체통"],
}
CATCH_DEFAULT = ["눈사람", "에펠탑", "햄버거", "무지개", "우주인", "피라미드", "해바라기", "크리스마스트리", "고양이", "노트북",
                 "커피", "바나나", "산타클로스", "등대", "문어", "축구공", "공룡", "로봇", "텐트", "바베큐",
                 "키보드", "수박", "선인장", "스마트폰", "해적", "왕관", "거북이", "롤러코스터", "캠프파이어", "자유의 여신상",
                 "돋보기", "비눗방울", "놀이공원", "고슴도치", "치킨", "펭귄", "기린", "달팽이", "무당벌레", "해파리",
                 "오징어", "고래", "독수리", "부엉이", "공작새", "판다", "낙타", "다람쥐", "우산", "안경",
                 "시계", "가위", "연필", "자전거", "비행기", "헬리콥터", "잠수함", "소방차", "신호등", "엘리베이터",
                 "냉장고", "선풍기", "전구", "망원경", "카메라", "헤드폰", "마이크", "피아노", "기타", "트럼펫",
                 "화산", "무인도", "폭포", "사막", "번개", "은하수", "피자", "김밥", "떡볶이", "라면",
                 "아이스크림", "케이크", "도넛", "핫도그", "초밥", "붕어빵", "허수아비", "돌고래", "풍차", "이글루",
                 "남산타워", "숭례문", "경복궁", "첨성대", "만리장성", "스핑크스", "피사의 사탑", "빅벤", "오페라하우스", "타지마할",
                 "코끼리", "사자", "호랑이", "얼룩말", "하마", "코뿔소", "원숭이", "토끼", "돼지", "개구리",
                 "상어", "불가사리", "앵무새", "홍학", "꿀벌", "나비", "거미", "뱀", "악어", "유니콘",
                 "칫솔", "숟가락", "젓가락", "냄비", "프라이팬", "주전자", "머그컵", "와인잔", "의자", "침대",
                 "소파", "책상", "모래시계", "지구본", "나침반", "풍선", "연", "낙하산", "열기구", "기차",
                 "유람선", "트럭", "경찰차", "구급차", "로켓", "우주선", "인공위성", "텔레비전", "리모컨", "마우스",
                 "프린터", "청소기", "세탁기", "다리미", "드라이기", "손전등", "촛불", "크레파스", "물감", "책",
                 "가방", "지갑", "열쇠", "자물쇠", "반지", "목걸이", "넥타이", "운동화", "장화", "양말",
                 "김치", "비빔밥", "삼겹살", "짜장면", "팥빙수", "옥수수", "당근", "브로콜리", "파인애플", "포도"]
STROKES = {"epoch": 0, "segs": []}  # 캐치마인드 그림 (메모리만, 저장 안 함)


def yt_id(url):
    m = re.search(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|embed/|shorts/|live/)|youtu\.be/)([\w-]{11})", str(url or ""))
    return m.group(1) if m else None


def link_out(lk):
    vid = yt_id(lk["url"])
    t = re.search(r"[?&]t=(?:(\d+)h)?(?:(\d+)m)?(\d+)s?", lk["url"])
    start = (int(t.group(1) or 0) * 3600 + int(t.group(2) or 0) * 60 + int(t.group(3) or 0)) if t else 0
    return {"label": lk["label"], "url": lk["url"], "vid": vid, "start": start,
            "embed": bool(vid) and vid not in NO_EMBED and not lk.get("noEmbed")}


def new_counts():
    return {t: 0 for t in GT}


def default_games():
    cells = [{"title": t, "sub": s, "links": [{"label": a, "url": u} for a, u in ls], "counts": new_counts(),
              "owner": None, "seq": 0} for t, s, ls in BINGO_DEFAULT]
    return {
        "v": 2,
        "bingo": {"cells": cells, "seq": 0, "finished": False, "openIdx": None},
        "charades": {"limit": 420, "words": {t: list(CHARADES_DEFAULT[t]) for t in GT}, "used": [],
                     "results": {t: None for t in GT}, "live": None, "finished": False},
        "catch": {"limit": 420, "words": list(CATCH_DEFAULT), "used": [], "results": {t: None for t in GT},
                  "live": None, "finished": False},
        "bonus": {t: 0 for t in GT},
        "reveal": 0,
        "vote": new_vote(),
        "mvp": new_vote(),  # 참여왕 투표 (모두 휴대폰으로 QR 찍고 한 사람에게 한 표)
        "stay": {"votes": {}, "phase": "open"},
        "menu": {"items": [], "phase": "open"},  # 2차 먹고 싶은 메뉴 (이름 없이 입력, 같은 메뉴는 묶어서 셈)  # 남겠다/집에 가겠다 (본명 → "stay"|"home", 다시 누르면 바꿈)
    }


def _round_result(r):
    if not isinstance(r, dict):
        return None
    return {"correct": int(r.get("correct") or 0), "pass": int(r.get("pass") or 0),
            "words": [str(w) for w in (r.get("words") or [])][:300],
            "passed": [str(w) for w in (r.get("passed") or [])][:300], "at": int(r.get("at") or 0)}


def sanitize_games(g):
    d = default_games()
    if not isinstance(g, dict) or g.get("v") != 2:
        return d  # 예전 형식(1차 버전)은 새 기본값으로
    try:
        b = g.get("bingo") or {}
        if isinstance(b.get("cells"), list) and len(b["cells"]) == 16:
            cells = []
            for c in b["cells"]:
                links = [{"label": str(lk.get("label") or "영상")[:20], "url": str(lk.get("url") or "")[:500]}
                         for lk in (c.get("links") or []) if isinstance(lk, dict) and lk.get("url")][:6]
                cells.append({"title": str(c.get("title") or "")[:20], "sub": str(c.get("sub") or "")[:30],
                              "links": links, "counts": {t: max(0, int((c.get("counts") or {}).get(t) or 0)) for t in GT},
                              "owner": c.get("owner") if c.get("owner") in GT + ("X",) else None,
                              "seq": int(c.get("seq") or 0)})
            d["bingo"] = {"cells": cells, "seq": int(b.get("seq") or 0), "finished": bool(b.get("finished")),
                          "openIdx": None}
        for key in ("charades", "catch"):
            c = g.get(key) or {}
            d[key]["limit"] = max(30, min(1800, int(c.get("limit") or 420)))
            d[key]["results"] = {t: _round_result((c.get("results") or {}).get(t)) for t in GT}
            d[key]["finished"] = bool(c.get("finished"))  # '마치고 집계하기'로 확정했는지
            lv = c.get("live")  # 진행 중 라운드도 보존 → 서버를 재시작해도 이어서 진행 (시간은 절대 시각 기준)
            if isinstance(lv, dict) and lv.get("team") in GT and isinstance(lv.get("ends"), int):
                d[key]["live"] = {"team": lv["team"], "queue": [str(w) for w in (lv.get("queue") or [])],
                                  "word": lv.get("word"), "correct": [str(w) for w in (lv.get("correct") or [])],
                                  "passed": [str(w) for w in (lv.get("passed") or [])], "ends": lv["ends"],
                                  "paused": lv.get("paused") if isinstance(lv.get("paused"), int) else None,
                                  "wseq": int(lv.get("wseq") or 0)}
        cw = (g.get("charades") or {}).get("words")
        if isinstance(cw, dict):
            d["charades"]["words"] = {t: [clean(w) for w in (cw.get(t) or []) if clean(w)][:200] for t in GT}
        k = g.get("catch") or {}
        if isinstance(k.get("words"), list):
            d["catch"]["words"] = [clean(w) for w in k["words"] if clean(w)][:400]
        d["catch"]["used"] = [clean(w) for w in (k.get("used") or []) if clean(w)]
        d["charades"]["used"] = [clean(w) for w in ((g.get("charades") or {}).get("used") or []) if clean(w)]
        d["bonus"] = {t: int((g.get("bonus") or {}).get(t) or 0) for t in GT}
        d["reveal"] = max(0, min(3, int(g.get("reveal") or 0)))
        mn = g.get("menu") or {}
        d["menu"] = {"items": [{"text": str(i.get("text"))[:MENU_MAX], "at": int(i.get("at") or 0)}
                               for i in (mn.get("items") or []) if isinstance(i, dict) and str(i.get("text") or "").strip()][-MENU_LIMIT:],
                     "phase": "closed" if mn.get("phase") == "closed" else "open"}
        st = g.get("stay") or {}
        d["stay"] = {"votes": {str(k): c for k, c in (st.get("votes") or {}).items() if c in STAY_CHOICES},
                     "phase": "closed" if st.get("phase") == "closed" else "open"}
        for vk in ("vote", "mvp"):
            v = g.get(vk) or {}
            d[vk] = {"votes": {str(k): str(c) for k, c in (v.get("votes") or {}).items()},
                     "phase": "revealed" if v.get("phase") == "revealed" else "open",
                     "round": max(1, int(v.get("round") or 1)),
                     "pool": [str(x) for x in v["pool"]] if isinstance(v.get("pool"), list) else None}
    except (TypeError, ValueError, AttributeError):
        return default_games()
    return d


def G():
    if not isinstance(S.get("games"), dict) or S["games"].get("v") != 2:
        S["games"] = default_games()
    return S["games"]


def comp_rank(vals):
    """{team: 값} → {team: 순위} (높을수록 좋음, 공동 순위 1-1-3 방식)."""
    return {t: 1 + sum(1 for o in vals if vals[o] > vals[t]) for t in vals}


# ----- 빙고 -----
BINGO_LINES = [[r * 4 + c for c in range(4)] for r in range(4)] + [[r * 4 + c for r in range(4)] for c in range(4)] \
    + [[0, 5, 10, 15], [3, 6, 9, 12]]


def cell_leader(c):
    top = max(c["counts"].values())
    tops = [t for t in GT if c["counts"][t] == top]
    return (tops[0] if len(tops) == 1 and top > 0 else None), tops, top


def bingo_result():
    cells = G()["bingo"]["cells"]
    counts = {t: sum(1 for c in cells if c["owner"] == t) for t in GT}
    best = None  # (완성 순서, 팀, 줄)
    for line in BINGO_LINES:
        t = cells[line[0]]["owner"]
        if t in GT and all(cells[i]["owner"] == t for i in line):
            done_at = max(cells[i]["seq"] for i in line)
            if best is None or done_at < best[0]:
                best = (done_at, t, line)
    resolved = sum(1 for c in cells if c["owner"] is not None)
    if best:
        winner, how, line = best[1], "bingo", best[2]
    elif G()["bingo"]["finished"] or resolved == 16:
        top = max(counts.values())
        tops = [t for t in GT if counts[t] == top]
        winner, how, line = (tops[0] if len(tops) == 1 and top > 0 else None), "count", None
    else:
        winner, how, line = None, None, None
    rank = None
    if how:
        rank = comp_rank({t: (1 if (how == "bingo" and t == winner) else 0) * 100 + counts[t] for t in GT})
    return {"counts": counts, "winner": winner, "how": how, "line": line, "decided": how is not None,
            "resolved": resolved, "rank": rank}


# ----- 7분 라운드 (몸으로 말해요 · 캐치마인드 공통) -----
def time_left(lv, now=None):
    if not lv:
        return 0
    if lv.get("paused") is not None:
        return lv["paused"]
    return max(0, lv["ends"] - (now or now_ms()))


def round_finalize(key):
    g = G()[key]
    lv = g["live"]
    if not lv:
        return
    g["results"][lv["team"]] = {"correct": len(lv["correct"]), "pass": len(lv["passed"]), "words": lv["correct"],
                                "passed": lv["passed"], "at": now_ms()}
    g["live"] = None
    if key == "catch":
        STROKES["epoch"] += 1
        STROKES["segs"] = []


def used_words():
    """지금까지 낸 제시어 (몸으로 말해요 + 캐치마인드 합쳐서). 한 번 나온 단어는 어느 게임에서도 다시 안 나온다."""
    g = G()
    return set(g["charades"].get("used") or []) | set(g["catch"].get("used") or [])


def next_word(key):
    """제시어를 무작위로 하나 뽑는다. 이미 나온 단어(두 게임 통틀어)는 제외."""
    g = G()[key]
    lv = g["live"]
    used = used_words()
    src = g["words"][lv["team"]] if key == "charades" else g["words"]
    pool = list(dict.fromkeys(w for w in src if w not in used))  # 목록 안 중복도 한 번만
    lv["word"] = pool[secrets.randbelow(len(pool))] if pool else None
    if lv["word"]:
        g.setdefault("used", []).append(lv["word"])
    lv["wseq"] = lv.get("wseq", 0) + 1
    if key == "catch":
        STROKES["epoch"] += 1  # 새 제시어마다 그림판 비우기
        STROKES["segs"] = []
    if lv["word"] is None:
        round_finalize(key)


def round_tick(key):
    lv = G()[key]["live"]
    if lv and lv.get("paused") is None and now_ms() >= lv["ends"]:
        round_finalize(key)
        return True
    return False


def games_tick():
    """cond 를 잡은 상태에서 호출. 7분이 끝난 라운드를 마감하고 바뀌었으면 저장·알림."""
    a = round_tick("charades")
    b = round_tick("catch")
    if a or b:
        changed()


def timers_running():
    g = G()
    return any(g[k]["live"] and g[k]["live"].get("paused") is None for k in ("charades", "catch"))


def round_view(key, now):
    g = G()[key]
    lv = g["live"]
    live = None
    if lv:
        live = {"team": lv["team"], "word": lv["word"], "remain": time_left(lv, now), "paused": lv.get("paused") is not None,
                "correct": len(lv["correct"]), "pass": len(lv["passed"]), "wseq": lv.get("wseq", 0),
                "lastCorrect": lv["correct"][-1] if lv["correct"] else None}
    return {"limit": g["limit"], "results": g["results"], "live": live, "finished": bool(g.get("finished"))}


def standings():
    g = G()
    br = bingo_result()
    games = {"bingo": {"decided": br["decided"], "raw": br["counts"], "rank": br["rank"]}}
    for key in ("charades", "catch"):
        res = g[key]["results"]
        vals = {t: (res[t]["correct"] if res[t] else 0) for t in GT}  # 안 한 팀은 0개
        done = all(res[t] for t in GT) or bool(g[key].get("finished"))
        games[key] = {"decided": done, "raw": vals, "rank": comp_rank(vals) if done else None}
    pts = {t: 0 for t in GT}
    wins = {t: 0 for t in GT}
    for v in games.values():
        v["points"] = None
        if v["rank"]:
            v["points"] = {t: RANK_POINTS[v["rank"][t] - 1] for t in GT}
            for t in GT:
                pts[t] += v["points"][t]
                wins[t] += 1 if v["rank"][t] == 1 else 0
    total = {t: pts[t] + g["bonus"][t] for t in GT}
    final_rank = comp_rank({t: total[t] * 100 + wins[t] for t in GT})
    return {"games": games, "points": pts, "bonus": dict(g["bonus"]), "total": total, "wins": wins,
            "rank": final_rank, "allDecided": all(v["decided"] for v in games.values()),
            "prizes": {t: PRIZES[min(final_rank[t], 3) - 1] for t in GT}}


# ----- 닉네임 특별상 투표 (진행자 화면 한 대에서 한 명씩) -----
def new_vote():
    return {"votes": {}, "phase": "open", "round": 1, "pool": None}  # votes: 본명 → 후보 member id (비밀)


def vote_candidates(key="vote"):
    v = G()[key]
    pool = set(v["pool"]) if v.get("pool") else None
    return [m for m in S["members"] if not is_pending(m) and (pool is None or m["id"] in pool)]


def vote_voters():
    return list(S["roster"])  # 명단 전원(사회자 포함) 한 표씩


def vote_result(key="vote"):
    v = G()[key]
    counts = {}
    for cid in v["votes"].values():
        counts[cid] = counts.get(cid, 0) + 1
    rows = []
    for m in vote_candidates(key):
        rows.append({"id": m["id"], "name": m["name"], "real": m["real"], "team": m["team"], "votes": counts.get(m["id"], 0)})
    rows.sort(key=lambda r: -r["votes"])
    top = rows[0]["votes"] if rows else 0
    winners = [r for r in rows if r["votes"] == top and top > 0]
    return {"rows": rows, "top": top, "winners": winners, "tie": len(winners) > 1}


def vote_view(key="vote"):
    v = G()[key]
    voted = set(v["votes"])
    out = {"phase": v["phase"], "round": v["round"], "runoff": bool(v.get("pool")),
           "voters": [{"name": r, "voted": r in voted, "host": norm(r) == norm(ADMIN)} for r in vote_voters()],
           "votedCount": len(voted), "total": len(vote_voters()),
           "candidates": [{"id": m["id"], "name": m["name"], "team": m["team"], "realKey": norm(m["real"]),
                           "real": m["real"]} for m in vote_candidates(key)]}
    out["result"] = vote_result(key)  # 득표 현황은 투표 중에도 공개 (누가 누구를 찍었는지는 저장 안 함)
    return out


STAY_CHOICES = {"stay": "남겠다", "home": "집에 가겠다"}
MENU_MAX, MENU_LIMIT = 20, 400  # 메뉴 한 개 글자 수 · 전체 개수 상한


def menu_key(t):
    return re.sub(r"[\s~!.?·,]+", "", str(t)).lower()


def menu_view():
    m = G()["menu"]
    groups = {}
    for it in m["items"]:
        k = menu_key(it["text"])
        if not k:
            continue
        gr = groups.setdefault(k, {"text": it["text"], "count": 0, "last": 0})
        gr["count"] += 1
        gr["last"] = max(gr["last"], it["at"])
    rows = sorted(groups.values(), key=lambda r: (-r["count"], -r["last"]))
    return {"phase": m["phase"], "total": len(m["items"]), "rows": rows}


def menu_action(a, b):
    m = G()["menu"]
    if a == "add":
        if m["phase"] != "open":
            raise Bad("메뉴 받기가 마감됐어요")
        t = re.sub(r"\s+", " ", str(b.get("text") or "")).strip()[:MENU_MAX]
        if not menu_key(t):
            raise Bad("먹고 싶은 메뉴를 적어 주세요")
        if len(m["items"]) >= MENU_LIMIT:
            raise Bad("메뉴가 너무 많이 모였어요")
        m["items"].append({"text": t, "at": now_ms()})
        print(f"[{time.strftime('%H:%M:%S')}] 2차 메뉴: {t}")
        return f"'{t}' 보냈어요!"
    if a == "remove":  # 진행자가 장난 입력 지우기 (같은 메뉴 묶음 통째로)
        k = menu_key(b.get("text"))
        m["items"] = [i for i in m["items"] if menu_key(i["text"]) != k]
        return "지웠어요"
    if a == "close":
        m["phase"] = "closed"
        return "메뉴 받기를 마감했어요"
    if a == "reopen":
        m["phase"] = "open"
        return "메뉴를 다시 받아요"
    if a == "reset":
        G()["menu"] = {"items": [], "phase": "open"}
        return "메뉴를 처음부터 다시 받아요"
    raise Bad("알 수 없는 동작입니다")


def stay_view():
    st = G()["stay"]
    voters = vote_voters()
    by = {c: [r for r in voters if st["votes"].get(r) == c] for c in STAY_CHOICES}
    return {"phase": st["phase"], "labels": STAY_CHOICES, "total": len(voters), "votedCount": len(st["votes"]),
            "voters": [{"name": r, "voted": r in st["votes"]} for r in voters],
            "counts": {c: len(v) for c, v in by.items()}, "names": by}  # 인원 파악용이라 이름 공개


def stay_action(a, b):
    st = G()["stay"]
    if a == "cast":
        voter = in_roster(b.get("voter"))
        if not voter:
            raise Bad("명단에서 본명을 골라 주세요")
        if st["phase"] != "open":
            raise Bad("투표가 마감됐어요")
        c = b.get("choice")
        if c not in STAY_CHOICES:
            raise Bad("남겠다 / 집에 가겠다 중에 골라 주세요")
        before = st["votes"].get(voter)
        st["votes"][voter] = c
        print(f"[{time.strftime('%H:%M:%S')}] 남/집 투표: {voter} → {STAY_CHOICES[c]}")
        return f"{voter}님: {STAY_CHOICES[c]}" + (" (바꿨어요)" if before and before != c else "")
    if a == "close":
        st["phase"] = "closed"
        return "남/집 투표를 마감했어요"
    if a == "reopen":
        st["phase"] = "open"
        return "남/집 투표를 다시 받아요"
    if a == "cancel":
        voter = in_roster(b.get("voter"))
        st["votes"].pop(voter, None)
        return f"{voter}님 표를 지웠어요"
    if a == "reset":
        G()["stay"] = {"votes": {}, "phase": "open"}
        return "남/집 투표를 처음부터 다시 받아요"
    raise Bad("알 수 없는 동작입니다")


def vote_action(a, b, key="vote"):
    """key: "vote" = 닉네임 특별상, "mvp" = 참여왕 (둘 다 1인 1표, 자기 자신 제외)."""
    v = G()[key]
    if a == "cast":
        voter = in_roster(b.get("voter"))
        if not voter:
            raise Bad("명단에서 본명을 골라 주세요")
        if v["phase"] != "open":
            raise Bad("투표가 마감됐어요")
        if voter in v["votes"]:
            raise Bad(f"{voter}님은 이미 투표했어요")
        cand = next((m for m in vote_candidates(key) if m["id"] == b.get("cand")), None)
        if not cand:
            raise Bad("후보를 다시 골라 주세요")
        if norm(cand["real"]) == norm(voter):
            raise Bad("자기 자신에게는 투표할 수 없어요" if key == "mvp" else "자기 닉네임에는 투표할 수 없어요")
        v["votes"][voter] = cand["id"]
        print(f"[{time.strftime('%H:%M:%S')}] {'참여왕' if key == 'mvp' else '특별상'} 투표: {voter} (누적 {len(v['votes'])}표)")
        return f"{voter}님 투표 완료!"
    if a == "cancel":
        voter = in_roster(b.get("voter"))
        if not voter or voter not in v["votes"]:
            raise Bad("투표하지 않은 사람이에요")
        v["votes"].pop(voter)
        return f"{voter}님의 표를 취소했어요. 다시 투표할 수 있어요"
    if a == "reveal":
        if not v["votes"]:
            raise Bad("아직 표가 없어요")
        v["phase"] = "revealed"
        return None
    if a == "reopen":
        v["phase"] = "open"
        return "발표를 취소했어요. 투표를 이어서 받아요"
    if a == "runoff":
        r = vote_result(key)
        if v["phase"] != "revealed" or not r["tie"]:
            raise Bad("동점일 때만 결선 투표를 할 수 있어요")
        v.update(votes={}, phase="open", round=v["round"] + 1, pool=[w["id"] for w in r["winners"]])
        return f"결선 투표 시작! 후보 {len(r['winners'])}명"
    if a == "reset":
        G()[key] = new_vote()
        return "투표를 처음부터 다시 받아요"
    raise Bad("알 수 없는 동작입니다")


def gview(admin=False):
    g = G()
    now = now_ms()
    b = g["bingo"]
    cells = []
    for c in b["cells"]:
        ld, tops, top = cell_leader(c)
        cells.append({"title": c["title"], "sub": c["sub"], "links": [link_out(lk) for lk in c["links"]],
                      "counts": c["counts"], "owner": c["owner"], "leader": ld, "tie": tops if (ld is None and top > 0) else []})
    ca = round_view("catch", now)
    if ca.get("live"):  # 캐치마인드 제시어는 그리는 사람 휴대폰(/api/word)으로만 — 큰 화면 데이터에서 뺀다
        ca["live"] = {k: v for k, v in ca["live"].items() if k != "word"}
    used = used_words()
    ca["left"] = len(set(w for w in g["catch"]["words"] if w not in used))
    ca["words"] = g["catch"]["words"]
    ch = round_view("charades", now)
    ch["words"] = g["charades"]["words"]
    return {
        "now": now, "reveal": g["reveal"],
        "teams": [{"id": t, "base": tl(t), "name": (S.get("teamNames") or {}).get(t) or ""} for t in GT],
        "members": {t: [m["name"] for m in team_of(t) if not is_pending(m)] for t in GT},
        "bingo": {"cells": cells, "openIdx": b["openIdx"], "finished": b["finished"], "result": bingo_result()},
        "charades": ch, "catch": ca,
        "standings": standings(), "rankPoints": RANK_POINTS, "prizeList": PRIZES, "vote": vote_view(), "mvp": vote_view("mvp"), "stay": stay_view(), "menu": menu_view(),
        "stroke": {"epoch": STROKES["epoch"], "n": len(STROKES["segs"])},
    }


def word_view():
    """캐치마인드 그리는 사람 휴대폰용: 지금 제시어만 (몸으로 말해요는 큰 화면으로 진행)."""
    g = G()
    out = {}
    for key in ("catch",):
        lv = g[key]["live"]
        out[key] = {"team": lv["team"], "word": lv["word"], "wseq": lv.get("wseq", 0),
                    "remain": time_left(lv), "paused": lv.get("paused") is not None,
                    "correct": len(lv["correct"]), "pass": len(lv["passed"])} if lv else None
    out["teams"] = {t: ((S.get("teamNames") or {}).get(t) or tl(t)) for t in GT}
    out["stroke"] = {"epoch": STROKES["epoch"], "n": len(STROKES["segs"])}  # 휴대폰 그림판 동기화용
    return out


def game_action(b):
    """cond 를 잡은 상태에서 호출."""
    g = G()
    game, a = b.get("game"), b.get("action")
    if game == "bingo":
        bg = g["bingo"]
        idx = b.get("idx")
        if a in ("open", "count", "award", "unaward", "zero"):
            if not isinstance(idx, int) or not 0 <= idx < 16:
                raise Bad("칸 번호가 올바르지 않습니다")
        if a == "open":
            bg["openIdx"] = idx
            return None
        if a == "close":
            bg["openIdx"] = None
            return None
        if a == "count":
            t = b.get("team")
            if t not in GT:
                raise Bad("팀 값이 올바르지 않습니다")
            c = bg["cells"][idx]
            c["counts"][t] = max(0, c["counts"][t] + (1 if (b.get("delta") or 1) > 0 else -1))
            return None
        if a == "zero":
            bg["cells"][idx]["counts"] = new_counts()
            return None
        if a == "award":
            c = bg["cells"][idx]
            t = b.get("team")
            if t is None:
                ld, tops, top = cell_leader(c)
                if top == 0:
                    raise Bad("아직 맞힌 팀이 없어요. 점수를 올리거나 '아무도 못 가져감'을 고르세요")
                if not ld:
                    raise Bad("동점이에요! 칸을 가져갈 팀을 직접 골라 주세요")
                t = ld
            if t not in GT + ("X",):
                raise Bad("팀 값이 올바르지 않습니다")
            c["owner"] = t
            bg["seq"] += 1
            c["seq"] = bg["seq"]
            bg["openIdx"] = None
            return None
        if a == "unaward":
            bg["cells"][idx].update(owner=None, seq=0)
            return None
        if a == "finish":  # 빙고가 안 나왔어도 지금 가진 칸 수로 집계
            bg["finished"] = bool(b.get("on", True))
            if bg["finished"]:
                bg["openIdx"] = None
            return "지금까지 가진 칸 수로 집계했어요" if bg["finished"] else "집계를 취소했어요. 이어서 진행하세요"
        if a == "edit":
            cells = b.get("cells")
            if not isinstance(cells, list) or len(cells) != 16:
                raise Bad("칸은 16개여야 합니다")
            for c, n in zip(bg["cells"], cells):
                c["title"] = str(n.get("title") or "").strip()[:20] or "문제"
                c["sub"] = str(n.get("sub") or "").strip()[:30]
                links = []
                for line in str(n.get("links") or "").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    label, _, url = line.rpartition("|") if "|" in line else ("", "", line)
                    if url.strip().startswith("http"):
                        links.append({"label": label.strip()[:20] or f"{len(links) + 1}편", "url": url.strip()[:500]})
                c["links"] = links[:6]
            return "빙고 칸을 저장했습니다"
        if a == "reset":
            for c in bg["cells"]:
                c.update(counts=new_counts(), owner=None, seq=0)
            bg.update(seq=0, finished=False, openIdx=None)
            return "빙고 판을 초기화했습니다"
    if game in ("charades", "catch"):
        r = g[game]
        lv = r["live"]
        if a == "start":
            t = b.get("team")
            if t not in GT:
                raise Bad("팀을 선택해 주세요")
            if lv:
                raise Bad("진행 중인 라운드가 있습니다")
            if r.get("finished"):
                raise Bad("이미 집계를 마쳤어요. 더 하려면 [집계 취소]를 누르세요")
            if r["results"][t]:
                raise Bad("이미 끝난 팀이에요. 다시 하려면 기록을 지우세요")
            r["live"] = {"team": t, "queue": [], "word": None, "correct": [], "passed": [],
                         "ends": now_ms() + r["limit"] * 1000, "paused": None, "wseq": 0}
            next_word(game)
            return None
        if a in ("correct", "pass", "pause", "resume", "end"):
            if not lv:
                raise Bad("진행 중인 라운드가 없습니다")
            if round_tick(game):
                return "시간이 끝났습니다"
        if a in ("correct", "pass"):
            if lv.get("paused") is not None:
                raise Bad("일시정지 중입니다")
            # 큰 화면과 휴대폰에서 거의 동시에 누르면 두 번 세지 않도록: 보고 누른 제시어 번호가 지금과 같을 때만
            if "wseq" in b and b.get("wseq") != lv.get("wseq"):
                raise Bad("방금 다른 화면에서 눌러서 다음 제시어로 넘어갔어요")
            (lv["correct"] if a == "correct" else lv["passed"]).append(lv["word"])
            next_word(game)
            return None
        if a == "pause":
            if lv.get("paused") is None:
                lv["paused"] = time_left(lv)
            return None
        if a == "resume":
            if lv.get("paused") is not None:
                lv["ends"] = now_ms() + lv["paused"]
                lv["paused"] = None
            return None
        if a == "end":
            round_finalize(game)
            return "라운드를 종료했습니다"
        if a == "finish":  # 다 못 끝냈어도 지금 결과로 순위 확정 (안 한 팀은 0개)
            on = bool(b.get("on", True))
            if on and lv:
                round_finalize(game)
            r["finished"] = on
            return "지금까지 결과로 집계했어요" if on else "집계를 취소했어요. 이어서 진행하세요"
        if a == "clear":
            t = b.get("team")
            if t not in GT:
                raise Bad("팀을 선택해 주세요")
            r["results"][t] = None
            return f"{tl(t)} 기록을 지웠습니다"
        if a == "adjust":
            t = b.get("team")
            if t not in GT or not r["results"][t]:
                raise Bad("기록이 있는 팀만 조정할 수 있습니다")
            r["results"][t]["correct"] = max(0, r["results"][t]["correct"] + (1 if (b.get("delta") or 1) > 0 else -1))
            return None
        if a == "edit":
            try:
                r["limit"] = max(30, min(1800, int(b.get("limit") or r["limit"])))
            except (TypeError, ValueError):
                raise Bad("시간은 숫자로 입력해 주세요")
            ws = b.get("words")
            if game == "charades" and isinstance(ws, dict):
                for t in GT:
                    if t in ws:
                        r["words"][t] = [clean(w) for w in str(ws[t]).splitlines() if clean(w)][:200]
            if game == "catch" and ws is not None:
                lst = [clean(w) for w in str(ws).splitlines() if clean(w)]
                if not lst:
                    raise Bad("제시어를 1개 이상 입력해 주세요")
                r["words"] = lst[:400]
            return "설정을 저장했습니다"
        if a == "reset":
            r.update(results={t: None for t in GT}, live=None, finished=False, used=[])  # 판 초기화 = 그 게임 제시어도 처음부터
            if game == "catch":
                STROKES["epoch"] += 1
                STROKES["segs"] = []
            return "기록을 초기화했습니다"
    if game in ("vote", "mvp"):
        return vote_action(a, b, game)
    if game == "stay":
        return stay_action(a, b)
    if game == "menu":
        return menu_action(a, b)
    if game == "final":
        if a == "bonus":
            t = b.get("team")
            if t not in GT:
                raise Bad("팀 값이 올바르지 않습니다")
            g["bonus"][t] += 1 if (b.get("delta") or 1) > 0 else -1
            return None
        if a == "reveal":
            try:
                g["reveal"] = max(0, min(3, int(b.get("step"))))
            except (TypeError, ValueError):
                raise Bad("발표 단계가 올바르지 않습니다")
            return None
    if game == "all" and a == "reset":
        S["games"] = default_games()
        STROKES["epoch"] += 1
        STROKES["segs"] = []
        return "게임 기록을 모두 초기화했습니다"
    raise Bad("알 수 없는 동작입니다")


def stroke_action(b):
    a = b.get("action")
    if a == "add":
        segs = b.get("segs")
        if not isinstance(segs, list):
            raise Bad("형식이 올바르지 않습니다")
        ok = []
        for s in segs[:400]:
            if isinstance(s, list) and len(s) == 7:
                try:
                    x0, y0, x1, y1 = (max(0.0, min(1.0, float(v))) for v in s[:4])
                    ok.append([x0, y0, x1, y1, str(s[4])[:9], max(0.001, min(0.2, float(s[5]))), int(s[6])])
                except (TypeError, ValueError):
                    continue
        if len(STROKES["segs"]) + len(ok) > 60000:
            raise Bad("그림이 너무 많아요. 전체 지우기 후 다시 그려 주세요")
        STROKES["segs"].extend(ok)
    elif a == "clear":
        STROKES["epoch"] += 1
        STROKES["segs"] = []
    elif a == "undo":
        if STROKES["segs"]:
            sid = STROKES["segs"][-1][6]
            STROKES["segs"] = [s for s in STROKES["segs"] if s[6] != sid]
        STROKES["epoch"] += 1
    else:
        raise Bad("알 수 없는 동작입니다")
    cond.notify_all()
    return {"epoch": STROKES["epoch"], "n": len(STROKES["segs"])}


# ---------------- 상태 ----------------
def default_state(ver=0):
    return {"v": STATE_V, "ver": ver, "goal": GOAL, "total": max(2, GOAL - L_SIZE),
            "extra": str(secrets.randbelow(2) + 1), "roster": list(ROSTER), "members": [],
            "teamNames": {t: "" for t in TEAMS}, "games": default_games()}


def sanitize(s):
    """백업 복원·파일 로드 시 형식 검증. 문제가 있으면 ValueError."""
    if not isinstance(s, dict) or s.get("v") != STATE_V or not isinstance(s.get("members"), list):
        raise ValueError("형식이 올바르지 않습니다")
    roster, rk = [], set()
    for n in s.get("roster") or ROSTER:
        n = clean(n)
        if n and norm(n) not in rk:
            rk.add(norm(n)); roster.append(n)
    out, nicks, reals = [], set(), set()
    for m in s["members"]:
        if not isinstance(m, dict) or m.get("team") not in TEAMS or not clean(m.get("name")) or not clean(m.get("real")):
            raise ValueError("참가자 형식이 올바르지 않습니다")
        name, real = clean(m["name"]), clean(m["real"])
        if norm(name) in nicks or norm(real) in reals:
            continue
        nicks.add(norm(name)); reals.add(norm(real))
        if norm(real) not in rk:
            rk.add(norm(real)); roster.append(real)
        out.append({"id": str(m.get("id") or secrets.token_hex(5)), "name": name, "real": real, "team": m["team"],
                    "leader": bool(m.get("leader")), "ts": int(m.get("ts") or 0), "revealAt": 0,
                    "ltoken": m.get("ltoken") if isinstance(m.get("ltoken"), str) else secrets.token_urlsafe(12)})
    for t in TEAMS:  # 팀당 팀장 1명
        for m in [m for m in out if m["team"] == t and m["leader"]][1:]:
            m["leader"] = False
    goal = max(L_SIZE + 2, min(200, int(s.get("goal") or GOAL)))
    return {"v": STATE_V, "ver": int(s.get("ver") or 0), "goal": goal, "total": goal - L_SIZE,
            "extra": "2" if str(s.get("extra")) == "2" else "1", "roster": roster, "members": out,
            "teamNames": {t: clean_team_name((s.get("teamNames") or {}).get(t)) for t in TEAMS},
            "games": sanitize_games(s.get("games"))}


def load():
    try:
        with open(DATA, encoding="utf-8") as f:
            return sanitize(json.load(f))
    except FileNotFoundError:
        return default_state()
    except Exception as e:  # 예전 형식이거나 깨진 파일은 보존하고 새로 시작
        bad = DATA + f".old-{int(time.time())}"
        try:
            os.replace(DATA, bad)
        except OSError:
            pass
        print(f"[안내] 기존 data.json 을 쓸 수 없어 새로 시작합니다 ({e}). 원본 보관: {bad}")
        return default_state()


S = load()


def save():
    tmp = DATA + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(S, f, ensure_ascii=False, indent=1)
    os.replace(tmp, DATA)


def changed():
    """cond 를 잡은 상태에서 호출."""
    S["ver"] += 1
    save()
    cond.notify_all()


def team_of(t):
    return [m for m in S["members"] if m["team"] == t]


def by_nick(n):
    k = norm(n)
    return next((m for m in S["members"] if norm(m["name"]) == k), None)


def by_real(n):
    k = norm(n)
    return next((m for m in S["members"] if norm(m["real"]) == k), None)


def by_id(i):
    return next((m for m in S["members"] if m["id"] == i), None)


def in_roster(n):
    k = norm(n)
    return next((r for r in S["roster"] if norm(r) == k), None)


def leader_roster():
    return [r for r in S["roster"] if is_leader_name(r)]


def targets():
    big, small = (S["total"] + 1) // 2, S["total"] // 2
    return {"1": big, "2": small} if S["extra"] == "1" else {"1": small, "2": big}


def l_extra_count():
    """리더팀에 랜덤으로 들어온(고정 명단이 아닌) 인원."""
    return sum(1 for m in team_of("L") if not is_leader_name(m["real"]))


def remaining():
    T, r = targets(), {}
    for t in ("1", "2"):
        ms = team_of(t)
        left = max(0, T[t] - len(ms))
        r[t] = {"left": left, "leader": 1 if (left > 0 and not any(m["leader"] for m in ms)) else 0}
    fixed_left = sum(1 for n in leader_roster() if not by_real(n))
    extra_left = max(0, LEADER_EXTRA - l_extra_count())
    left = fixed_left + extra_left
    r["L"] = {"left": left, "fixed": fixed_left, "extra": extra_left,
              "leader": 1 if (left and not any(m["leader"] for m in team_of("L"))) else 0}
    return r


def l_leader_draw():
    """리더팀 팀장 카드: 남은 리더팀 자리(고정 미추첨 + 랜덤 자리, 본인 포함) 중 1장 → 순서와 무관하게 균등."""
    if any(m["leader"] for m in team_of("L")):
        return False
    left = remaining()["L"]["left"]
    return True if left <= 1 else secrets.randbelow(left) == 0


def pick_card():
    """고정 리더팀이 아닌 사람: 남은 카드(1팀·2팀·리더팀 랜덤 자리)에서 균등하게 1장
    = 미리 섞은 카드 더미에서 위에서부터 뽑는 것과 같은 확률."""
    r, cards = remaining(), []
    for t in ("1", "2"):
        for i in range(r[t]["left"]):
            cards.append((t, i < r[t]["leader"]))
    for _ in range(r["L"]["extra"]):
        cards.append(("L", None))
    if cards:
        team, leader = cards[secrets.randbelow(len(cards))]
        return (team, l_leader_draw()) if team == "L" else (team, leader)
    c1, c2 = len(team_of("1")), len(team_of("2"))
    t = str(secrets.randbelow(2) + 1) if c1 == c2 else ("1" if c1 < c2 else "2")
    return t, not any(m["leader"] for m in team_of(t))


def pick_leader_card():
    """고정 리더팀: 무조건 리더팀, 팀장 여부만 추첨."""
    return l_leader_draw()


def is_pending(m, t=None):
    return m.get("revealAt", 0) > (t or now_ms())


def member_out(m, admin, t):
    d = {k: m[k] for k in ("id", "name", "real", "team", "leader", "ts")}
    d["fixed"] = is_leader_name(m["real"])
    d["pending"] = is_pending(m, t)
    if d["pending"] and not admin:
        d["leader"] = False
        if m["team"] != "L" or not d["fixed"]:  # 랜덤 리더팀 합류도 연출 끝날 때까지 숨김
            d["team"] = None
    return d


def view(admin=False):
    t = now_ms()
    drawn = {norm(m["real"]) for m in S["members"]}
    return {
        "ver": S["ver"],
        "total": S["total"],
        "goal": S["goal"],
        "targets": targets(),
        "leaderTotal": len(leader_roster()) + LEADER_EXTRA,
        "admin": admin,
        "adminName": ADMIN,
        "remaining": remaining() if admin else None,
        "roster": [{"name": r, "drawn": norm(r) in drawn, "host": norm(r) == norm(ADMIN)} for r in S["roster"]],
        "teamNames": S.get("teamNames") or {},
        "members": [member_out(m, admin, t) for m in S["members"]],
    }


def mates(m):
    t = now_ms()
    return [{"name": x["name"], "real": x["real"], "leader": x["leader"]}
            for x in S["members"] if x["team"] == m["team"] and x["id"] != m["id"] and not is_pending(x, t)]


def full(m):
    d = {k: m[k] for k in ("id", "name", "real", "team", "leader", "ts")}
    d["fixed"] = is_leader_name(m["real"])
    return d


def has_pending():
    t = now_ms()
    return any(is_pending(m, t) for m in S["members"])


# ---------------- 관리자 동작 ----------------
class Bad(Exception):
    pass


def need(m):
    if not m:
        raise Bad("참가자를 찾을 수 없습니다")
    return m


def set_leader(m, on):
    if on:
        for x in team_of(m["team"]):
            x["leader"] = False
    m["leader"] = bool(on)


def move(m, t):
    if t not in TEAMS:
        raise Bad("팀 값이 올바르지 않습니다")
    if m["team"] == t:
        return
    m["team"] = t
    m["revealAt"] = 0
    if m["leader"] and any(x is not m and x["leader"] for x in team_of(t)):
        m["leader"] = False
    if not m["ts"]:
        m["ts"] = now_ms()


def admin_action(b):
    global S
    a = b.get("action")
    m = by_id(b.get("id")) if b.get("id") else None
    if a == "move":
        need(m); move(m, b.get("team")); return f"{m['name']}님을 {tl(m['team'])}(으)로 옮겼습니다"
    if a == "leader":
        need(m); set_leader(m, b.get("on"))
        return f"{m['name']}님이 {tl(m['team'])} 팀장이 됐습니다" if b.get("on") else f"{m['name']}님 팀장 해제"
    if a == "rename":
        need(m); v = clean(b.get("name"))
        if not v:
            raise Bad("닉네임을 입력해 주세요")
        d = by_nick(v)
        if d and d is not m:
            raise Bad("이미 사용 중인 닉네임입니다")
        m["name"] = v; return "닉네임을 바꿨습니다"
    if a == "del":
        need(m); S["members"] = [x for x in S["members"] if x is not m]
        return f"{m['name']}({m['real']})님의 추첨을 취소했습니다. 다시 뽑을 수 있습니다"
    if a == "add":
        v, real, t = clean(b.get("name")), clean(b.get("real")), b.get("team")
        if not real:
            raise Bad("본명을 입력해 주세요")
        v = v or real
        if t not in TEAMS:
            raise Bad("팀 값이 올바르지 않습니다")
        if by_real(real):
            raise Bad("이미 추첨한 사람입니다")
        if by_nick(v):
            raise Bad("이미 사용 중인 닉네임입니다")
        if not in_roster(real):
            S["roster"].append(real)
        S["members"].append({"id": secrets.token_hex(5), "name": v, "real": in_roster(real), "team": t,
                             "leader": False, "ts": now_ms(), "revealAt": 0})
        return f"{v}({real})님을 {tl(t)}에 추가했습니다"
    if a == "rosterAdd":
        v = clean(b.get("name"))
        if not v:
            raise Bad("이름을 입력해 주세요")
        if in_roster(v):
            raise Bad("이미 명단에 있습니다")
        S["roster"].append(v); return f"명단에 {v}님을 추가했습니다"
    if a == "rosterDel":
        r = in_roster(b.get("name"))
        if not r:
            raise Bad("명단에 없는 이름입니다")
        if by_real(r):
            raise Bad("이미 추첨한 사람은 명단에서 뺄 수 없습니다. 먼저 추첨을 취소하세요")
        S["roster"] = [x for x in S["roster"] if x != r]; return f"명단에서 {r}님을 뺐습니다"
    if a == "total":  # 총원(사회자 제외) 변경 → 1·2팀 인원 재계산
        try:
            n = int(b.get("n"))
        except (TypeError, ValueError):
            raise Bad("숫자를 입력해 주세요")
        lo = L_SIZE + 2
        if not lo <= n <= 200:
            raise Bad(f"{lo}~200 사이로 입력해 주세요")
        S["goal"], S["total"] = n, n - L_SIZE
        return f"총원을 {n}명으로 바꿨습니다 (리더팀 {L_SIZE} + 1·2팀 {n - L_SIZE}명)"
    if a == "reshuffle":
        pool = [x for x in S["members"] if x["team"] != "L"]
        if len(pool) < 2:
            raise Bad("섞을 인원이 부족합니다")
        for i in range(len(pool) - 1, 0, -1):
            j = secrets.randbelow(i + 1)
            pool[i], pool[j] = pool[j], pool[i]
        first = str(secrets.randbelow(2) + 1)
        second = "2" if first == "1" else "1"
        half = (len(pool) + 1) // 2
        for i, x in enumerate(pool):
            x["team"] = first if i < half else second
            x["leader"] = i in (0, half)
            x["revealAt"] = 0
        return "1·2팀을 다시 섞었습니다"
    if a == "reset":
        keep = S.get("games")  # 팀뽑기 초기화는 게임 기록을 건드리지 않음
        S = default_state(S["ver"]); S["games"] = keep or default_games(); return "초기화했습니다"
    if a == "restore":
        try:
            ns = sanitize(b.get("state"))
        except ValueError as e:
            raise Bad(f"올바른 백업 파일이 아닙니다 ({e})")
        ns["ver"] = S["ver"]
        if "games" not in (b.get("state") or {}):
            ns["games"] = S.get("games") or default_games()
        S = ns; return "백업을 불러왔습니다"
    raise Bad("알 수 없는 동작입니다")


GAME_LOGIN_HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>게임 진행 화면 · 비밀번호</title>
<style>
*{box-sizing:border-box}html,body{margin:0;height:100%}
body{background:radial-gradient(ellipse at center,#26104e,#07060f 70%);color:#f4f2ff;font-family:"Pretendard","Apple SD Gothic Neo","Malgun Gothic","Noto Sans KR",system-ui,sans-serif;display:flex;align-items:center;justify-content:center;padding:16px}
form{width:min(420px,100%);padding:28px;border-radius:24px;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.14);box-shadow:0 30px 80px rgba(0,0,0,.6);text-align:center}
h1{margin:0 0 6px;font-size:28px}p{margin:0 0 18px;color:#aaa3cc;line-height:1.6}
input{width:100%;font:inherit;font-size:32px;letter-spacing:.4em;text-align:center;padding:14px;border-radius:16px;border:1px solid rgba(255,255,255,.2);background:rgba(0,0,0,.45);color:#fff;outline:none}
input:focus{border-color:#22d3ee;box-shadow:0 0 0 4px rgba(34,211,238,.2)}
button{margin-top:14px;width:100%;font:inherit;font-size:20px;font-weight:900;padding:14px;border:0;border-radius:16px;cursor:pointer;color:#160b28;background:linear-gradient(90deg,#22d3ee,#f472b6,#fbbf24)}
.err{min-height:22px;margin-top:10px;color:#fca5a5;font-weight:700}
.shake{animation:sh .4s}@keyframes sh{25%{transform:translateX(-8px)}75%{transform:translateX(8px)}}
a{color:#aaa3cc;font-size:14px;display:inline-block;margin-top:14px}
</style></head><body>
<form id="f" autocomplete="off"><h1>__TITLE__</h1><p>진행자만 들어갈 수 있어요.<br>비밀번호를 입력해 주세요.</p>
<input id="pw" type="password" inputmode="numeric" maxlength="20" autofocus placeholder="••••">
<button type="submit">들어가기</button><div class="err" id="err"></div><a href="/">← 팀뽑기로 돌아가기</a></form>
<script>
const f=document.getElementById('f'),pw=document.getElementById('pw'),err=document.getElementById('err');
f.addEventListener('submit',async e=>{e.preventDefault();err.textContent='';
  try{const r=await fetch('/api/gamelogin',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pw:pw.value,page:'__PAGE__'})});
    if(r.ok){const j=await r.json();location.replace(location.pathname+'?t='+encodeURIComponent(j.ticket));return}
    const j=await r.json().catch(()=>({}));err.textContent=j.error||'비밀번호가 틀렸어요';}
  catch(x){err.textContent='서버에 연결할 수 없어요'}
  f.classList.remove('shake');void f.offsetWidth;f.classList.add('shake');pw.value='';pw.focus();});
</script></body></html>"""

# ---------------- 네트워크 정보 ----------------
PUBLIC_URL_FILE = os.path.join(BASE, "public_url.txt")  # tunnel.command 가 인터넷 주소를 적어 둠


def public_url():
    try:
        with open(PUBLIC_URL_FILE, encoding="utf-8") as f:
            v = f.read().strip()
        return v if re.fullmatch(r"https://[a-z0-9-]+\.trycloudflare\.com/", v) else ""
    except OSError:
        return ""


_ip_cache = {"t": 0, "v": []}


def lan_ips():
    if time.time() - _ip_cache["t"] < 30:  # 30초 캐시
        return _ip_cache["v"]
    _ip_cache["v"] = _lan_ips()
    _ip_cache["t"] = time.time()
    return _ip_cache["v"]


def _lan_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # 실제 패킷은 나가지 않음
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=3).stdout
        ips.update(re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", out))
    except Exception:
        pass

    def private(ip):
        a, b = (int(x) for x in ip.split(".")[:2])
        return a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168)

    ips = [ip for ip in ips if not ip.startswith("127.") and not ip.startswith("169.254.")]
    priv = sorted(ip for ip in ips if private(ip))
    return priv or sorted(ips)  # 사내망 주소가 있으면 VPN(100.64.x 등) 주소는 숨김


# ---------------- HTTP ----------------
class Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # 기본 구현은 getfqdn() 역조회를 해서 사내 DNS 환경에서 기동이 수 초 걸린다 → 생략
        socketserver.TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = self.server_address[1]


class Handler(BaseHTTPRequestHandler):
    server_version = "QETeamDraw/1.0"

    def log_message(self, fmt, *args):  # 접속 로그는 생략(결과 스포일 방지)
        pass

    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj, extra=None):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", extra)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 2_000_000:
            raise Bad("요청이 너무 큽니다")
        raw = self.rfile.read(n) if n else b"{}"
        try:
            b = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            raise Bad("요청 형식이 올바르지 않습니다")
        if not isinstance(b, dict):
            raise Bad("요청 형식이 올바르지 않습니다")
        return b

    def _game_ok(self, q=None):
        """이번 화면 열람에서 비밀번호를 통과했는지 (헤더 X-Game-Token 또는 ?gt=)."""
        if not GAME_PASSWORD:  # 비밀번호 해제 상태
            return True
        tok = self.headers.get("X-Game-Token") or ((q or {}).get("gt") or [""])[0]
        t0 = GAME_SESSIONS.get(tok) if tok else None
        return bool(t0 and now_ms() - t0 < SESSION_TTL_MS)

    def _game_page(self, path, page, ticket):
        """입장권이 유효하면 1회 소모하고 화면 전용 세션을 심어서 보냄, 아니면 비밀번호 화면."""
        now = now_ms()
        with cond:  # 여러 요청이 동시에 와도 입장권은 딱 한 번만 쓰이도록
            for k in [k for k, (t0, _) in GAME_TICKETS.items() if now - t0 > TICKET_TTL_MS]:
                GAME_TICKETS.pop(k, None)
            for k in [k for k, t0 in GAME_SESSIONS.items() if now - t0 > SESSION_TTL_MS]:
                GAME_SESSIONS.pop(k, None)
            tk = GAME_TICKETS.pop(ticket, None) if ticket else None
            sid = secrets.token_urlsafe(18) if (tk and tk[1] == page) or not GAME_PASSWORD else None
            if sid:
                GAME_SESSIONS[sid] = now
        if not sid:
            return self._game_login_page("🎮 게임 진행 화면" if page == "games" else "📱 제시어 보기", page)
        try:
            with open(path, "rb") as f:
                body = f.read().decode("utf-8")
        except FileNotFoundError:
            return self._send(500, f"{os.path.basename(path)} 이 없습니다".encode(), "text/plain; charset=utf-8")
        inject = ('<script>window.__GT=' + json.dumps(sid) +
                  ';history.replaceState(null,"",location.pathname);</script>')  # 주소에서 입장권 제거 → 새로고침 시 다시 비밀번호
        body = body.replace("</head>", inject + "</head>", 1)
        return self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")

    def _game_login_page(self, title, page="games"):
        body = GAME_LOGIN_HTML.replace("__TITLE__", title).replace("__PAGE__", page).encode("utf-8")
        return self._send(200, body, "text/html; charset=utf-8")

    @staticmethod
    def _is_admin(tok):
        return isinstance(tok, str) and secrets.compare_digest(tok, ADMIN_TOKEN)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        admin = self._is_admin((q.get("token") or [""])[0])
        if u.path in ("/", "/index.html"):
            try:
                with open(INDEX, "rb") as f:
                    body = f.read()
            except FileNotFoundError:
                return self._send(500, "index.html 이 없습니다".encode(), "text/plain; charset=utf-8")
            return self._send(200, body, "text/html; charset=utf-8")
        if u.path in ("/timetable", "/timetable.html"):
            try:
                with open(TIMETABLE, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                return self._send(500, "timetable.html 이 없습니다".encode(), "text/plain; charset=utf-8")
        if u.path in ("/vote", "/vote.html"):
            try:
                with open(VOTE, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                return self._send(500, "vote.html 이 없습니다".encode(), "text/plain; charset=utf-8")
        if u.path in ("/mvp", "/mvp.html"):
            try:
                with open(MVP, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                return self._send(500, "mvp.html 이 없습니다".encode(), "text/plain; charset=utf-8")
        if u.path in ("/stay", "/stay.html"):
            try:
                with open(STAY, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                return self._send(500, "stay.html 이 없습니다".encode(), "text/plain; charset=utf-8")
        if u.path in ("/menu", "/menu.html"):
            try:
                with open(MENU, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                return self._send(500, "menu.html 이 없습니다".encode(), "text/plain; charset=utf-8")
        if u.path == "/api/menu":
            with cond:
                return self._json(200, menu_view())
        if u.path == "/api/stay":
            with cond:
                return self._json(200, stay_view())
        if u.path == "/api/mvp":  # 공개: 참여왕 투표 현황 (누가 누구를 찍었는지는 저장 안 함)
            with cond:
                return self._json(200, dict(vote_view("mvp"), teams={t: ((S.get("teamNames") or {}).get(t) or tl(t)) for t in GT}))
        if u.path == "/api/vote":  # 공개: 투표 현황(득표수는 발표 후에만)
            with cond:
                return self._json(200, dict(vote_view(), teams={t: ((S.get("teamNames") or {}).get(t) or tl(t)) for t in GT}))
        ticket = (q.get("t") or [""])[0]
        if u.path in ("/games", "/games.html"):
            return self._game_page(GAMES, "games", ticket)
        if u.path in ("/word", "/word.html"):
            return self._game_page(WORD, "word", ticket)
        if u.path in ("/api/gstate", "/api/word", "/api/gevents", "/api/strokes") and not self._game_ok(q):
            return self._json(401, {"error": "게임 비밀번호가 필요합니다"})
        if u.path == "/api/gstate":
            with cond:
                games_tick()
                return self._json(200, gview())
        if u.path == "/api/word":
            with cond:
                games_tick()
                return self._json(200, word_view())
        if u.path == "/api/gevents":
            return self._gevents(admin)
        if u.path == "/api/strokes":
            try:
                ep = int((q.get("epoch") or ["-1"])[0]); frm = int((q.get("from") or ["0"])[0])
            except ValueError:
                ep, frm = -1, 0
            with cond:
                if ep != STROKES["epoch"]:
                    frm = 0
                return self._json(200, {"epoch": STROKES["epoch"], "from": frm, "segs": STROKES["segs"][frm:frm + 4000],
                                        "n": len(STROKES["segs"])})
        if u.path == "/api/state":
            with cond:
                return self._json(200, view(admin))
        if u.path == "/api/info":
            return self._json(200, {"urls": [f"http://{ip}:{PORT}/" for ip in lan_ips()], "port": PORT,
                                    "public": public_url()})
        if u.path == "/api/events":
            return self._events(admin)
        if u.path == "/api/backup":
            if not admin:
                return self._json(403, {"error": "관리자만 사용할 수 있습니다"})
            with cond:
                body = json.dumps(S, ensure_ascii=False, indent=1).encode("utf-8")
            name = time.strftime("qe-teamdraw-%Y%m%d-%H%M.json")
            return self._send(200, body, "application/json; charset=utf-8",
                              {"Content-Disposition": f'attachment; filename="{name}"'})
        if u.path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        return self._json(404, {"error": "없는 주소입니다"})

    def _events(self, admin):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        last = None
        try:
            while True:
                with cond:
                    payload = json.dumps(view(admin), ensure_ascii=False)
                    if payload == last:
                        cond.wait(1.0 if has_pending() else 15.0)
                        payload = json.dumps(view(admin), ensure_ascii=False)
                if payload != last:
                    self.wfile.write(b"retry: 2000\ndata: " + payload.encode("utf-8") + b"\n\n")
                    last = payload
                else:
                    self.wfile.write(b"event: ping\ndata: 1\n\n")  # 화면이 '스트림 살아 있음'을 알도록 이벤트로 보냄
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return

    def _gevents(self, admin):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        last_ver = None
        try:
            while True:
                with cond:
                    games_tick()
                    key = (S["ver"], STROKES["epoch"], len(STROKES["segs"]))
                    if key == last_ver and not timers_running():
                        cond.wait(15.0)
                    elif key == last_ver:
                        cond.wait(0.5)
                    games_tick()
                    key = (S["ver"], STROKES["epoch"], len(STROKES["segs"]))
                    payload = json.dumps(gview(), ensure_ascii=False)
                self.wfile.write(b"retry: 2000\ndata: " + payload.encode("utf-8") + b"\n\n")
                self.wfile.flush()
                last_ver = key
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return

    def do_POST(self):
        u = urlparse(self.path)
        try:
            b = self._body()
            if u.path == "/api/draw":
                return self._json(200, self._draw(b))
            if u.path == "/api/teamname":
                return self._json(200, self._teamname(b))
            if u.path == "/api/reveal":
                with cond:
                    m = by_id(b.get("id"))
                    if m and is_pending(m):
                        m["revealAt"] = 0
                        changed()
                return self._json(200, {"ok": True})
            if u.path == "/api/gamelogin":
                if secrets.compare_digest(str(b.get("pw") or ""), GAME_PASSWORD):
                    page = "word" if b.get("page") == "word" else "games"
                    ticket = secrets.token_urlsafe(18)
                    with cond:
                        GAME_TICKETS[ticket] = (now_ms(), page)
                    print(f"[{time.strftime('%H:%M:%S')}] {'제시어' if page == 'word' else '게임'} 화면 입장 ({self.client_address[0]})")
                    return self._json(200, {"ok": True, "ticket": ticket, "page": page})
                time.sleep(0.6)  # 연속 대입 늦추기
                return self._json(401, {"error": "비밀번호가 틀렸어요"})
            if u.path == "/api/menu":  # 공개: 메뉴 한 개 추가 (이름 없이)
                with cond:
                    msg = menu_action("add", b)
                    changed()
                    return self._json(200, {"ok": True, "msg": msg, "m": menu_view()})
            if u.path == "/api/stay":  # 공개: 남/집 한 표 (다시 누르면 바꿈)
                with cond:
                    msg = stay_action("cast", b)
                    changed()
                    return self._json(200, {"ok": True, "msg": msg, "s": stay_view()})
            if u.path == "/api/mvp":  # 공개: 참여왕 한 표만 (마감·발표는 게임 화면에서)
                with cond:
                    msg = vote_action("cast", b, "mvp")
                    changed()
                    return self._json(200, {"ok": True, "msg": msg})
            if u.path == "/api/vote":  # 공개: 투표 한 표만 (마감·발표·취소는 비밀번호 화면에서)
                with cond:
                    msg = vote_action("cast", b)
                    changed()
                    return self._json(200, {"ok": True, "msg": msg})
            if u.path in ("/api/game", "/api/stroke", "/api/word") and not self._game_ok(parse_qs(u.query)):
                return self._json(401, {"error": "게임 비밀번호가 필요합니다"})
            if u.path == "/api/game":  # 비밀번호(쿠키) 통과한 진행자 화면만
                with cond:
                    games_tick()
                    msg = game_action(b)
                    changed()
                    return self._json(200, {"ok": True, "msg": msg, "g": gview()})
            if u.path == "/api/word":  # 그리는 사람 휴대폰: 캐치마인드 정답·패스만
                if b.get("action") not in ("correct", "pass") or not isinstance(b.get("wseq"), int):
                    raise Bad("정답 또는 패스만 누를 수 있어요")
                with cond:
                    games_tick()
                    msg = game_action({"game": "catch", "action": b["action"], "wseq": b["wseq"]})
                    changed()
                    return self._json(200, {"ok": True, "msg": msg, "w": word_view()})
            if u.path == "/api/stroke":
                with cond:
                    return self._json(200, stroke_action(b))
            if u.path == "/api/admin":
                if not self._is_admin(b.get("token")):
                    return self._json(403, {"error": "관리자 인증이 필요합니다"})
                with cond:
                    msg = admin_action(b)
                    changed()
                    return self._json(200, {"ok": True, "msg": msg, "state": view(True)})
            return self._json(404, {"error": "없는 주소입니다"})
        except Bad as e:
            return self._json(400, {"error": str(e)})

    def _teamname(self, b):
        t = b.get("team")
        if t not in TEAMS:
            raise Bad("팀 값이 올바르지 않습니다")
        name = clean_team_name(b.get("name"))
        with cond:
            ok = self._is_admin(b.get("token"))
            if not ok:
                lt = b.get("ltoken")
                ld = next((m for m in team_of(t) if m["leader"]), None)
                ok = bool(ld and isinstance(lt, str) and lt and secrets.compare_digest(lt, ld.get("ltoken", "")))
            if not ok:
                return_err = "팀명은 그 팀의 팀장(또는 관리자)만 정할 수 있어요"
                raise Bad(return_err)
            S.setdefault("teamNames", {})[t] = name
            changed()
            print(f"[{time.strftime('%H:%M:%S')}] 팀명: {tl(t)} → {name or '(기본)'}")
            return {"ok": True, "teamNames": S["teamNames"]}

    def _draw(self, b):
        real_in = clean(b.get("real"))
        real = in_roster(real_in) if real_in else None
        if not real:
            raise Bad("아래 명단에서 본명을 선택해 주세요")
        self_draw = bool(b.get("selfDraw"))
        if norm(real) == norm(ADMIN) and not self_draw:
            print(f"[{time.strftime('%H:%M:%S')}] 관리자 입장 ({self.client_address[0]})")
            return {"status": "admin", "token": ADMIN_TOKEN}
        if self_draw and not self._is_admin(b.get("token")):
            raise Bad("관리자 인증이 필요합니다")
        nick = clean(b.get("name"))
        with cond:
            ex = by_real(real)
            if ex and is_pending(ex):
                return {"status": "pending"}
            if ex:
                return {"status": "already", "member": full(ex), "mates": mates(ex)}
            if not nick:
                raise Bad("재치있는 닉네임을 입력해 주세요")
            if by_nick(nick):
                raise Bad("이미 누가 쓰고 있는 닉네임이에요. 다른 닉네임을 써 주세요")
            t = now_ms()
            if is_leader_name(real):
                team, leader = "L", pick_leader_card()
            else:
                team, leader = pick_card()
            m = {"id": secrets.token_hex(5), "name": nick, "real": real, "team": team, "leader": leader,
                 "ts": t, "revealAt": t + REVEAL_SEC * 1000, "ltoken": secrets.token_urlsafe(12)}
            S["members"].append(m)
            changed()
            print(f"[{time.strftime('%H:%M:%S')}] 추첨: {nick} ({real}) — 누적 {len(S['members'])}명, 결과는 화면에서 공개")
            resp = {"status": "new", "member": full(m), "mates": mates(m)}
            if leader:  # 팀장으로 뽑힌 기기에만 팀명 결정 권한
                resp["ltoken"] = m["ltoken"]
            return resp


def main():
    global PORT, REVEAL_SEC
    ap = argparse.ArgumentParser(description="10월23일 QE합동 워크샾 팀뽑기 서버")
    ap.add_argument("--host", default="0.0.0.0", help="기본 0.0.0.0 (모든 네트워크에서 접속 허용)")
    ap.add_argument("--port", type=int, default=8023)
    ap.add_argument("--reveal", type=int, default=REVEAL_SEC, help="다른 화면에 결과를 숨기는 최대 초")
    args = ap.parse_args()
    PORT, REVEAL_SEC = args.port, max(0, args.reveal)
    try:
        srv = Server((args.host, PORT), Handler)
    except OSError as e:
        print(f"[오류] {PORT} 포트를 열 수 없습니다: {e}\n다른 포트로 실행하세요: python3 server.py --port 9023")
        sys.exit(1)
    with cond:
        save()
    line = "=" * 58
    print(line)
    print(" 10월23일 QE합동 워크샾 팀뽑기 서버가 켜졌습니다")
    print(line)
    print(f" 이 PC에서 접속      : http://localhost:{PORT}/")
    ips = lan_ips()
    for i, ip in enumerate(ips):
        print(f" {'다른 PC·휴대폰 접속' if i == 0 else '                  '} : http://{ip}:{PORT}/")
    if not ips:
        print(" (네트워크 IP 를 찾지 못했습니다. Wi-Fi/유선 연결을 확인하세요)")
    print(f" 결과 저장 파일      : {DATA}")
    print(" 종료               : 이 창에서 Ctrl + C")
    print(line, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        print("\n서버를 종료했습니다. 결과는 data.json 에 저장돼 있습니다.")


if __name__ == "__main__":
    main()
