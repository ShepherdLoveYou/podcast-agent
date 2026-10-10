"""把一次生成的产物整理成 episodes/NNN/，供 new-episode 工作流开 PR。

读 OUT_DIR（默认 out）里的 episode.m4a、notes.json、transcript.txt，
写 EPISODES_DIR（默认 episodes）/NNN/ 下的 audio.mp3、notes.md、meta.json、transcript.txt，
并把 PR 正文写到 OUT_DIR/pr_body.md。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path


def transcode(src: Path, dst: Path) -> None:
    """转成 96 kbps 的 MP3，并把响度统一到播客常用的 -16 LUFS。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", str(src), "-vn",
         "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-ar", "44100", "-ac", "2",
         "-c:a", "libmp3lame", "-b:a", "96k", str(dst)],
        check=True,
    )


def probe_duration(path: Path) -> int:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout
    return round(float(out))


def next_number(episodes_dir: Path) -> int:
    taken = [int(d.name) for d in episodes_dir.iterdir() if d.is_dir() and d.name.isdigit()]
    return max(taken, default=0) + 1


def render_notes(title: str, notes: dict) -> str:
    parts = [f"# {title}"]
    if notes.get("description"):
        parts.append(f"**本期看点**：{notes['description']}")
    if notes.get("highlights"):
        parts.append("## 本期要点")
        parts.append("\n".join(f"- {h}" for h in notes["highlights"]))
    return "\n\n".join(parts) + "\n"


def render_pr_body(number: str, title: str, notes: dict, duration: int, audio_bytes: int) -> str:
    parts = [
        f"## {title}",
        f"合并这个 PR 就会发布这一期。合并前可以直接改 `episodes/{number}/notes.md` 里的标题和简介。",
        f"- 时长：{duration // 60} 分 {duration % 60} 秒，{audio_bytes / 1e6:.1f} MB\n"
        f"- 试听：在 Files changed 里打开 `episodes/{number}/audio.mp3` 下载\n"
        f"- 全文：`episodes/{number}/transcript.txt`（NotebookLM 的自动转写，有错字）",
    ]
    if notes.get("fallback_reason"):
        parts.append(f"**注意：没能自动写出标题和简介，请手动补上。** 原因：{notes['fallback_reason']}")
    if notes.get("description"):
        parts.append(f"### 简介\n\n{notes['description']}")
    if notes.get("highlights"):
        parts.append("### 本期要点\n\n" + "\n".join(f"- {h}" for h in notes["highlights"]))
    if notes.get("qa"):
        parts.append(f"### 质检备注（AI 对照来源生成，仅供参考）\n\n{notes['qa']}")
    return "\n\n".join(parts) + "\n"


def main() -> None:
    out_dir = Path(os.environ.get("OUT_DIR") or "out")
    episodes_dir = Path(os.environ.get("EPISODES_DIR") or "episodes")
    episodes_dir.mkdir(parents=True, exist_ok=True)

    notes = json.loads((out_dir / "notes.json").read_text(encoding="utf-8"))

    n = next_number(episodes_dir)
    number = f"{n:03d}"
    title = f"第{n}期：{notes['title']}".replace("\n", " ")
    episode_dir = episodes_dir / number

    audio = episode_dir / "audio.mp3"
    transcode(out_dir / "episode.m4a", audio)
    duration = probe_duration(audio)

    (episode_dir / "notes.md").write_text(render_notes(title, notes), encoding="utf-8")
    meta = {
        "guid": str(uuid.uuid4()),
        "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duration_seconds": duration,
    }
    (episode_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if (out_dir / "transcript.txt").is_file():
        shutil.copyfile(out_dir / "transcript.txt", episode_dir / "transcript.txt")

    (out_dir / "pr_body.md").write_text(
        render_pr_body(number, title, notes, duration, audio.stat().st_size),
        encoding="utf-8",
    )
    print(f"已整理：{episode_dir}（{title}）")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
            fh.write(f"number={number}\ntitle={title}\n")


if __name__ == "__main__":
    main()
