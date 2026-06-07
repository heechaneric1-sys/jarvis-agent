#!/usr/bin/env python3
import sys
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)
"""
JARVIS Voice Briefing — complete rewrite
  • Wake:     "Hey Jarvis" (SpeechRecognition via sounddevice) OR double-clap
  • Data:     Google Trends, News (NPR/CNN/BBC), Stocks+BTC (yfinance)
  • Briefing: claude CLI → Korean narration
  • Voice:    say -v Daniel -r 210
  • Exits after one briefing (no repeat loop)
  • Posts data to jarvis_server web UI if running
"""

import io, json, math, re, signal, socket, struct, subprocess
import sys, threading, time, warnings
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Optional

warnings.filterwarnings("ignore")   # suppress urllib3/yfinance noise
socket.setdefaulttimeout(5)

import requests
import yfinance as yf

# ── Config ────────────────────────────────────────────────────────────────────
CLAUDE_BIN = (
    Path.home()
    / "Library/Application Support/Claude/claude-code/2.1.156/claude.app/Contents/MacOS/claude"
)
JARVIS_SERVER = "http://localhost:5001"
HEADERS       = {"User-Agent": "Mozilla/5.0"}
TIMEOUT       = 3    # per-request timeout (seconds)
FETCH_WALL    = 6    # total parallel-fetch deadline (seconds)

WAKE_PHRASES  = ["hey jarvis", "헤이 자비스", "jarvis", "자비스"]
SAMPLE_RATE   = 16000
LISTEN_SECS   = 3

# Clap detection thresholds (sounddevice float32 RMS)
CLAP_RMS      = 0.08   # 낮출수록 민감 (0.25→0.08)
CLAP_COOLDOWN = 0.15
CLAP_WINDOW   = 0.8


# ═════════════════════════════════════════════════════════════════════════════
# DATA FETCHERS — all return str | None, all have try/except + timeout
# ═════════════════════════════════════════════════════════════════════════════

def fetch_trends() -> Optional[str]:
    """Google Trends US — top 5 real-time trending searches."""
    try:
        r = requests.get(
            "https://trends.google.com/trending/rss?geo=US",
            headers=HEADERS, timeout=TIMEOUT,
        )
        r.raise_for_status()
        tree = ET.fromstring(r.content)
        ns   = {"ht": "https://trends.google.com/trending/rss"}
        rows = []
        for item in tree.findall(".//item")[:5]:
            title   = (item.findtext("title") or "").strip()
            traffic = (item.findtext("ht:approx_traffic", None, ns) or "").strip()
            if title:
                rows.append(f"• {title}" + (f" ({traffic} searches)" if traffic else ""))
        return "\n".join(rows) or None
    except Exception:
        return None


def fetch_news() -> Optional[str]:
    """Top 3 US headlines — NPR → CNN → BBC."""
    feeds = [
        ("NPR",  "https://feeds.npr.org/1001/rss.xml"),
        ("CNN",  "http://rss.cnn.com/rss/edition.rss"),
        ("BBC",  "http://feeds.bbci.co.uk/news/rss.xml"),
    ]
    items: list[str] = []
    for label, url in feeds:
        if len(items) >= 3:
            break
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            tree = ET.fromstring(r.content)
            for item in tree.findall(".//item")[:5]:
                if len(items) >= 3:
                    break
                title = (item.findtext("title") or "").strip()
                if title:
                    items.append(f"• [{label}] {title}")
        except Exception:
            continue
    return "\n".join(items) or None


def fetch_stocks() -> Optional[str]:
    """US indices + Korea + Bitcoin via yfinance — parallel per symbol."""
    SYMBOLS = {
        "^GSPC":   "S&P 500",
        "^IXIC":   "NASDAQ",
        "^DJI":    "DOW",
        "^KS11":   "KOSPI",
        "^KQ11":   "KOSDAQ",
        "BTC-USD": "Bitcoin",
    }
    rows: dict[str, str] = {}

    def _fetch_one(sym, name):
        try:
            info  = yf.Ticker(sym).fast_info
            price = info.last_price
            prev  = info.previous_close or price
            chg   = ((price - prev) / prev * 100) if prev else 0
            arrow = "▲" if chg >= 0 else "▼"
            unit  = "" if sym in ("^KS11", "^KQ11") else "$"
            rows[sym] = f"• {name}: {unit}{price:,.2f} {arrow}{abs(chg):.2f}%"
        except Exception:
            pass

    threads = [threading.Thread(target=_fetch_one, args=(s, n), daemon=True)
               for s, n in SYMBOLS.items()]
    for t in threads: t.start()
    for t in threads: t.join(timeout=4)

    return "\n".join(rows[s] for s in SYMBOLS if s in rows) or None


