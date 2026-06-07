#!/usr/bin/env python3
"""
JARVIS Menubar App
- 메뉴바에서 브리핑 시작
- GUI 세션에서 실행되므로 마이크 접근 가능
- "헤이 자비스" 감지 → 서버에 trigger 전송
"""

import io
import subprocess
import threading
import time
import wave
import requests as _req
from pathlib import Path

import rumps

PYTHON     = "/usr/bin/python3"
SCRIPT     = Path(__file__).parent / "jarvis_voice_free.py"
SERVER     = "http://localhost:5001"
SAMPLE_RATE = 16000
WAKE_PHRASES = ["헤이 자비스", "hey jarvis", "자비스", "jarvis", "헤이자비스"]


class JarvisApp(rumps.App):
    def __init__(self):
        super().__init__("🤖", quit_button=None)
        self.menu = [
            rumps.MenuItem("▶  브리핑 시작",    callback=self.start_briefing),
            rumps.MenuItem("⏹  중지",           callback=self.stop_briefing),
            None,
            rumps.MenuItem("🌐  웹 UI 열기",     callback=self.open_web),
            rumps.MenuItem("🎤  음성 인식: ON",  callback=self.toggle_wake),
            None,
            rumps.MenuItem("Quit JARVIS",        callback=rumps.quit_application),
        ]
        self._proc       = None
        self._wake_on    = True
        self._wake_thread = threading.Thread(target=self._wake_loop, daemon=True)
        self._wake_thread.start()

    # ── 웨이크워드 루프 ────────────────────────────────────────────────────────

    def _record_wav(self) -> bytes:
        import sounddevice as sd
        audio = sd.rec(int(3 * SAMPLE_RATE), samplerate=SAMPLE_RATE,
                       channels=1, dtype="int16", blocking=True)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1); wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE); wf.writeframes(audio.tobytes())
        buf.seek(0)
        return buf.read()

    def _recognize(self, wav_bytes: bytes) -> str:
        import speech_recognition as sr
        rec = sr.Recognizer()
        try:
            return rec.recognize_google(
                sr.AudioData(wav_bytes, SAMPLE_RATE, 2), language="ko-KR"
            ).lower()
        except Exception:
            return ""

    def _wake_loop(self):
        try:
            import sounddevice      # noqa
            import speech_recognition  # noqa
        except ImportError:
            print("[wake] sounddevice / SpeechRecognition 없음")
            return

        print("[wake] 🎤 대기 중...")
        while True:
            try:
                if not self._wake_on:
                    time.sleep(1)
                    continue
                wav  = self._record_wav()
                text = self._recognize(wav)
                if text:
                    print(f"[wake] 들림: {text}")
                if any(p in text for p in WAKE_PHRASES):
                    print("[wake] ✅ 헤이 자비스!")
                    self.title = "⚡"
                    self._trigger_server()
                    time.sleep(30)   # 브리핑 중 재트리거 방지
                    self.title = "🤖"
            except Exception as e:
                print(f"[wake] 오류: {e}")
                time.sleep(1)

    def _trigger_server(self):
        """웹 서버에 브리핑 트리거."""
        try:
            _req.post(f"{SERVER}/trigger_briefing", timeout=3)
        except Exception:
            # 서버 없으면 CLI로 직접 실행
            self.start_briefing()

    # ── 메뉴 액션 ─────────────────────────────────────────────────────────────

    def start_briefing(self, _=None):
        if self._proc and self._proc.poll() is None:
            return
        self.title = "⏳"

        def _run():
            self._proc = subprocess.Popen(
                [PYTHON, str(SCRIPT), "now"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            self._proc.wait()
            self.title = "🤖"

        threading.Thread(target=_run, daemon=True).start()

    def stop_briefing(self, _=None):
        if self._proc and self._proc.poll() is None:
            self._proc.kill()
            self._proc = None
        self.title = "🤖"

    def toggle_wake(self, sender):
        self._wake_on = not self._wake_on
        sender.title  = f"🎤  음성 인식: {'ON' if self._wake_on else 'OFF'}"

    def open_web(self, _=None):
        subprocess.run(["open", SERVER])


if __name__ == "__main__":
    JarvisApp().run()
