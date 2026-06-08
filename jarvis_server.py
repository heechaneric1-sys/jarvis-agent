#!/usr/bin/env python3
"""
JARVIS Web Server — Flask + Socket.IO
- Background thread listens for wake word ("hey jarvis") or double-clap
- On trigger: fetches real-time data, calls Claude, streams briefing to browser
- Browser animates + speaks via Web Speech API
"""

import io
import json
import math
import struct
import subprocess
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Optional

from flask import Flask, send_file, request, jsonify
from flask_socketio import SocketIO, emit

# Notion / Obsidian 연동
try:
    from jarvis_notion_obsidian import handle_note_command, obsidian_save, obsidian_search
    _notion_ok = True
except Exception:
    _notion_ok = False

# ── Config ────────────────────────────────────────────────────────────────────
WATCH_SYMBOLS = ["AAPL", "NVDA", "TSLA", "BTC-USD", "^GSPC"]
WAKE_PHRASES  = ["hey jarvis", "헤이 자비스", "jarvis", "자비스"]

# Clap detection (sounddevice fallback)
SAMPLE_RATE       = 16000
CLAP_THRESHOLD    = 0.25   # RMS float32
CLAP_COOLDOWN     = 0.15
DOUBLE_CLAP_WINDOW = 0.8

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

_state      = {"status": "idle"}
_state_lock = threading.Lock()
_busy       = threading.Event()
_last_data  = {}
_chat_proc  = None   # 현재 실행 중인 Claude 대화 프로세스


# ── State helpers ─────────────────────────────────────────────────────────────

def set_state(status: str):
    with _state_lock:
        _state["status"] = status
    socketio.emit("state", {"status": status})
    print(f"[state] {status}")


# ── Data fetchers (requests + yfinance, daemon threads, 3s timeout) ───────────

import requests as _requests
import yfinance as _yf
import os as _os

_HEADERS    = {"User-Agent": "Mozilla/5.0"}
_TIMEOUT    = 3
_FETCH_WALL = 3

# ~/.env.jarvis 로드
_env_file = Path.home() / ".env.jarvis"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            _os.environ[_k.strip()] = _v.strip()  # setdefault 대신 항상 덮어씀

YOUTUBE_API_KEY   = _os.environ.get("YOUTUBE_API_KEY", "")
ANTHROPIC_API_KEY  = _os.environ.get("ANTHROPIC_API_KEY", "")
ELEVENLABS_API_KEY = _os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = _os.environ.get("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")
print(f"[init] ANTHROPIC_API_KEY: {'set' if ANTHROPIC_API_KEY else 'MISSING'}")
print(f"[init] YOUTUBE_API_KEY:   {'set' if YOUTUBE_API_KEY else 'MISSING'}")

def _fetch_rss_titles(url, limit=5):
    r = _requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
    r.raise_for_status()
    tree = ET.fromstring(r.content)
    return [(item.findtext("title") or "").strip()
            for item in tree.findall(".//item")[:limit]
            if (item.findtext("title") or "").strip()]

def fetch_youtube_trending() -> Optional[list]:
    """AI 제작 바이럴 영상 — YouTube Search API."""
    if not YOUTUBE_API_KEY:
        return None
    try:
        from datetime import timedelta
        since = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        r = _requests.get(
            "https://www.googleapis.com/youtube/v3/search",
            params={"part":"snippet","q":"AI generated video viral 2026",
                    "type":"video","order":"viewCount","publishedAfter":since,
                    "maxResults":5,"key":YOUTUBE_API_KEY},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        ids = [i["id"]["videoId"] for i in r.json().get("items",[]) if i.get("id",{}).get("videoId")]
        if not ids:
            return None
        r2 = _requests.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={"part":"snippet,statistics","id":",".join(ids),"key":YOUTUBE_API_KEY},
            timeout=_TIMEOUT,
        )
        r2.raise_for_status()
        return [{"title": v["snippet"]["title"],
                 "views": int(v["statistics"].get("viewCount",0)),
                 "channel": v["snippet"]["channelTitle"]}
                for v in r2.json().get("items",[])] or None
    except Exception:
        return None


