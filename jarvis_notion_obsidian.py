#!/usr/bin/env python3
"""
JARVIS — Notion + Obsidian 완전 통합
1. 대화 자동 Notion 기록
2. Notion 아이디어 → 사업기획서 자동 변환
3. 매일 아침 Notion 할일 브리핑
4. Notion ↔ Obsidian 양방향 동기화
"""

import os, re, json, subprocess
from pathlib import Path
from datetime import datetime, date

import requests

# ── 환경변수 로드 ──────────────────────────────────────────────────────────────
_env = Path.home() / ".env.jarvis"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.split("#")[0].strip()

NOTION_KEY     = os.environ.get("NOTION_API_KEY", "")
OBSIDIAN_VAULT = Path(os.environ.get("OBSIDIAN_VAULT",
                      str(Path.home() / "Documents/Obsidian Vault")))
ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_BIN     = Path.home() / "Library/Application Support/Claude/claude-code/2.1.156/claude.app/Contents/MacOS/claude"

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}


# ══════════════════════════════════════════════════════════════════════════════
# Notion API
# ══════════════════════════════════════════════════════════════════════════════

def notion_search(query: str = "", filter_type: str = None) -> list:
    body = {"page_size": 50}
    if query:
        body["query"] = query
    if filter_type:
        body["filter"] = {"value": filter_type, "property": "object"}
    r = requests.post("https://api.notion.com/v1/search",
                      headers=NOTION_HEADERS, json=body, timeout=10)
    return r.json().get("results", [])


def notion_get_title(page: dict) -> str:
    """페이지/DB에서 제목 추출."""
    if page.get("object") == "database":
        texts = page.get("title", [])
        return "".join(t.get("plain_text", "") for t in texts)
    props = page.get("properties", {})
    for p in props.values():
        if p.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in p.get("title", []))
    return page.get("id", "")[:8]


def notion_get_page_text(page_id: str) -> str:
    """페이지 블록 → 텍스트."""
    r = requests.get(f"https://api.notion.com/v1/blocks/{page_id}/children",
                     headers=NOTION_HEADERS, timeout=10)
    blocks = r.json().get("results", [])
    lines = []
    for b in blocks:
        btype = b.get("type", "")
        data  = b.get(btype, {})
        text  = "".join(t.get("plain_text", "") for t in data.get("rich_text", []))
        if btype == "heading_1":   lines.append(f"# {text}")
        elif btype == "heading_2": lines.append(f"## {text}")
        elif btype == "heading_3": lines.append(f"### {text}")
        elif btype == "bulleted_list_item": lines.append(f"- {text}")
        elif btype == "numbered_list_item": lines.append(f"1. {text}")
        elif btype == "to_do":
            checked = data.get("checked", False)
            lines.append(f"- [{'x' if checked else ' '}] {text}")
        elif btype == "paragraph" and text:
            lines.append(text)
    return "\n".join(lines)


def notion_create_page(title: str, content: str, parent_id: str, is_database: bool = False) -> dict:
    """Notion에 새 페이지 생성."""
    parent = {"database_id": parent_id} if is_database else {"page_id": parent_id}
    blocks = _md_to_blocks(content)
    body = {
        "parent": parent,
        "properties": {"title": {"title": [{"text": {"content": title}}]}},
        "children": blocks,
    }
    r = requests.post("https://api.notion.com/v1/pages",
                      headers=NOTION_HEADERS, json=body, timeout=15)
    return r.json()


def _md_to_blocks(md: str) -> list:
    blocks = []
    for line in md.split("\n")[:100]:
        if line.startswith("# "):
            blocks.append({"object":"block","type":"heading_1","heading_1":{"rich_text":[{"text":{"content":line[2:]}}]}})
        elif line.startswith("## "):
            blocks.append({"object":"block","type":"heading_2","heading_2":{"rich_text":[{"text":{"content":line[3:]}}]}})
        elif line.startswith("### "):
            blocks.append({"object":"block","type":"heading_3","heading_3":{"rich_text":[{"text":{"content":line[4:]}}]}})
        elif line.startswith("- [x] "):
            blocks.append({"object":"block","type":"to_do","to_do":{"rich_text":[{"text":{"content":line[6:]}}],"checked":True}})
        elif line.startswith("- [ ] "):
            blocks.append({"object":"block","type":"to_do","to_do":{"rich_text":[{"text":{"content":line[6:]}}],"checked":False}})
        elif line.startswith("- "):
            blocks.append({"object":"block","type":"bulleted_list_item","bulleted_list_item":{"rich_text":[{"text":{"content":line[2:]}}]}})
        elif line.startswith("> "):
            blocks.append({"object":"block","type":"quote","quote":{"rich_text":[{"text":{"content":line[2:]}}]}})
        elif line.strip() == "---":
            blocks.append({"object":"block","type":"divider","divider":{}})
        elif line.strip():
            blocks.append({"object":"block","type":"paragraph","paragraph":{"rich_text":[{"text":{"content":line}}]}})
    return blocks


