#!/usr/bin/env python3
"""
JARVIS Slack Agent — 24/7 background daemon
agent-command-center inbox 폴링 → Claude 분류 → 에이전트 채널 라우팅

라우팅:
  YouTube URL / 유튜브 트렌드  → agent-youtube-trends
  쇼츠/스크립트/훅/릴스        → agent-shorts-scripter
  블로그/글 작성               → agent-blog-writer
  번역                        → agent-paper-translator
  크롤링/스크래핑              → agent-web-crawler
  영상 편집                   → agent-video-editor
  아이디어/키워드 리서치        → 웹검색+YouTube+쇼츠 자동생성 → command-center
  나머지                      → agent-command-center
"""

import os, sys, time, subprocess, re, tempfile, json, urllib.parse
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import requests
import anthropic

# ── 환경변수 로드 ──────────────────────────────────────────────────────────────
_env = Path.home() / ".env.jarvis"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.split("#")[0].strip()

SLACK_BOT_TOKEN  = os.environ.get("SLACK_BOT_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
YOUTUBE_API_KEY  = os.environ.get("YOUTUBE_API_KEY", "")
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "")

if not SLACK_BOT_TOKEN:
    sys.exit("❌ SLACK_BOT_TOKEN 없음. ~/.env.jarvis에 추가하세요.")

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SLACK_HEADERS = {
    "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
    "Content-Type": "application/json",
}

# ── 채널 ID ────────────────────────────────────────────────────────────────────
CHANNEL_IDS = {
    "command-center":    "C0B8WBSTWJV",
    "youtube-trends":    "C0B8WCB4BSM",
    "shorts-scripter":   "C0B9NRMSZNC",
    "blog-writer":       "C0B8D3CP8KZ",
    "video-editor":      "C0B9NRN9ETA",
    "web-crawler":       "C0B8N67PJP5",
    "hook-generator":    "C0B8WCB4PED",
    "comment-harvester": "C0B8WCCKE5P",
    "email-manager":     "C0B8N67QL7M",
    "paper-translator":  "C0B8SF813TQ",
    "voice-toolkit":     "C0B8UH9TBDG",
    "linkedin-briefing": "C0B8N67F4TV",
    "youtube-motion":    "C0B8Y5XH51S",
    "daily-briefing":    "C0B9NRMNG72",
    "learning-queue":    "C0B9NRNGY80",
    "n8n-builder":       "C0B8Y5C4SNQ",
}

INBOX_ID = CHANNEL_IDS["command-center"]
_last_ts: dict[str, str] = {}


# ── Slack API ─────────────────────────────────────────────────────────────────

def slack_get(endpoint: str, params: dict) -> dict:
    r = requests.get(
        f"https://slack.com/api/{endpoint}",
        headers=SLACK_HEADERS, params=params, timeout=10,
    )
    return r.json()


def slack_post(channel_id: str, text: str, thread_ts: str = None) -> dict:
    if channel_id.startswith("REPLACE_ME"):
        channel_id = INBOX_ID
    payload = {"channel": channel_id, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    r = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers=SLACK_HEADERS, json=payload, timeout=10,
    )
    return r.json()


def get_new_messages(channel_id: str) -> list[dict]:
    params = {"channel": channel_id, "limit": 10}
    last = _last_ts.get(channel_id)
    if last:
        params["oldest"] = last
    data = slack_get("conversations.history", params)
    messages = data.get("messages", [])
    if not messages:
        return []
    new_msgs = [m for m in messages if m.get("bot_id") is None and m.get("text")]
    if new_msgs:
        _last_ts[channel_id] = new_msgs[0]["ts"]
    return list(reversed(new_msgs))


# ── ElevenLabs TTS ────────────────────────────────────────────────────────────

def speak(text: str) -> None:
    if not ELEVENLABS_API_KEY or not ELEVENLABS_VOICE_ID:
        return
    resp = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",
        headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
        json={
            "text": text[:500],
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        },
        timeout=30,
    )
    if not resp.ok:
        print(f"[ElevenLabs] 오류 {resp.status_code}")
        return
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        f.write(resp.content)
        tmp = f.name
    try:
        subprocess.run(["afplay", tmp], check=True)
    finally:
        os.unlink(tmp)