def fetch_trends() -> Optional[list]:
    try:
        r = _requests.get("https://trends.google.com/trending/rss?geo=US",
                          headers=_HEADERS, timeout=_TIMEOUT)
        r.raise_for_status()
        tree = ET.fromstring(r.content)
        ns   = {"ht": "https://trends.google.com/trending/rss"}
        out  = []
        for item in tree.findall(".//item")[:5]:
            title   = (item.findtext("title") or "").strip()
            traffic = (item.findtext("ht:approx_traffic", None, ns) or "").strip()
            if title:
                out.append({"keyword": title, "traffic": traffic})
        return out or None
    except Exception:
        return None

def fetch_news() -> Optional[list]:
    """경제 뉴스 3개 + 일반 뉴스 3개."""
    econ_feeds = [
        ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
        ("Yahoo Finance","https://finance.yahoo.com/rss/topstories"),
    ]
    general_feeds = [
        ("NPR", "https://feeds.npr.org/1001/rss.xml"),
        ("CNN", "http://rss.cnn.com/rss/edition.rss"),
        ("BBC", "http://feeds.bbci.co.uk/news/rss.xml"),
    ]
    econ, general = [], []
    for label, url in econ_feeds:
        if len(econ) >= 3: break
        try:
            for t in _fetch_rss_titles(url, 5):
                if len(econ) >= 3: break
                econ.append(f"[경제/{label}] {t}")
        except Exception:
            continue
    for label, url in general_feeds:
        if len(general) >= 3: break
        try:
            for t in _fetch_rss_titles(url, 5):
                if len(general) >= 3: break
                general.append(f"[{label}] {t}")
        except Exception:
            continue
    items = econ + general
    return items or None

def fetch_stocks() -> Optional[list]:
    SYMBOLS = {
        "^GSPC": "S&P 500", "^IXIC": "NASDAQ", "^DJI": "DOW",
        "^KS11": "KOSPI",   "^KQ11": "KOSDAQ",  "BTC-USD": "Bitcoin",
    }
    rows: dict = {}
    def _one(sym, name):
        try:
            info  = _yf.Ticker(sym).fast_info
            price = info.last_price
            prev  = info.previous_close or price
            chg   = ((price - prev) / prev * 100) if prev else 0
            rows[sym] = {"symbol": name, "price": round(price, 2), "change": round(chg, 2)}
        except Exception:
            pass
    threads = [threading.Thread(target=_one, args=(s, n), daemon=True)
               for s, n in SYMBOLS.items()]
    for t in threads: t.start()
    for t in threads: t.join(timeout=4)
    return [rows[s] for s in SYMBOLS if s in rows] or None

def fetch_quote() -> Optional[dict]:
    try:
        r = _requests.get("https://zenquotes.io/api/random",
                          headers=_HEADERS, timeout=_TIMEOUT)
        r.raise_for_status()
        d = r.json()[0]
        return {"text": d["q"], "author": d["a"]}
    except Exception:
        return None

def collect_all() -> dict:
    tasks = {"youtube": fetch_youtube_trending, "trends": fetch_trends, "news": fetch_news,
             "stocks": fetch_stocks, "quote": fetch_quote}
    results = {k: None for k in tasks}

    def _run(k, fn):
        try: results[k] = fn()
        except Exception: pass

    threads = {k: threading.Thread(target=_run, args=(k, fn), daemon=True)
               for k, fn in tasks.items()}
    for t in threads.values(): t.start()
    deadline = time.monotonic() + _FETCH_WALL
    for k, t in threads.items():
        t.join(timeout=max(deadline - time.monotonic(), 0))
        socketio.emit("log", f"{'✓' if results[k] else '✗'} {k}")
    return results