# ══════════════════════════════════════════════════════════════════════════════
# 1. 대화 자동 Notion 기록
# ══════════════════════════════════════════════════════════════════════════════

# 오늘의 대화 로그 페이지 ID 캐시
_today_log_id: str = ""

def get_or_create_today_log() -> str:
    """오늘 날짜 대화 로그 페이지 가져오거나 생성."""
    global _today_log_id
    if _today_log_id:
        return _today_log_id

    today_str = date.today().strftime("%Y-%m-%d")
    title = f"JARVIS 대화 로그 {today_str}"

    # 기존 페이지 검색
    results = notion_search(title)
    for r in results:
        if notion_get_title(r) == title:
            _today_log_id = r["id"]
            return _today_log_id

    # 없으면 생성 — 접근 가능한 첫 페이지 하위에
    pages = notion_search(filter_type="page")
    if not pages:
        print("[Notion] 접근 가능한 페이지 없음 — Notion에서 Jarvis 봇 공유 필요")
        return ""

    parent_id = pages[0]["id"]
    content = f"# JARVIS 대화 로그\n날짜: {today_str}\n\n---\n"
    result = notion_create_page(title, content, parent_id)
    _today_log_id = result.get("id", "")
    return _today_log_id


def log_conversation(user_text: str, jarvis_text: str):
    """대화를 Notion 오늘 로그 페이지에 추가."""
    page_id = get_or_create_today_log()
    if not page_id:
        return

    now = datetime.now().strftime("%H:%M")
    blocks = [
        {"object":"block","type":"paragraph","paragraph":{"rich_text":[
            {"text":{"content":f"[{now}] 👤 {user_text}"},"annotations":{"bold":False}}]}},
        {"object":"block","type":"paragraph","paragraph":{"rich_text":[
            {"text":{"content":f"[{now}] 🤖 {jarvis_text}"},"annotations":{"color":"blue"}}]}},
        {"object":"block","type":"divider","divider":{}},
    ]
    requests.patch(
        f"https://api.notion.com/v1/blocks/{page_id}/children",
        headers=NOTION_HEADERS,
        json={"children": blocks},
        timeout=10,
    )
    print(f"[Notion] 대화 기록 완료")


# ══════════════════════════════════════════════════════════════════════════════
# 2. Notion 아이디어 → 사업기획서 자동 변환
# ══════════════════════════════════════════════════════════════════════════════

def idea_to_business_plan(page_id: str = None, keyword: str = None) -> str:
    """Notion 아이디어 페이지를 읽어 사업기획서 생성 후 Notion + Obsidian에 저장."""
    # 페이지 찾기
    if keyword:
        results = notion_search(keyword)
        page = next((r for r in results if r.get("object") == "page"), None)
        if not page:
            return f"'{keyword}' 관련 Notion 페이지를 찾지 못했습니다."
        page_id = page["id"]

    if not page_id:
        return "페이지 ID 또는 키워드가 필요합니다."

    # 아이디어 내용 읽기
    idea_text = notion_get_page_text(page_id)
    if not idea_text.strip():
        return "페이지 내용이 비어있습니다."

    # Claude로 사업기획서 생성
    prompt = f"""아래 아이디어를 바탕으로 전문적인 사업기획서를 작성해줘.

형식:
# 사업명
## 1. 사업 개요
## 2. 문제 정의 & 솔루션
## 3. 타겟 시장 & 고객
## 4. 수익 모델
## 5. 경쟁사 분석
## 6. 마케팅 전략
## 7. 실행 로드맵 (3개월/6개월/1년)
## 8. 필요 자원 & 예산
## 9. 리스크 및 대응 방안

한국어로, 구체적이고 실용적으로. 마크다운 형식으로.

[아이디어]
{idea_text}"""

    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    proc = subprocess.run(
        [str(CLAUDE_BIN), "-p", prompt],
        capture_output=True, text=True, env=env, timeout=120,
    )
    plan = proc.stdout.strip()
    if not plan:
        return "사업기획서 생성 실패"

    # Notion에 새 페이지로 저장
    title = f"사업기획서_{datetime.now().strftime('%Y%m%d')}"
    pages = notion_search(filter_type="page")
    if pages:
        notion_create_page(title, plan, pages[0]["id"])

    # Obsidian에도 저장
    obsidian_save(title, plan, folder="사업기획서")

    return f"사업기획서 '{title}'를 Notion과 Obsidian에 저장했습니다."


# ══════════════════════════════════════════════════════════════════════════════
# 3. 매일 아침 Notion 할일 브리핑
# ══════════════════════════════════════════════════════════════════════════════

