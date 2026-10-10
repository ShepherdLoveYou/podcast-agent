"""把 show.toml 和 episodes/ 生成 Pages 站点：feed.xml、音频、封面和一个简单首页。

用法：python scripts/build_site.py [输出目录，默认 _site] [--console 策划台输出路径]

策划台（console/index.html）里会写入 GEMINI_API_KEY，所以它不放进输出目录，
而是写到 --console 指定的路径，由发布工作流加密之后再放进站点。
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import sys
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NS = {
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
    "atom": "http://www.w3.org/2005/Atom",
    "content": "http://purl.org/rss/1.0/modules/content/",
}
# 策划台依次尝试的 Gemini 模型，前一个不可用时换下一个
CONSOLE_MODELS = ["gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-flash-latest"]
INLINE_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
INLINE_BOLD = re.compile(r"\*\*(.+?)\*\*")


@dataclass
class Episode:
    number: str
    title: str
    body_html: str
    guid: str
    published_at: datetime
    duration_seconds: int
    audio: Path


def inline_html(text: str) -> str:
    text = html.escape(text)
    text = INLINE_LINK.sub(lambda m: f'<a href="{m[2]}">{m[1]}</a>', text)
    return INLINE_BOLD.sub(r"<strong>\1</strong>", text)


def notes_html(body: str) -> str:
    """notes.md 正文 → 播客客户端能显示的简单 HTML（段落、列表、小标题、加粗、链接）。"""
    blocks = []
    for block in re.split(r"\n\s*\n", body.strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        if lines[0].startswith("#"):
            blocks.append(f"<p><strong>{inline_html(lines[0].lstrip('#').strip())}</strong></p>")
            lines = lines[1:]
            if not lines:
                continue
        if all(line.startswith("- ") for line in lines):
            items = "".join(f"<li>{inline_html(line[2:])}</li>" for line in lines)
            blocks.append(f"<ul>{items}</ul>")
        else:
            blocks.append("<p>" + "<br/>".join(inline_html(line) for line in lines) + "</p>")
    return "\n".join(blocks)


def load_episodes(episodes_dir: Path) -> list[Episode]:
    episodes = []
    for episode_dir in sorted(d for d in episodes_dir.iterdir() if d.is_dir()):
        notes = (episode_dir / "notes.md").read_text(encoding="utf-8")
        first, _, body = notes.partition("\n")
        if not first.startswith("# "):
            sys.exit(f"{episode_dir}/notes.md 的第一行必须是 '# 标题'")
        meta = json.loads((episode_dir / "meta.json").read_text(encoding="utf-8"))
        audio = episode_dir / "audio.mp3"
        if not audio.is_file():
            sys.exit(f"缺少 {audio}")
        episodes.append(Episode(
            number=episode_dir.name,
            title=first[2:].strip(),
            body_html=notes_html(body),
            guid=meta["guid"],
            published_at=datetime.fromisoformat(meta["published_at"]),
            duration_seconds=int(meta["duration_seconds"]),
            audio=audio,
        ))
    # 最新的排最前
    return sorted(episodes, key=lambda e: e.published_at, reverse=True)


def rfc822(moment: datetime) -> str:
    return format_datetime(moment.astimezone(timezone.utc), usegmt=True)


def hms(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def add(parent: ET.Element, tag: str, text: str | None = None, /, **attrs: str) -> ET.Element:
    # text 只能按位置传：<itunes:category text="..."> 需要一个也叫 text 的属性
    if ":" in tag:
        prefix, name = tag.split(":")
        tag = f"{{{NS[prefix]}}}{name}"
    element = ET.SubElement(parent, tag, attrs)
    element.text = text
    return element


def build_feed(show: dict, episodes: list[Episode]) -> ET.ElementTree:
    site = show["site_url"].rstrip("/")
    cover_url = f"{site}/{show['cover']}"
    explicit = "true" if show.get("explicit") else "false"

    for prefix, uri in NS.items():
        ET.register_namespace(prefix, uri)
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    add(channel, "title", show["title"])
    add(channel, "link", f"{site}/")
    add(channel, "description", show["description"])
    add(channel, "language", show["language"])
    add(channel, "copyright", show["author"])
    add(channel, "generator", "podcast-agent")
    if episodes:
        add(channel, "lastBuildDate", rfc822(episodes[0].published_at))
    add(channel, "atom:link", href=f"{site}/feed.xml", rel="self", type="application/rss+xml")
    add(channel, "itunes:author", show["author"])
    add(channel, "itunes:summary", show["description"])
    add(channel, "itunes:type", "episodic")
    owner = add(channel, "itunes:owner")
    add(owner, "itunes:name", show["author"])
    if show.get("owner_email"):
        add(owner, "itunes:email", show["owner_email"])
    add(channel, "itunes:explicit", explicit)
    category = add(channel, "itunes:category", text=show["category"])
    if show.get("subcategory"):
        add(category, "itunes:category", text=show["subcategory"])
    add(channel, "itunes:image", href=cover_url)

    for episode in episodes:
        item = ET.SubElement(channel, "item")
        add(item, "title", episode.title)
        add(item, "description", episode.body_html)
        add(item, "content:encoded", episode.body_html)
        add(item, "link", f"{site}/#{episode.number}")
        add(item, "guid", episode.guid, isPermaLink="false")
        add(item, "pubDate", rfc822(episode.published_at))
        add(item, "enclosure", url=f"{site}/audio/{episode.number}.mp3",
            length=str(episode.audio.stat().st_size), type="audio/mpeg")
        add(item, "itunes:author", show["author"])
        add(item, "itunes:duration", hms(episode.duration_seconds))
        add(item, "itunes:explicit", explicit)
        # 不写 <itunes:episode>：节目是 episodic 类型，校验工具要求这类节目不带期号
        add(item, "itunes:episodeType", "full")
        add(item, "itunes:image", href=cover_url)

    tree = ET.ElementTree(rss)
    ET.indent(tree)
    return tree


def render_index(show: dict, episodes: list[Episode]) -> str:
    items = "\n".join(
        f'<article id="{e.number}"><h2>{html.escape(e.title)}</h2>'
        f"<time>{e.published_at:%Y-%m-%d}</time>"
        f'<audio controls preload="none" src="audio/{e.number}.mp3"></audio>'
        f"{e.body_html}</article>"
        for e in episodes
    )
    return f"""<!doctype html>