def build_prompt(data: dict) -> str:
    today = datetime.now().strftime("%Y년 %m월 %d일 %A")
    parts = [f"오늘은 {today}입니다."]

    if data.get("trends"):
        lines = "\n".join(f"- {t['keyword']} ({t['traffic']} searches)"
                          for t in data["trends"])
        parts.append(f"[트렌딩]\n{lines}")

    if data.get("news"):
        lines = "\n".join(f"- {n}" for n in data["news"])
        parts.append(f"[뉴스]\n{lines}")

    if data.get("stocks"):
        lines = "\n".join(
            f"- {s['symbol']}: {s['price']:,.2f} "
            f"({'▲' if s['change']>=0 else '▼'}{abs(s['change']):.2f}%)"
            for s in data["stocks"])
        parts.append(f"[주식]\n{lines}")

    if data.get("quote"):
        q = data["quote"]
        parts.append(f"[명언]\n\"{q['text']}\" — {q['author']}")

    context = "\n\n".join(parts)
    return f"""{context}

위 실시간 데이터로 자비스처럼 한국어 브리핑을 해줘.
아래 5개 섹션 순서대로:

1. 인사: 날짜 포함 짧은 한 문장

2. 트렌드: 핫한 키워드 언급 + 왜 중요한지 인사이트 한 문장

3. 뉴스: 각 뉴스 한 줄 요약 + 전체를 관통하는 인사이트 한 문장

4. 주식: 수치와 등락률 + 지금 주목해야 할 것 한 문장

5. 명언 + 오늘 핵심 메시지 한 문장

규칙:
- 순수 텍스트만. 이모지, 마크다운, 특수문자(▲▼$) 없이
- 퍼센트는 "퍼센트", 달러는 "달러"로
- 20초 안에 읽힐 분량. 무조건 짧게.
- 차갑고 정확하게. 감탄사 없이
- 마지막은 "브리핑 종료"로 끝낼 것"""


# ── Briefing pipeline ─────────────────────────────────────────────────────────

def build_briefing_prompt(data: dict) -> str:
    """데이터를 Claude에게 넘겨 한국어 요약 + 인사이트 브리핑 생성."""
    import re as _re
    today = datetime.now().strftime("%Y년 %m월 %d일 %A")
    parts = [f"오늘은 {today}입니다."]

    if data.get("youtube"):
        yt_lines = "\n".join(f"- {v['title']} ({v['views']:,}회)" for v in data["youtube"][:5])
        parts.append(f"[유튜브 글로벌 트렌딩]\n{yt_lines}")

    if data.get("trends"):
        t_lines = "\n".join(f"- {t['keyword']} ({t['traffic']} searches)" for t in data["trends"][:5])
        parts.append(f"[Google 트렌드 US]\n{t_lines}")

    if data.get("news"):
        clean = [_re.sub(r"^\[.*?\]\s*", "", n) for n in data["news"][:5]]
        parts.append("[주요 뉴스]\n" + "\n".join(f"- {n}" for n in clean))

    if data.get("stocks"):
        s_lines = "\n".join(
            f"- {s['symbol']}: {s['price']:,.2f} ({'+'if s['change']>=0 else ''}{s['change']:.2f}%)"
            for s in data["stocks"])
        parts.append(f"[주식/코인]\n{s_lines}")

    if data.get("quote"):
        q = data["quote"]
        parts.append(f"[명언]\n\"{q['text']}\" — {q['author']}")

    context = "\n\n".join(parts)

    return f"""{context}

위 실시간 데이터로 자비스처럼 한국어 브리핑을 해줘. 각 섹션에 인사이트 필수.

형식 (순서대로):
1. 날짜 인사 (1문장)
2. 유튜브 트렌딩: 영상 제목 한국어로 설명 + "지금 전 세계가 이걸 보는 이유/트렌드 인사이트" 1문장
3. 트렌드: 키워드 언급 + 이 트렌드가 시사하는 것 1문장
4. 뉴스: 각 뉴스 한국어 한 줄 요약 + 전체 뉴스를 관통하는 인사이트 1문장
5. 주식: 수치 + "지금 시장에서 주목할 점" 1문장
6. 명언 + 오늘 핵심 메시지 1문장

규칙:
- 모든 영어 제목/내용은 반드시 한국어로 번역해서 읽어라
- 이모지, 마크다운, 특수문자(▲▼$%) 없이 순수 텍스트
- 퍼센트는 "퍼센트", 달러는 "달러"로
- 차갑고 정확하게. 감탄사 없이
- 마지막은 "브리핑 종료입니다"로"""


_afplay_proc = None   # 현재 재생 중인 afplay 프로세스