# ── 웹 검색 (DuckDuckGo + YouTube) ───────────────────────────────────────────

def search_duckduckgo(query: str) -> str:
    """DuckDuckGo Instant Answer API로 요약 정보 가져오기."""
    try:
        r = requests.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"},
            timeout=10,
        )
        data = r.json()
        results = []
        if data.get("Abstract"):
            results.append(f"[요약] {data['Abstract']}")
        for topic in data.get("RelatedTopics", [])[:5]:
            if isinstance(topic, dict) and topic.get("Text"):
                results.append(f"• {topic['Text'][:150]}")
        return "\n".join(results) if results else "검색 결과 없음"
    except Exception as e:
        return f"DuckDuckGo 오류: {e}"


def search_youtube(query: str, max_results: int = 5) -> str:
    """YouTube Data API로 인기 영상 검색."""
    if not YOUTUBE_API_KEY:
        return "YOUTUBE_API_KEY 미설정"
    try:
        r = requests.get(
            "https://www.googleapis.com/youtube/v3/search",
            params={
                "part": "snippet",
                "q": query,
                "type": "video",
                "order": "viewCount",
                "maxResults": max_results,
                "key": YOUTUBE_API_KEY,
                "relevanceLanguage": "ko",
            },
            timeout=10,
        )
        items = r.json().get("items", [])
        lines = []
        for item in items:
            snip = item["snippet"]
            vid_id = item["id"]["videoId"]
            lines.append(
                f"• [{snip['channelTitle']}] {snip['title']}\n"
                f"  https://youtu.be/{vid_id}"
            )
        return "\n".join(lines) if lines else "YouTube 결과 없음"
    except Exception as e:
        return f"YouTube 오류: {e}"


def search_youtube_channels(query: str) -> str:
    """경쟁/레퍼런스 채널 검색."""
    if not YOUTUBE_API_KEY:
        return "YOUTUBE_API_KEY 미설정"
    try:
        r = requests.get(
            "https://www.googleapis.com/youtube/v3/search",
            params={
                "part": "snippet",
                "q": query,
                "type": "channel",
                "order": "relevance",
                "maxResults": 5,
                "key": YOUTUBE_API_KEY,
            },
            timeout=10,
        )
        items = r.json().get("items", [])
        lines = []
        for item in items:
            snip = item["snippet"]
            ch_id = item["id"]["channelId"]
            lines.append(
                f"• {snip['channelTitle']} — {snip.get('description','')[:80]}\n"
                f"  https://youtube.com/channel/{ch_id}"
            )
        return "\n".join(lines) if lines else "채널 결과 없음"
    except Exception as e:
        return f"채널 검색 오류: {e}"


# ── Claude 호출 ───────────────────────────────────────────────────────────────

def call_claude(system: str, user: str, model: str = "claude-sonnet-4-6", max_tokens: int = 2048) -> str:
    resp = claude.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return resp.content[0].text.strip()


# ── 분류 ──────────────────────────────────────────────────────────────────────

ROUTE_KEYS = [
    "youtube-trends", "shorts-scripter", "blog-writer",
    "paper-translator", "web-crawler", "video-editor",
    "hook-generator", "comment-harvester", "email-manager",
    "voice-toolkit", "linkedin-briefing", "youtube-motion",
    "daily-briefing", "learning-queue", "n8n-builder",
    "research", "command-center",
]