<html lang="{html.escape(show['language'])}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(show['title'])}</title>
<link rel="alternate" type="application/rss+xml" title="{html.escape(show['title'])}" href="feed.xml">
<style>
:root {{ color-scheme: light dark; }}
body {{ font: 16px/1.7 system-ui, sans-serif; max-width: 42rem; margin: 0 auto; padding: 2rem 1rem; }}
header img {{ width: 8rem; border-radius: .5rem; }}
article {{ border-top: 1px solid #8884; padding: 1.5rem 0; }}
h2 {{ font-size: 1.15rem; margin: 0; }}
time {{ opacity: .6; font-size: .85rem; }}
audio {{ display: block; width: 100%; margin: .75rem 0; }}
</style>
</head>
<body>
<header>
<img src="{html.escape(show['cover'])}" alt="">
<h1>{html.escape(show['title'])}</h1>
<p>{html.escape(show['description'])}</p>
<p><a href="feed.xml">RSS</a></p>
</header>
{items}
</body>
</html>
"""


def build_console(show: dict, episodes: list[Episode], path: Path) -> bool:
    """生成未加密的策划台页面。没有 GEMINI_API_KEY 时不生成，返回 False。"""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        return False
    config = {
        "geminiKey": key,
        "models": [m for m in [os.environ.get("GEMINI_MODEL")] if m] + CONSOLE_MODELS,
        "repo": os.environ.get("GITHUB_REPOSITORY") or show["repo"],
        "workflow": "new-episode.yml",
        "showTitle": show["title"],
        "houseStyle": show.get("house_style", "").strip(),
        "episodes": [{"number": e.number, "title": e.title} for e in episodes],
    }
    # "</" 会提前结束 <script>，转义掉
    payload = json.dumps(config, ensure_ascii=False).replace("</", "<\\/")
    template = (ROOT / "console" / "index.html").read_text(encoding="utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(template.replace("__CONFIG__", payload), encoding="utf-8")
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("out_dir", nargs="?", default=ROOT / "_site", type=Path)
    parser.add_argument("--console", type=Path, help="未加密的策划台写到哪里")
    args = parser.parse_args()
    out_dir = args.out_dir
    show = tomllib.loads((ROOT / "show.toml").read_text(encoding="utf-8"))
    episodes = load_episodes(ROOT / "episodes")

    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "audio").mkdir(parents=True)
    shutil.copyfile(ROOT / show["cover"], out_dir / show["cover"])
    for episode in episodes:
        shutil.copyfile(episode.audio, out_dir / "audio" / f"{episode.number}.mp3")
    build_feed(show, episodes).write(out_dir / "feed.xml", encoding="utf-8", xml_declaration=True)
    (out_dir / "index.html").write_text(render_index(show, episodes), encoding="utf-8")
    print(f"已生成 {out_dir}：{len(episodes)} 期")
    if args.console:
        built = build_console(show, episodes, args.console)
        print("已生成策划台（未加密）" if built else "没有 GEMINI_API_KEY，跳过策划台")


if __name__ == "__main__":
    main()