def stop_speaking():
    """재생 중인 afplay 즉시 중단."""
    global _afplay_proc
    if _afplay_proc and _afplay_proc.poll() is None:
        _afplay_proc.kill()
        _afplay_proc = None

def speak_server(text: str):
    """TTS: macOS Yuna."""
    global _afplay_proc
    if not text:
        return
    try:
        _afplay_proc = subprocess.Popen(["say", "-v", "Yuna", text[:500]])
        _afplay_proc.wait()
    except Exception as e:
        print(f"[TTS] 오류: {e}")


def run_briefing():
    if _busy.is_set():
        return
    _busy.set()
    try:
        set_state("processing")
        # 데이터 수집 중 공백 채우기 — 즉시 TTS
        msg = "잠시만요. 실시간 데이터를 수집하고 있습니다."
        socketio.emit("speak_now", msg)
        speak_server(msg)   # 다 읽을 때까지 기다린 후 브리핑 시작
        data = collect_all()
        global _last_data
        _last_data = data
        socketio.emit("data", data)

        set_state("speaking")
        prompt = build_briefing_prompt(data)

        CLAUDE_BIN = (
            Path.home()
            / "Library/Application Support/Claude/claude-code/2.1.156/claude.app/Contents/MacOS/claude"
        )
        import os as _env_os
        env = {k:v for k,v in _env_os.environ.items() if k != "ANTHROPIC_API_KEY"}
        proc = subprocess.Popen(
            [str(CLAUDE_BIN), "--model", "claude-haiku-4-5-20251001", "-p", prompt, "--allowedTools", "WebSearch"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        full = ""
        for chunk in iter(lambda: proc.stdout.read(32), ""):
            full += chunk
            socketio.emit("text_chunk", chunk)
        proc.wait()
        full = full.strip()
        socketio.emit("text_done", full)
        threading.Thread(target=speak_server, args=(full,), daemon=True).start()

    except Exception as e:
        socketio.emit("error", str(e))
    finally:
        set_state("idle")
        _busy.clear()


# ── Wake word detection (sounddevice + Google STT) ────────────────────────────

def _record_wav(seconds: int) -> bytes:
    import sounddevice as sd
    import wave

    audio = sd.rec(
        int(seconds * SAMPLE_RATE),
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocking=True,
    )
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio.tobytes())
    buf.seek(0)
    return buf.read()


def _recognize(wav_bytes: bytes) -> str:
    import speech_recognition as sr
    recognizer = sr.Recognizer()
    audio = sr.AudioData(wav_bytes, SAMPLE_RATE, 2)
    try:
        return recognizer.recognize_google(audio, language="ko-KR").lower()
    except Exception:
        return ""


def wake_word_loop():
    """Continuously listens for 'Hey Jarvis'. Runs in background thread."""
    try:
        import sounddevice  # noqa: F401
        import speech_recognition  # noqa: F401
    except ImportError:
        print("[wake] sounddevice or SpeechRecognition not available — skipping wake word")
        return

    print('[wake] Listening for "헤이 자비스"...')
    while True:
        try:
            wav = _record_wav(3)
            text = _recognize(wav)
            if text:
                print(f"[wake] heard: {text}")
            if any(p in text for p in WAKE_PHRASES):
                print("[wake] ✅ Wake word detected!")
                stop_speaking()   # 재생 중이면 끊기
                socketio.emit("wake", {"heard": text})
                threading.Thread(target=run_briefing, daemon=True).start()
                time.sleep(30)   # cooldown — don't re-trigger mid-briefing
        except Exception as e:
            print(f"[wake] error: {e}")
            time.sleep(1)


# ── Clap detection fallback (sounddevice RMS) ─────────────────────────────────

def clap_loop():
    """Double-clap detection via sounddevice RMS. Runs in background thread."""
    try:
        import sounddevice as sd
        import numpy as np
    except ImportError:
        print("[clap] sounddevice/numpy not available — skipping clap detection")
        return

    print("[clap] Listening for double clap...")
    clap_count = 0
    last_clap  = 0.0
    CHUNK      = int(SAMPLE_RATE * 0.05)   # 50ms chunks

    def callback(indata, frames, t, status):
        nonlocal clap_count, last_clap
        rms = float(np.sqrt(np.mean(indata ** 2)))
        now = time.time()
        if rms > CLAP_THRESHOLD and (now - last_clap) > CLAP_COOLDOWN:
            clap_count += 1
            last_clap = now
            print(f"[clap] 👏 {clap_count}/2  (rms={rms:.3f})")
            socketio.emit("clap", {"count": clap_count})
            if clap_count >= 2:
                clap_count = 0
                socketio.emit("wake", {"heard": "clap"})
                threading.Thread(target=run_briefing, daemon=True).start()
        if clap_count == 1 and (now - last_clap) > DOUBLE_CLAP_WINDOW:
            clap_count = 0

    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="float32",
        blocksize=CHUNK,
        callback=callback,
    ):
        while True:
            time.sleep(1)


