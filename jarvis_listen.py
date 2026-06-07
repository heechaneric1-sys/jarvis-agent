#!/usr/bin/env python3
"""
JARVIS Voice Input
"자비스" 웨이크워드 감지 → 명령 녹음 → Google STT → Slack 전송

sounddevice로 녹음 (PyAudio 불필요)
"""

import os, sys, time, subprocess, tempfile, struct, math, wave, io
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import sounddevice as sd
import requests
import speech_recognition as sr

# ── 환경변수 로드 ──────────────────────────────────────────────────────────────
_env = Path.home() / ".env.jarvis"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.split("#")[0].strip()

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
INBOX_CHANNEL   = "C0B8WBSTWJV"

SLACK_HEADERS = {
    "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
    "Content-Type": "application/json",
}

WAKE_WORDS   = ["자비스", "jarvis", "자비", "쟈비스"]
SAMPLE_RATE  = 16000
CHANNELS_MIC = 1
SILENCE_THRESHOLD = 500   # RMS 기준 침묵 판단
SILENCE_DURATION  = 1.8   # 초 — 이 이상 침묵이면 발화 종료
MAX_RECORD_SEC    = 20


def slack_post(text: str) -> bool:
    try:
        r = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers=SLACK_HEADERS,
            json={"channel": INBOX_CHANNEL, "text": text},
            timeout=10,
        )
        return r.json().get("ok", False)
    except Exception as e:
        print(f"[Slack 오류] {e}", file=sys.stderr)
        return False


def say(text: str):
    subprocess.Popen(["say", "-v", "Yuna", text])


def record_until_silence(max_sec=MAX_RECORD_SEC) -> np.ndarray:
    """침묵이 SILENCE_DURATION 초 지속되면 녹음 종료."""
    chunks = []
    chunk_size = int(SAMPLE_RATE * 0.1)  # 100ms 단위
    silent_chunks = 0
    max_silent = int(SILENCE_DURATION / 0.1)

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS_MIC,
                        dtype="int16", blocksize=chunk_size) as stream:
        for _ in range(int(max_sec / 0.1)):
            data, _ = stream.read(chunk_size)
            chunks.append(data.copy())
            rms = np.sqrt(np.mean(data.astype(np.float32) ** 2))
            if rms < SILENCE_THRESHOLD:
                silent_chunks += 1
                if silent_chunks >= max_silent:
                    break
            else:
                silent_chunks = 0

    return np.concatenate(chunks, axis=0)


def audio_to_text(audio_np: np.ndarray) -> str:
    """numpy 배열 → WAV 파일 → Google STT."""
    recognizer = sr.Recognizer()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS_MIC)
        wf.setsampwidth(2)  # int16 = 2 bytes
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio_np.tobytes())
    buf.seek(0)

    with sr.AudioFile(buf) as source:
        audio = recognizer.record(source)

    try:
        return recognizer.recognize_google(audio, language="ko-KR")
    except sr.UnknownValueError:
        return ""
    except Exception as e:
        print(f"[STT 오류] {e}", file=sys.stderr)
        return ""


def contains_wake_word(text: str) -> bool:
    t = text.lower().replace(" ", "")
    return any(w in t for w in WAKE_WORDS)


def strip_wake_word(text: str) -> str:
    for w in WAKE_WORDS:
        text = text.lower().replace(w, "")
    return text.strip(" ,.")


def main():
    print("🎙️  JARVIS 음성 입력 시작")
    print("   '자비스' 라고 부르면 명령을 받습니다.")

    recognizer = sr.Recognizer()
    recognizer.energy_threshold = 300
    recognizer.dynamic_energy_threshold = True

    while True:
        # ── 웨이크워드 감지 루프 ──────────────────────────────────────────
        print("   대기 중...", end="\r")
        try:
            audio_np = record_until_silence(max_sec=6)
        except Exception as e:
            print(f"[녹음 오류] {e}", file=sys.stderr)
            time.sleep(1)
            continue

        # 침묵이면 스킵
        rms = np.sqrt(np.mean(audio_np.astype(np.float32) ** 2))
        if rms < SILENCE_THRESHOLD:
            continue

        text = audio_to_text(audio_np)
        if not text:
            continue

        print(f"[감지] {text}")

        if not contains_wake_word(text):
            continue

        # ── 웨이크워드 확인 → 명령 수신 ──────────────────────────────────
        say("네")
        print("🔴 명령 대기...")

        command = strip_wake_word(text)

        if not command:
            # 웨이크워드만 말한 경우 → 다음 발화를 명령으로
            try:
                cmd_audio = record_until_silence(max_sec=MAX_RECORD_SEC)
                command = audio_to_text(cmd_audio)
            except Exception as e:
                print(f"[명령 녹음 오류] {e}", file=sys.stderr)
                say("듣지 못했습니다")
                continue

        if not command:
            say("듣지 못했습니다")
            continue

        print(f"[명령] {command}")

        # ── Slack 전송 ────────────────────────────────────────────────────
        ok = slack_post(command)
        if ok:
            say("알겠습니다")
            print(f"[Slack ✅] {command}")
        else:
            say("전송 실패했습니다")
            print(f"[Slack ❌] 전송 실패", file=sys.stderr)


if __name__ == "__main__":
    main()