def fetch_youtube() -> Optional[str]:
    """
    YouTube trending via Google Trends US (no API key needed).
    True YouTube RSS is blocked without an API key.
    Returns trending topic strings labelled as global trending.
    """
    return fetch_trends()   # reuse — same data, already fetched separately


def fetch_quote() -> Optional[str]:
    """Daily quote from ZenQuotes.io."""
    try:
        r = requests.get("https://zenquotes.io/api/random", headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        d = r.json()[0]
        return f'"{d["q"]}" — {d["a"]}'
    except Exception:
        return None


# ═════════════════════════════════════════════════════════════════════════════
# PARALLEL COLLECTION — daemon threads, hard wall-clock deadline
# ═════════════════════════════════════════════════════════════════════════════

def collect_data() -> dict:
    print("📡 실시간 데이터 수집 중...")
    tasks = {
        "trends":   fetch_trends,
        "news":     fetch_news,
        "stocks":   fetch_stocks,
        "quote":    fetch_quote,
    }
    results = {k: None for k in tasks}

    def _run(k, fn):
        try:
            results[k] = fn()
        except Exception:
            pass

    threads = {k: threading.Thread(target=_run, args=(k, fn), daemon=True)
               for k, fn in tasks.items()}
    for t in threads.values():
        t.start()

    deadline = time.monotonic() + FETCH_WALL
    for k, t in threads.items():
        t.join(timeout=max(deadline - time.monotonic(), 0))
        ok = results[k] is not None
        live = t.is_alive()
        print(f"  {'✓' if ok else '✗'} {k}" + (" (timeout)" if live else ""))

    return results


# ═════════════════════════════════════════════════════════════════════════════
# WEB UI — push data to jarvis_server if running
# ═════════════════════════════════════════════════════════════════════════════

def push_to_webui(data: dict):
    """POST fetched data to the web server so panels update immediately."""
    # Convert text strings to structured lists for the web panels
    payload = {}

    if data.get("trends"):
        payload["trends"] = [
            {"keyword": line.lstrip("• ").split(" (")[0],
             "traffic": (line.split("(")[1].rstrip(")") if "(" in line else "")}
            for line in data["trends"].splitlines() if line.strip()
        ]

    if data.get("news"):
        payload["news"] = [
            re.sub(r"^•\s*\[\w+\]\s*", "", line)
            for line in data["news"].splitlines() if line.strip()
        ]

    if data.get("stocks"):
        rows = []
        for line in data["stocks"].splitlines():
            # "• S&P 500: $7,383.74 ▲2.64%"
            m = re.match(r"•\s*(.+?):\s*\$?([\d,\.]+)\s*([▲▼])([\d\.]+)%", line)
            if m:
                chg = float(m.group(4)) * (1 if m.group(3) == "▲" else -1)
                rows.append({
                    "symbol": m.group(1),
                    "price":  float(m.group(2).replace(",", "")),
                    "change": chg,
                })
        payload["stocks"] = rows

    if data.get("quote"):
        m = re.match(r'"(.+)"\s*—\s*(.+)', data["quote"])
        if m:
            payload["quote"] = {"text": m.group(1), "author": m.group(2)}

    try:
        requests.post(f"{JARVIS_SERVER}/push_data", json=payload, timeout=2)
    except Exception:
        pass   # server not running — silent skip


# ═════════════════════════════════════════════════════════════════════════════
# BRIEFING — claude CLI
# ═════════════════════════════════════════════════════════════════════════════

def build_prompt(data: dict) -> str:
    today = datetime.now().strftime("%Y년 %m월 %d일 %A")
    parts = [f"오늘은 {today}입니다."]

    if data.get("trends"):
        parts.append(f"[트렌딩 데이터]\n{data['trends']}")
    if data.get("news"):
        parts.append(f"[뉴스 데이터]\n{data['news']}")
    if data.get("stocks"):
        parts.append(f"[주식 데이터]\n{data['stocks']}")
    if data.get("quote"):
        parts.append(f"[명언]\n{data['quote']}")

    context = "\n\n".join(parts)

    return f"""{context}

위 실시간 데이터를 바탕으로 아이언맨의 자비스처럼 간결하고 전문적인 한국어 브리핑을 해줘.
반드시 아래 5개 섹션 순서대로, 각 섹션 1~2문장 이내로 짧고 빠르게:

1. 인사: 날짜와 함께 자비스 스타일 짧은 인사
2. 트렌드: 지금 가장 핫한 트렌드 키워드 3~5개를 자연스럽게 언급
3. 뉴스: 오늘 주요 뉴스 3개를 한 줄씩 핵심만
4. 주식: 미국 3대 지수와 코스피 코스닥 수치와 등락률 언급
5. 명언: 짧고 임팩트 있게 끝맺음

규칙:
- 이모지, 마크다운, 특수문자, 숫자 기호(▲▼$) 없이 순수 텍스트
- 퍼센트는 "퍼센트"로 읽을 것
- 달러는 "달러"로 읽을 것
- 전체 브리핑 30초 안에 읽힐 분량
- 자비스처럼 차갑고 정확하게, 감탄사 없이"""


def speak_streaming(prompt: str):
    """
    Stream Claude output and pipe each sentence to `say` immediately.
    First sentence starts playing before Claude finishes the whole briefing.
    """
    import re

    claude = subprocess.Popen(
        [str(CLAUDE_BIN), "-p", prompt],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )

    buf       = ""
    say_procs: list[subprocess.Popen] = []
    SPLIT     = re.compile(r'(?<=[.!?\n])\s+')

    def _old_sigint(sig, frame):
        for p in say_procs:
            if p.poll() is None: p.kill()
        claude.kill()
        print("\n[자비스] 중지됨.")
        sys.exit(0)

    old = signal.signal(signal.SIGINT, _old_sigint)

    try:
        for chunk in iter(lambda: claude.stdout.read(16), ""):
            buf += chunk
            print(chunk, end="", flush=True)
            # Split on sentence boundary
            parts = SPLIT.split(buf)
            for sentence in parts[:-1]:
                sentence = sentence.strip()
                if sentence:
                    p = subprocess.Popen(["say", "-v", "Yuna", "-r", "200", sentence])
                    say_procs.append(p)
            buf = parts[-1]  # keep remainder

        # Flush last bit
        if buf.strip():
            say_procs.append(subprocess.Popen(["say", "-v", "Yuna", "-r", "200", buf.strip()]))

        # Wait for all say processes
        for p in say_procs:
            p.wait()
    finally:
        signal.signal(signal.SIGINT, old)

    claude.wait()


def get_briefing(prompt: str) -> str:
    """Run claude CLI and return full text. Streams to stdout as it arrives."""
    proc = subprocess.Popen(
        [str(CLAUDE_BIN), "-p", prompt],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    text = ""
    for chunk in iter(lambda: proc.stdout.read(32), ""):
        text += chunk
        print(chunk, end="", flush=True)
    proc.wait()
    if proc.returncode != 0:
        print("\nClaude CLI 오류:", proc.stderr.read()[:200])
        sys.exit(1)
    return text.strip()


# ═════════════════════════════════════════════════════════════════════════════
# TTS — say -v Daniel -r 210, Ctrl+C kills instantly
# ═════════════════════════════════════════════════════════════════════════════

_say_proc: Optional[subprocess.Popen] = None


def stop_speaking():
    global _say_proc
    if _say_proc and _say_proc.poll() is None:
        _say_proc.kill()
        _say_proc = None
        print("\n[자비스] 중지됨.")


def speak(text: str):
    global _say_proc
    _say_proc = subprocess.Popen(["say", "-v", "Reed", "-r", "280", text])

    def _stop(sig, frame):
        stop_speaking()
        sys.exit(0)

    old = signal.signal(signal.SIGINT, _stop)
    try:
        _say_proc.wait()
    finally:
        signal.signal(signal.SIGINT, old)
        _say_proc = None


# ═════════════════════════════════════════════════════════════════════════════
# WAKE WORD — sounddevice + SpeechRecognition (no PyAudio needed)
# ═════════════════════════════════════════════════════════════════════════════

def _record_wav(seconds: int) -> bytes:
    import sounddevice as sd, wave
    audio = sd.rec(int(seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE,
                   channels=1, dtype="int16", blocking=True)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE); wf.writeframes(audio.tobytes())
    buf.seek(0)
    return buf.read()


def _recognize(wav_bytes: bytes) -> str:
    import speech_recognition as sr
    rec = sr.Recognizer()
    try:
        return rec.recognize_google(
            sr.AudioData(wav_bytes, SAMPLE_RATE, 2), language="ko-KR"
        ).lower()
    except Exception:
        return ""


def wait_for_wake_word() -> bool:
    """Returns True when wake phrase heard; False if mic unavailable."""
    try:
        import sounddevice      # noqa
        import speech_recognition  # noqa
    except ImportError:
        print("sounddevice / SpeechRecognition 없음 — 박수 감지로 전환")
        return False

    print('🎤 대기 중... "헤이 자비스"라고 말하세요.')
    while True:
        try:
            wav  = _record_wav(LISTEN_SECS)
            text = _recognize(wav)
            if text:
                print(f"  들림: {text}")
            if any(p in text for p in WAKE_PHRASES):
                print("✅ 웨이크 워드 감지!")
                subprocess.run(["say", "-v", "Reed", "-r", "280", "네, 브리핑 시작합니다."])
                return True
        except KeyboardInterrupt:
            sys.exit(0)
        except Exception:
            time.sleep(0.3)


# ═════════════════════════════════════════════════════════════════════════════
# CLAP DETECTION — sounddevice RMS, double-clap within 0.8 s
# ═════════════════════════════════════════════════════════════════════════════

def detect_clap():
    try:
        import sounddevice as sd
        import numpy as np
    except ImportError:
        input("sounddevice 없음 — Enter 키로 시작: ")
        return

    print(f"👏 박수 두 번으로 시작하세요... (임계값 RMS={CLAP_RMS})")
    done       = threading.Event()
    clap_count = 0
    last_clap  = 0.0
    frame_idx  = 0

    def callback(indata, frames, t, status):
        nonlocal clap_count, last_clap, frame_idx
        rms = float(np.sqrt(np.mean(indata ** 2)))
        now = time.time()

        # 매 20프레임마다 현재 RMS 출력 (디버깅용)
        frame_idx += 1
        if frame_idx % 20 == 0:
            bar = "█" * int(rms * 80)
            print(f"\r  RMS: {rms:.3f}  {bar:<20}", end="", flush=True)

        if rms > CLAP_RMS and (now - last_clap) > CLAP_COOLDOWN:
            clap_count += 1
            last_clap   = now
            print(f"\n  👏 {clap_count}/2  (RMS={rms:.3f})")
            if clap_count >= 2:
                done.set()

        if clap_count == 1 and (time.time() - last_clap) > CLAP_WINDOW:
            clap_count = 0

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                        blocksize=int(SAMPLE_RATE * 0.05), callback=callback):
        done.wait(timeout=120)
    print()


# ═════════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════════

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "clap"  # 기본값: 박수

    # ── Step 1: trigger ───────────────────────────────────────────────────────
    if mode == "now":
        print("🚀 브리핑 시작합니다...")
    elif mode == "wake":
        if not wait_for_wake_word():
            detect_clap()
    else:                           # clap (기본값)
        detect_clap()

    # ── Step 2: fetch data ────────────────────────────────────────────────────
    data = collect_data()

    # ── Step 3: push to web UI ────────────────────────────────────────────────
    push_to_webui(data)

    # ── Step 4: generate + speak simultaneously (stream) ─────────────────────
    print("\n💬 브리핑 생성 및 재생 중... (Ctrl+C 중지)\n")
    try:
        speak_streaming(build_prompt(data))
    except KeyboardInterrupt:
        stop_speaking()

    print("✅ 브리핑 완료. 종료합니다.")
    sys.exit(0)


if __name__ == "__main__":
    main()