# ── Socket.IO events ──────────────────────────────────────────────────────────

@socketio.on("connect")
def on_connect():
    with _state_lock:
        emit("state", {"status": _state["status"]})
    print("[ws] client connected")


@socketio.on("trigger")
def on_trigger(data=None):
    """Browser can also manually trigger the briefing."""
    threading.Thread(target=run_briefing, daemon=True).start()


@socketio.on("user_message")
def on_user_message(data):
    """Voice conversation: user speaks → Claude responds."""
    text = (data or {}).get("text", "").strip()
    if not text:
        return
    print(f"[chat] user: {text}")

    # 노션/옵시디언 명령어 감지
    NOTE_KEYWORDS = ["저장해줘","노트해줘","기록해줘","옵시디언","노션에","동기화","노트 찾아","검색해줘"]
    if _notion_ok and any(k in text for k in NOTE_KEYWORDS):
        threading.Thread(target=run_note_command, args=(text,), daemon=True).start()
        return

    threading.Thread(target=run_chat, args=(text,), daemon=True).start()


def run_note_command(text: str):
    set_state("speaking")
    socketio.emit("chat_user", text)
    try:
        result = handle_note_command(text)
        if result:
            socketio.emit("text_done", result)
            threading.Thread(target=speak_server, args=(result,), daemon=True).start()
        else:
            threading.Thread(target=run_chat, args=(text,), daemon=True).start()
    except Exception as e:
        socketio.emit("error", str(e))
    finally:
        set_state("idle")


def web_search(query: str) -> str:
    """DuckDuckGo Instant Answer + 상위 결과 텍스트."""
    try:
        r = _requests.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"},
            timeout=6,
        )
        d = r.json()
        parts = []
        if d.get("Abstract"):
            parts.append(d["Abstract"])
        for t in d.get("RelatedTopics", [])[:4]:
            if isinstance(t, dict) and t.get("Text"):
                parts.append(t["Text"][:200])
        return "\n".join(parts) if parts else ""
    except Exception:
        return ""


def youtube_search(query: str) -> str:
    """YouTube 인기 영상 검색."""
    if not YOUTUBE_API_KEY:
        return ""
    try:
        r = _requests.get(
            "https://www.googleapis.com/youtube/v3/search",
            params={"part":"snippet","q":query,"type":"video","order":"viewCount",
                    "maxResults":3,"key":YOUTUBE_API_KEY,"relevanceLanguage":"ko"},
            timeout=6,
        )
        items = r.json().get("items", [])
        lines = []
        for item in items:
            s = item["snippet"]
            vid = item["id"]["videoId"]
            lines.append(f"{s['title']} ({s['channelTitle']}) https://youtu.be/{vid}")
        return "\n".join(lines)
    except Exception:
        return ""