def classify(text: str) -> str:
    result = call_claude(
        system=(
            "메시지를 읽고 아래 카테고리 중 하나만 정확히 출력해라. 다른 말 금지.\n"
            "youtube-trends: YouTube URL 포함하거나 유튜브 트렌드/영상 분석 요청\n"
            "shorts-scripter: 쇼츠/스크립트/릴스/틱톡 콘텐츠 제작 요청\n"
            "hook-generator: 훅 문장/오프닝/첫 줄 생성 요청\n"
            "blog-writer: 블로그/글/아티클/포스팅 작성 요청\n"
            "paper-translator: 번역 요청 (논문, 문서, 텍스트)\n"
            "web-crawler: 크롤링/스크래핑/웹 검색/정보 수집 요청\n"
            "video-editor: 영상 편집/자막/썸네일/컷편집 관련 요청\n"
            "youtube-motion: 유튜브 모션그래픽/인트로/아웃트로/애니메이션 요청\n"
            "comment-harvester: 댓글 수집/분석/반응 파악 요청\n"
            "email-manager: 이메일 작성/관리/자동화 요청\n"
            "voice-toolkit: 음성/TTS/나레이션/보이스 관련 요청\n"
            "linkedin-briefing: 링크드인 포스팅/프로필/네트워킹 관련 요청\n"
            "daily-briefing: 오늘의 브리핑/뉴스/일정/요약 요청\n"
            "learning-queue: 학습/공부/강의/책 관련 요청\n"
            "n8n-builder: n8n/자동화/워크플로우/노코드 관련 요청\n"
            "research: 아이디어/키워드를 던지며 트렌드·경쟁채널·레퍼런스·인사이트 조사를 원하는 경우\n"
            "command-center: 위 어디에도 해당 안 되는 나머지"
        ),
        user=text,
        model="claude-haiku-4-5-20251001",
        max_tokens=20,
    )
    route = result.strip().lower()
    return route if route in ROUTE_KEYS else "command-center"


# ── 리서치 파이프라인 ─────────────────────────────────────────────────────────

def run_research_pipeline(text: str, thread_ts: str) -> None:
    """
    아이디어/키워드 → 웹검색 + YouTube 조사 → 트렌드 정리 → 쇼츠 스크립트
    모든 결과를 command-center에 포스팅.
    """
    slack_post(INBOX_ID, "🔍 리서치 시작... (웹검색 + YouTube 분석 중)", thread_ts=thread_ts)

    # 1. 키워드 추출
    keyword = call_claude(
        system="메시지에서 핵심 검색 키워드 1~3개만 추출해서 한 줄로 출력. 다른 말 금지.",
        user=text,
        model="claude-haiku-4-5-20251001",
        max_tokens=30,
    )
    print(f"[research] 키워드: {keyword}")

    # 2. 병렬 검색
    web_result     = search_duckduckgo(keyword)
    yt_videos      = search_youtube(keyword)
    yt_channels    = search_youtube_channels(keyword)

    # 3. Claude로 트렌드 분석 및 인사이트 정리
    research_summary = call_claude(
        system=(
            "너는 JARVIS다. 콘텐츠 전략가이자 트렌드 분석가.\n"
            "아래 검색 결과를 바탕으로 다음을 정리해줘:\n"
            "1. 핵심 트렌드 요약 (3줄)\n"
            "2. 경쟁/레퍼런스 채널 분석 (강점, 콘텐츠 방향)\n"
            "3. 콘텐츠 기획 인사이트 (나라면 이렇게 만든다)\n"
            "4. 추천 키워드/해시태그 5개\n"
            "한국어로, 마크다운 없이, 실용적으로."
        ),
        user=(
            f"[원본 요청]\n{text}\n\n"
            f"[웹 검색 결과]\n{web_result}\n\n"
            f"[YouTube 인기 영상]\n{yt_videos}\n\n"
            f"[관련 채널]\n{yt_channels}"
        ),
        max_tokens=1500,
    )

    # 4. 쇼츠 스크립트 자동 생성
    shorts_script = call_claude(
        system=(
            "너는 JARVIS다. YouTube Shorts 스크립트 전문가.\n"
            "리서치 결과를 바탕으로 60초 쇼츠 스크립트를 작성해줘.\n"
            "형식:\n"
            "[훅 0~3초] 시청자를 멈추게 하는 첫 문장\n"
            "[본문 3~50초] 핵심 내용 3~4포인트\n"
            "[CTA 50~60초] 구독/좋아요 유도\n"
            "한국어로, 실제 말하듯 자연스럽게."
        ),
        user=f"[주제]\n{text}\n\n[리서치 요약]\n{research_summary}",
        max_tokens=800,
    )

    # 5. 결과 포스팅
    summary_msg = (
        f"📊 *리서치 완료* — `{keyword}`\n"
        f"{'─' * 40}\n"
        f"{research_summary}"
    )
    slack_post(INBOX_ID, summary_msg, thread_ts=thread_ts)

    shorts_msg = (
        f"🎬 *자동 생성 쇼츠 스크립트*\n"
        f"{'─' * 40}\n"
        f"{shorts_script}"
    )
    slack_post(INBOX_ID, shorts_msg, thread_ts=thread_ts)

    # 쇼츠 채널에도 스크립트 별도 포스팅
    slack_post(
        CHANNEL_IDS["shorts-scripter"],
        f"*[자동 리서치 → 스크립트]* 주제: {keyword}\n\n{shorts_script}",
    )

    print(f"[research] 완료")
    speak(f"리서치 완료했습니다. {keyword} 관련 트렌드와 쇼츠 스크립트를 정리했어요.")


