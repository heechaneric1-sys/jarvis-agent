#!/usr/bin/env python3
"""
JARVIS — Notion ↔ Obsidian 연동
- Notion 페이지 → Obsidian .md 동기화
- Obsidian .md → Notion 페이지 생성
- 음성 명령으로 노트 저장/검색
"""

import os, re, json
from pathlib import Path
from datetime import datetime

import requests

# ── 환경변수 로드 ──────────────────────────────────────────────────────────────
_env = Path.home() / ".env.jarvis"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.split("#")[0].strip()

NOTION_KEY    = os.environ.get("NOTION_API_KEY", "")
OBSIDIAN_VAULT = Path(os.environ.get("OBSIDIAN_VAULT", str(Path.home() / "Documents/Obsidian Vault")))

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}


# ── Notion API ─────────────────────────────────────────────────────────────────

def notion_search(query: str = "", filter_type: str = None) -> list:
    """Notion 검색."""
    body = {"page_size": 20}
    if query:
        body["query"] = query
    if filter_type:
        body["filter"] = {"value": filter_type, "property": "object"}
    r = requests.post("https://api.notion.com/v1/search",
                      headers=NOTION_HEADERS, json=body, timeout=10)
    return r.json().get("results", [])


def notion_get_page_content(page_id: str) -> str:
    """Notion 페이지 블록을 마크다운으로 변환."""
    r = requests.get(f"https://api.notion.com/v1/blocks/{page_id}/children",
                     headers=NOTION_HEADERS, timeout=10)
    blocks = r.json().get("results", [])
    lines = []
    for block in blocks:
        btype = block.get("type", "")
        data  = block.get(btype, {})
        rich  = data.get("rich_text", [])
        text  = "".join(t.get("plain_text", "") for t in rich)

        if btype == "heading_1":   lines.append(f"# {text}")
        elif btype == "heading_2": lines.append(f"## {text}")
        elif btype == "heading_3": lines.append(f"### {text}")
        elif btype == "paragraph": lines.append(text)
        elif btype == "bulleted_list_item": lines.append(f"- {text}")
        elif btype == "numbered_list_item": lines.append(f"1. {text}")
        elif btype == "code":      lines.append(f"```\n{text}\n```")
        elif btype == "quote":     lines.append(f"> {text}")
        elif btype == "divider":   lines.append("---")
        elif btype == "to_do":
            checked = data.get("checked", False)
            lines.append(f"- [{'x' if checked else ' '}] {text}")
    return "\n".join(lines)


def notion_create_page(title: str, content: str, parent_page_id: str = None, database_id: str = None) -> dict:
    """Notion에 새 페이지 생성."""
    if parent_page_id:
        parent = {"type": "page_id", "page_id": parent_page_id}
    elif database_id:
        parent = {"type": "database_id", "database_id": database_id}
    else:
        return {"error": "parent_page_id 또는 database_id 필요"}

    # 마크다운을 Notion 블록으로 변환
    blocks = _md_to_blocks(content)

    body = {
        "parent": parent,
        "properties": {
            "title": {"title": [{"text": {"content": title}}]}
        },
        "children": blocks,
    }
    r = requests.post("https://api.notion.com/v1/pages",
                      headers=NOTION_HEADERS, json=body, timeout=15)
    return r.json()


def _md_to_blocks(md: str) -> list:
    """마크다운 → Notion 블록 리스트."""
    blocks = []
    for line in md.split("\n"):
        if line.startswith("# "):
            blocks.append({"object":"block","type":"heading_1","heading_1":{"rich_text":[{"text":{"content":line[2:]}}]}})
        elif line.startswith("## "):
            blocks.append({"object":"block","type":"heading_2","heading_2":{"rich_text":[{"text":{"content":line[3:]}}]}})
        elif line.startswith("### "):
            blocks.append({"object":"block","type":"heading_3","heading_3":{"rich_text":[{"text":{"content":line[4:]}}]}})
        elif line.startswith("- [ ] "):
            blocks.append({"object":"block","type":"to_do","to_do":{"rich_text":[{"text":{"content":line[6:]}}],"checked":False}})
        elif line.startswith("- [x] "):
            blocks.append({"object":"block","type":"to_do","to_do":{"rich_text":[{"text":{"content":line[6:]}}],"checked":True}})
        elif line.startswith("- "):
            blocks.append({"object":"block","type":"bulleted_list_item","bulleted_list_item":{"rich_text":[{"text":{"content":line[2:]}}]}})
        elif line.startswith("> "):
            blocks.append({"object":"block","type":"quote","quote":{"rich_text":[{"text":{"content":line[2:]}}]}})
        elif line.strip() == "---":
            blocks.append({"object":"block","type":"divider","divider":{}})
        elif line.strip():
            blocks.append({"object":"block","type":"paragraph","paragraph":{"rich_text":[{"text":{"content":line}}]}})
    return blocks[:100]   # Notion 블록 최대 100개