def get_notion_todos() -> str:
    """Notion에서 미완료 할일 가져오기."""
    pages = notion_search(filter_type="page")
    todos = []

    for page in pages:
        page_id = page["id"]
        text = notion_get_page_text(page_id)
        # 미완료 체크박스 찾기
        for line in text.split("\n"):
            if line.startswith("- [ ]"):
                task = line[6:].strip()
                if task:
                    todos.append(f"• {task}")

    if not todos:
        return "Notion에 등록된 할일이 없습니다."

    return f"Notion 할일 {len(todos)}개:\n" + "\n".join(todos[:10])


def morning_briefing_with_notion() -> str:
    """Notion 할일 포함 아침 브리핑."""
    todos = get_notion_todos()
    today = datetime.now().strftime("%Y년 %m월 %d일")
    return f"오늘은 {today}입니다.\n\n{todos}"


# ══════════════════════════════════════════════════════════════════════════════
# 4. Obsidian 연동
# ══════════════════════════════════════════════════════════════════════════════

def obsidian_save(title: str, content: str, folder: str = "") -> Path:
    safe = re.sub(r'[<>:"/\\|?*]', '', title)[:80]
    target = OBSIDIAN_VAULT / folder if folder else OBSIDIAN_VAULT
    target.mkdir(parents=True, exist_ok=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    filepath = target / f"{safe}.md"
    filepath.write_text(
        f"---\ncreated: {now}\ntags: [jarvis]\n---\n\n# {title}\n\n{content}",
        encoding="utf-8"
    )
    print(f"[Obsidian] 저장: {filepath}")
    return filepath


def obsidian_search(keyword: str) -> list:
    results = []
    for md_file in OBSIDIAN_VAULT.rglob("*.md"):
        try:
            text = md_file.read_text(encoding="utf-8")
            if keyword.lower() in text.lower():
                lines = [l.strip() for l in text.split("\n") if keyword.lower() in l.lower()]
                results.append({"file": md_file.name, "path": str(md_file), "matches": lines[:3]})
        except Exception:
            continue
    return results


def sync_notion_to_obsidian() -> int:
    """Notion 전체 페이지 → Obsidian 동기화."""
    pages = notion_search(filter_type="page")
    count = 0
    for page in pages:
        title = notion_get_title(page)
        if not title:
            continue
        content = notion_get_page_text(page["id"])
        obsidian_save(title, content, folder="Notion Sync")
        count += 1
    print(f"[Sync] {count}개 동기화 완료")
    return count


# ══════════════════════════════════════════════════════════════════════════════
# 5. 음성 명령 처리 (jarvis_server.py에서 호출)
# ══════════════════════════════════════════════════════════════════════════════

def handle_note_command(text: str, extra_content: str = "") -> str:
    """음성 명령 감지 → Notion/Obsidian 동작."""
    t = text.lower()

    if any(w in t for w in ["할일", "투두", "todo", "오늘 할"]):
        return get_notion_todos()

    if any(w in t for w in ["사업기획서", "기획서", "business plan"]):
        keyword = text.replace("사업기획서", "").replace("기획서로", "").replace("만들어줘", "").strip()
        return idea_to_business_plan(keyword=keyword or None)

    if any(w in t for w in ["동기화", "sync", "싱크"]):
        count = sync_notion_to_obsidian()
        return f"Notion {count}개 페이지를 Obsidian에 동기화했습니다."

    if any(w in t for w in ["저장", "노트", "기록", "save", "note"]):
        content = extra_content or text
        title = datetime.now().strftime("%Y-%m-%d 메모")
        obsidian_save(title, content)
        return f"Obsidian에 저장했습니다: {title}"

    if any(w in t for w in ["찾아", "검색", "search"]):
        keyword = re.sub(r"찾아줘|검색해줘|에서|노션|옵시디언", "", text).strip()
        results = obsidian_search(keyword)
        if not results:
            return f"'{keyword}' 관련 노트를 찾지 못했습니다."
        return f"'{keyword}' 관련 노트 {len(results)}개:\n" + "\n".join(r["file"] for r in results[:5])

    return ""


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "test"

    if cmd == "test":
        print("=== JARVIS Notion + Obsidian 연동 테스트 ===")
        print(f"Notion API: {'✅' if NOTION_KEY else '❌'}")
        print(f"Obsidian Vault: {'✅' if OBSIDIAN_VAULT.exists() else '❌'} ({OBSIDIAN_VAULT})")
        pages = notion_search()
        print(f"Notion 접근 가능 페이지: {len(pages)}개")
        for p in pages:
            print(f"  - {notion_get_title(p)}")
        mds = list(OBSIDIAN_VAULT.rglob("*.md"))
        print(f"Obsidian 노트: {len(mds)}개")

    elif cmd == "todos":
        print(get_notion_todos())

    elif cmd == "sync":
        n = sync_notion_to_obsidian()
        print(f"동기화 완료: {n}개")

    elif cmd == "plan" and len(sys.argv) > 2:
        print(idea_to_business_plan(keyword=sys.argv[2]))

    elif cmd == "log" and len(sys.argv) > 3:
        log_conversation(sys.argv[2], sys.argv[3])
        print("기록 완료")