# ── 에이전트 시스템 프롬프트 ──────────────────────────────────────────────────

AGENT_SYSTEMS = {
    "youtube-trends": (
        "너는 JARVIS다. YouTube 트렌드 분석 전문가.\n"
        "YouTube URL이 있으면 영상 주제, 바이럴 이유, 콘텐츠 인사이트를 분석해줘.\n"
        "한국어로, 마크다운 없이, 간결하게."
    ),
    "shorts-scripter": (
        "너는 JARVIS다. YouTube Shorts 스크립트 전문가.\n"
        "60초 이내 쇼츠 스크립트를 작성해줘.\n"
        "형식: [훅 0~3초] → [본문 3~50초, 3~5포인트] → [CTA 50~60초]\n"
        "한국어로, 실제 말하듯 자연스럽게."
    ),
    "blog-writer": (
        "너는 JARVIS다. 블로그/콘텐츠 작성 전문가.\n"
        "요청한 주제로 블로그 글을 작성해줘.\n"
        "구조: 제목 → 도입 → 본문(소제목 포함) → 마무리\n"
        "한국어로, 읽기 쉽게."
    ),
    "paper-translator": (
        "너는 JARVIS다. 전문 번역가.\n"
        "주어진 텍스트를 자연스럽게 번역해줘.\n"
        "한국어 ↔ 영어 자동 감지. 원문 뉘앙스와 전문 용어 유지."
    ),
    "web-crawler": (
        "너는 JARVIS다. 웹 리서치 전문가.\n"
        "요청한 주제에 대해 핵심 정보를 정리해줘.\n"
        "출처 명시, 최신 정보 우선, 한국어로 요약."
    ),
    "video-editor": (
        "너는 JARVIS다. 영상 편집 전문가.\n"
        "영상 편집 요청(자막, 썸네일, 컷편집, 효과)에 대해 단계별로 알려줘.\n"
        "한국어로, 명확하게."
    ),
    "hook-generator": (
        "너는 JARVIS다. 바이럴 훅 전문가.\n"
        "시청자가 영상을 멈추게 만드는 강력한 훅 문장 5개를 만들어줘.\n"
        "각각 다른 스타일(궁금증 유발/충격/공감/숫자/반전)로.\n"
        "한국어로, 짧고 강렬하게."
    ),
    "comment-harvester": (
        "너는 JARVIS다. 커뮤니티 분석 전문가.\n"
        "댓글/반응 분석 요청을 처리해줘.\n"
        "핵심 감정, 반복되는 키워드, 콘텐츠 개선 인사이트를 정리해줘.\n"
        "한국어로, 데이터 기반으로."
    ),
    "email-manager": (
        "너는 JARVIS다. 비즈니스 이메일 전문가.\n"
        "요청에 맞는 이메일을 작성하거나 관리 방법을 알려줘.\n"
        "전문적이고 간결하게, 한국어/영어 요청에 따라."
    ),
    "voice-toolkit": (
        "너는 JARVIS다. 음성/나레이션 전문가.\n"
        "TTS 스크립트, 나레이션 문체, 보이스 방향을 제안해줘.\n"
        "읽기 쉽고 자연스러운 구어체로, 한국어."
    ),
    "linkedin-briefing": (
        "너는 JARVIS다. 링크드인 전문가.\n"
        "링크드인 포스팅, 프로필 최적화, 네트워킹 전략을 도와줘.\n"
        "전문적이면서 인간적인 톤으로, 한국어."
    ),
    "youtube-motion": (
        "너는 JARVIS다. 유튜브 영상 연출 전문가.\n"
        "모션그래픽, 인트로/아웃트로, 화면 구성, 애니메이션 방향을 제안해줘.\n"
        "구체적인 툴(After Effects, CapCut 등)과 방법 포함. 한국어."
    ),
    "daily-briefing": (
        "너는 JARVIS다. 개인 비서.\n"
        "오늘의 주요 테크/AI/콘텐츠 트렌드를 브리핑해줘.\n"
        "3분 안에 읽을 수 있게 핵심만. 한국어, 마크다운 없이."
    ),
    "learning-queue": (
        "너는 JARVIS다. 학습 코치.\n"
        "학습 요청에 대해 커리큘럼, 추천 자료, 학습 순서를 제안해줘.\n"
        "실용적이고 단계별로. 한국어."
    ),
    "n8n-builder": (
        "너는 JARVIS다. n8n 자동화 전문가.\n"
        "n8n 워크플로우 설계, 노드 구성, 자동화 아이디어를 도와줘.\n"
        "구체적인 노드명과 연결 방법 포함. 한국어."
    ),
    "command-center": (
        "너는 JARVIS다. 아이언맨 토니 스타크의 AI 비서.\n"
        "사용자의 어떤 요청이든 최선을 다해 답해.\n"
        "한국어로, 실용적으로, 마크다운 최소화."
    ),
}