# ── Obsidian ──────────────────────────────────────────────────────────────────

def obsidian_save(title: str, content: str, folder: str = "") -> Path:
    """Obsidian vault에 마크다운 파일 저장."""
    safe_title = re.sub(r'[<>:"/\\|?*]', '', title)[:80]
    target_dir  = OBSIDIAN_VAULT / folder if folder else OBSIDIAN_VAULT
    target_dir.mkdir(parents=True, exist_ok=True)

    now      = datetime.now().strftime("%Y-%m-%d %H:%M")
    filepath = target_dir / f"{safe_title}.md"

    full_content = f"---\ncreated: {now}\ntags: [jarvis]\n---\n\n# {title}\n\n{content}"
    filepath.write_text(full_content, encoding="utf-8")
    print(f"[Obsidian] 저장: {filepath}")
    return filepath


def obsidian_search(keyword: str) -> list:
    """Obsidian vault에서 키워드 검색."""
    results = []
    for md_file in OBSIDIAN_VAULT.rglob("*.md"):
        try:
            text = md_file.read_text(encoding="utf-8")
            if keyword.lower() in text.lower():
                # 매칭된 줄 추출
                lines = [l.strip() for l in text.split("\n") if keyword.lower() in l.lower()]
                results.append({
                    "file": md_file.name,
                    "path": str(md_file),
                    "matches": lines[:3],
                })
        except Exception:
            continue
    return results


def obsidian_read(filename: str) -> str:
    """Obsidian 파일 읽기."""
    for md_file in OBSIDIAN_VAULT.rglob(f"*{filename}*"):
        return md_file.read_text(encoding="utf-8")
    return ""


# ── Notion → Obsidian 동기화 ──────────────────────────────────────────────────

def sync_notion_to_obsidian(folder: str = "Notion Sync") -> int:
    """Notion 페이지 전체 → Obsidian 동기화."""
    pages = notion_search(filter_type="page")
    count = 0
    for page in pages:
        # 제목 추출
        props = page.get("properties", {})
        title = ""
        for p in props.values():
            if p.get("type") == "title":
                title = "".join(t.get("plain_text","") for t in p.get("title",[]))
                break
        if not title:
            title = f"Notion_{page['id'][:8]}"

        # 내용 가져오기
        content = notion_get_page_content(page["id"])
        obsidian_save(title, content, folder=folder)
        count += 1
    print(f"[Sync] Notion → Obsidian: {count}개 동기화 완료")
    return count


# ── 음성 명령 처리 ────────────────────────────────────────────────────────────

def handle_note_command(text: str, extra_content: str = "") -> str:
    """
    음성 명령 → Notion/Obsidian 동작
    반환값: 사용자에게 읽어줄 응답 텍스트
    """
    text_lower = text.lower()

    # ── 저장 명령 ──
    if any(w in text_lower for w in ["저장", "노트", "기록", "save", "note"]):
        title = datetime.now().strftime("%Y-%m-%d 메모")
        content = extra_content or text
        obsidian_save(title, content)
        return f"옵시디언에 저장했습니다. 파일명: {title}"

    # ── 검색 명령 ──
    if any(w in text_lower for w in ["찾아", "검색", "search", "find"]):
        keyword = text.replace("찾아줘","").replace("검색해줘","").replace("찾아","").strip()
        results = obsidian_search(keyword)
        if not results:
            return f"옵시디언에서 '{keyword}' 관련 노트를 찾지 못했습니다."
        summary = ", ".join(r["file"] for r in results[:3])
        return f"'{keyword}' 관련 노트 {len(results)}개 찾았습니다. {summary}"

    # ── 노션 동기화 ──
    if any(w in text_lower for w in ["동기화", "sync", "싱크"]):
        count = sync_notion_to_obsidian()
        return f"노션 페이지 {count}개를 옵시디언에 동기화했습니다."

    return ""


if __name__ == "__main__":
    import sys
    cmd = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else ""

    if cmd == "sync":
        sync_notion_to_obsidian()
    elif cmd == "search" and len(sys.argv) > 2:
        results = obsidian_search(sys.argv[2])
        for r in results:
            print(f"📄 {r['file']}")
            for m in r["matches"]:
                print(f"   {m}")
    elif cmd == "test":
        print("Notion 연결:", "OK" if NOTION_KEY else "NO KEY")
        print("Obsidian vault:", OBSIDIAN_VAULT, "→", "존재" if OBSIDIAN_VAULT.exists() else "없음")
        pages = notion_search()
        print(f"Notion 접근 가능 페이지: {len(pages)}개")
        mds = list(OBSIDIAN_VAULT.rglob("*.md"))
        print(f"Obsidian 노트: {len(mds)}개")
    else:
        print("사용법: python3 jarvis_notion_obsidian.py [sync|search <keyword>|test]")
