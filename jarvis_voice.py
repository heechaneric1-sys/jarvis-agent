#!/usr/bin/env python3
"""
Jarvis Voice Briefing
- Double-clap detection via PyAudio
- Morning briefing via Claude API
- Text-to-speech via ElevenLabs
"""

import os
import sys
import time
import struct
import math
import tempfile
import subprocess
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path.home() / ".env.jarvis")

ELEVENLABS_API_KEY = os.environ["ELEVENLABS_API_KEY"]
ELEVENLABS_VOICE_ID = os.environ["ELEVENLABS_VOICE_ID"]

# Clap detection settings
CHUNK = 1024
RATE = 44100
CLAP_THRESHOLD = 3000       # RMS amplitude to count as a clap
CLAP_COOLDOWN = 0.15        # seconds between claps (debounce)
DOUBLE_CLAP_WINDOW = 0.8    # max seconds between two claps
SILENCE_AFTER = 0.3         # seconds of silence required after clap


def rms(data: bytes) -> float:
    count = len(data) // 2
    shorts = struct.unpack(f"{count}h", data)
    return math.sqrt(sum(s * s for s in shorts) / count) if count else 0.0


def wait_for_double_clap() -> None:
    import pyaudio

    pa = pyaudio.PyAudio()
    stream = pa.open(format=pyaudio.paInt16, channels=1, rate=RATE,
                     input=True, frames_per_buffer=CHUNK)
    print("Listening for double clap... (Ctrl-C to exit)")

    last_clap_time = 0.0
    clap_count = 0

    try:
        while True:
            data = stream.read(CHUNK, exception_on_overflow=False)
            level = rms(data)
            now = time.time()

            if level > CLAP_THRESHOLD:
                if now - last_clap_time > CLAP_COOLDOWN:
                    clap_count += 1
                    print(f"  Clap {clap_count} detected (rms={level:.0f})")
                    last_clap_time = now

                    if clap_count == 2:
                        if now - last_clap_time < DOUBLE_CLAP_WINDOW:
                            print("Double clap confirmed!")
                            break
                        else:
                            clap_count = 1  # reset; too slow

            # Reset if window expired
            if clap_count == 1 and (now - last_clap_time) > DOUBLE_CLAP_WINDOW:
                clap_count = 0
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()


def generate_briefing() -> str:
    import anthropic

    client = anthropic.Anthropic()
    today = datetime.now().strftime("%Y년 %m월 %d일 %A")

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=(
            "당신은 자비스입니다. 주인을 위해 매일 아침 간결하고 활기차게 브리핑합니다. "
            "한국어로 말하며, 자연스럽고 친근한 톤을 유지합니다. "
            "브리핑은 2분 이내로 읽을 수 있게 작성합니다."
        ),
        messages=[{
            "role": "user",
            "content": (
                f"오늘은 {today}입니다. "
                "아침 브리핑을 시작해주세요. 포함할 내용:\n"
                "1. 간단한 인사 및 오늘 날짜/날씨 언급\n"
                "2. 오늘의 주요 글로벌 테크 트렌드 2-3가지\n"
                "3. AI/스타트업 관련 최신 뉴스 2가지\n"
                "4. 오늘 집중할 것 한 가지 동기부여 메시지\n"
                "음성으로 읽힐 텍스트이므로 마크다운 없이 자연스러운 문장으로만 작성하세요."
            )
        }]
    )
    return message.content[0].text


def speak(text: str) -> None:
    import requests

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }

    response = requests.post(url, json=payload, headers=headers)
    response.raise_for_status()

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        f.write(response.content)
        tmp_path = f.name

    try:
        # macOS: afplay; fallback to mpg123
        if sys.platform == "darwin":
            subprocess.run(["afplay", tmp_path], check=True)
        else:
            subprocess.run(["mpg123", "-q", tmp_path], check=True)
    finally:
        os.unlink(tmp_path)


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "clap"

    if mode == "now":
        # Run immediately without clap detection (useful for testing / cron)
        pass
    else:
        wait_for_double_clap()

    print("Generating briefing...")
    briefing = generate_briefing()
    print("\n--- BRIEFING ---")
    print(briefing)
    print("----------------\n")

    print("Speaking...")
    speak(briefing)
    print("Done.")


if __name__ == "__main__":
    main()