# ── 메인 루프 ─────────────────────────────────────────────────────────────────

def init_timestamps():
    data = slack_get("conversations.history", {"channel": INBOX_ID, "limit": 1})
    msgs = data.get("messages", [])
    if msgs:
        _last_ts[INBOX_ID] = msgs[0]["ts"]
    print("✅ 타임스탬프 초기화 완료")


def main():
    print("🤖 JARVIS Slack Agent 시작")
    print(f"   inbox: agent-command-center ({INBOX_ID})")
    for name, ch_id in CHANNEL_IDS.items():
        if ch_id.startswith("REPLACE_ME"):
            print(f"   ⚠️  {name} 채널 ID 미설정")

    init_timestamps()

    while True:
        try:
            for msg in get_new_messages(INBOX_ID):
                text = msg.get("text", "").strip()
                ts   = msg.get("ts")
                if not text:
                    continue

                print(f"[inbox] {text[:80]}")
                route = classify(text)
                print(f"[inbox] → {route}")

                # 리서치 파이프라인은 별도 처리
                if route == "research":
                    run_research_pipeline(text, thread_ts=ts)
                    continue

                dest_id = CHANNEL_IDS.get(route, INBOX_ID)
                system  = AGENT_SYSTEMS.get(route, AGENT_SYSTEMS["command-center"])
                response = call_claude(system, text)

                # 원본 스레드에 라우팅 알림
                if route != "command-center":
                    slack_post(INBOX_ID, f"→ #{route} 처리 완료", thread_ts=ts)

                # 대상 채널에 결과 포스팅
                slack_post(dest_id, f"*[요청]* {text}\n\n*[응답]*\n{response}")
                print(f"[{route}] 전송 완료")
                speak(response)

        except Exception as e:
            print(f"[오류] {e}", file=sys.stderr)

        time.sleep(5)


if __name__ == "__main__":
    main()