def run_chat(user_text: str):
    global _chat_proc
    stop_speaking()   # 말하는 중이면 즉시 끊기
    if _chat_proc and _chat_proc.poll() is None:
        _chat_proc.kill()
        _chat_proc = None

    set_state("speaking")
    socketio.emit("chat_user", user_text)

    # ── 실시간 웹 검색 (3초 제한, 없으면 그냥 진행) ──
    import concurrent.futures as _cf
    web_result, yt_result = "", ""
    try:
        with _cf.ThreadPoolExecutor(max_workers=2) as ex:
            f_web = ex.submit(web_search, user_text)
            f_yt  = ex.submit(youtube_search, user_text)
            web_result = f_web.result(timeout=3)
            yt_result  = f_yt.result(timeout=3)
    except Exception:
        pass   # 검색 느리면 스킵하고 바로 Claude 호출

    # ── 컨텍스트 조합 ──
    ctx = ""
    if _last_data.get("stocks"):
        lines = "\n".join(
            f"{s['symbol']}: {s['price']:,.2f} ({'+' if s['change']>=0 else ''}{s['change']:.2f}%)"
            for s in _last_data["stocks"]
        )
        ctx += f"\n[주식]\n{lines}"
    if _last_data.get("trends"):
        ctx += "\n[트렌드]\n" + "\n".join(t['keyword'] for t in _last_data["trends"])
    if web_result:
        ctx += f"\n[웹 검색 결과]\n{web_result}"
    if yt_result:
        ctx += f"\n[YouTube 관련 영상]\n{yt_result}"

    prompt = f"""You are JARVIS, Iron Man Tony Stark's AI assistant. Always address the user as "Mr. Stark" (in English) or "스타크씨" (in Korean).
Detect the language of the user's message and reply in the same language.
Use the real-time search data below to answer accurately.
Rules:
- 2~3 sentences max, concise and clear
- Plain text only — no emojis, markdown, or special characters
- Use the search data if available for specific answers
{ctx}

User: {user_text}"""

    try:
        CLAUDE_BIN = (
            Path.home()
            / "Library/Application Support/Claude/claude-code/2.1.156/claude.app/Contents/MacOS/claude"
        )
        import os as _env_os
        env = {k: v for k, v in _env_os.environ.items() if k != "ANTHROPIC_API_KEY"}
        proc = subprocess.Popen(   # 로컬 변수 사용 — 전역 _chat_proc 접근 경쟁 방지
            [str(CLAUDE_BIN), "--model", "claude-haiku-4-5-20251001", "-p", prompt, "--allowedTools", "WebSearch"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        _chat_proc = proc          # 외부에서 kill 가능하도록 전역에도 저장
        full = ""
        for chunk in iter(lambda: proc.stdout.read(8), ""):
            full += chunk
            socketio.emit("text_chunk", chunk)
        proc.wait()
        full = full.strip()
        socketio.emit("text_done", full)
        threading.Thread(target=speak_server, args=(full,), daemon=True).start()
    except Exception as e:
        socketio.emit("error", str(e))
    finally:
        _chat_proc = None
        set_state("idle")


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_file(Path(__file__).parent / "jarvis_ui.html")


@app.route("/yes_sir")
def yes_sir():
    """미리 생성된 즉각 응답 오디오."""
    return send_file(Path(__file__).parent / "yes_sir.mp3", mimetype="audio/mpeg")


@app.route("/tts")
def tts():
    """ElevenLabs TTS → MP3."""
    import tempfile, os
    import requests as _req

    text = request.args.get("text", "").strip()
    if not text:
        return "", 400

    voice_id = ELEVENLABS_VOICE_ID or "JBFqnCBsd6RMkjVDRZzb"
    r = _req.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
        json={"text": text, "model_id": "eleven_multilingual_v2",
              "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}},
        timeout=15,
    )
    if r.status_code != 200:
        return f"ElevenLabs error: {r.text}", 500

    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp.write(r.content)
    tmp.close()

    response = send_file(tmp.name, mimetype="audio/mpeg")
    @response.call_on_close
    def _cleanup():
        try: os.unlink(tmp.name)
        except: pass
    return response


@app.route("/trigger_briefing", methods=["POST", "GET"])
def trigger_briefing():
    threading.Thread(target=run_briefing, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/push_data", methods=["POST"])
def push_data():
    """Receive pre-fetched data from jarvis_voice_free.py and broadcast to UI."""
    try:
        data = request.get_json(force=True) or {}
        socketio.emit("data", data)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Start wake word listener
    threading.Thread(target=wake_word_loop, daemon=True).start()
    # Start clap listener (runs concurrently — whichever triggers first wins)
    threading.Thread(target=clap_loop, daemon=True).start()

    print("JARVIS 서버 시작: http://localhost:5001")
    socketio.run(app, host="0.0.0.0", port=5001, debug=False, allow_unsafe_werkzeug=True)
