"""建笔记本、加来源、生成并下载一期音频，再让 NotebookLM 按成品写发布信息。

由 new-episode 工作流调用。输入是工作流表单里的这几项（本机运行时用同名的大写环境变量）：
  sources   来源链接，空白分隔
  focus     本期着重的方面，可空
  format    deep-dive | brief | critique | debate
  length    default | short | long
  notebook  已有笔记本 ID；给了就不重新生成，只下载并写发布信息
另有环境变量 LANGUAGE（默认 zh_Hans）和 OUT_DIR（默认 out）。

输出到 OUT_DIR：episode.m4a、meta.json、notes.json、transcript.txt。
来源的标题和链接不写进任何输出，也不出现在日志里。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE_TIMEOUT_S = 300
GENERATE_TIMEOUT_S = 2400
AUDIO_SOURCE_TIMEOUT_S = 600

NOTES_PROMPT = """只根据"{audio}"这段播客音频里两位主持人实际说的内容，为这一期节目写发布信息。

不要提到原始来源的标题、作者、频道或链接。只输出一个 JSON 对象，不要任何其他文字，不要引用标记，格式如下：
{{"title": "不超过 30 个字的中文标题，不要带期数", "description": "150 到 250 字的中文简介，一段话，说明这期聊了什么、对谁有用", "highlights": ["3 到 5 条本期要点，每条不超过 30 个字"]}}
"""

QA_PROMPT = """对照这个笔记本里除"{audio}"以外的原始来源，检查"{audio}"这段播客音频：有没有与原始来源明显不符、或者原始来源里完全没有依据的说法？

最多列 5 条，每条一句话，用"- "开头；如果没有发现，只回答"未发现明显问题"。不要引用标记，也不要写出原始来源的标题、作者或链接，提到时只说"来源"。
"""

# 成品里不该出现的、会暴露内容另有出处的词。转写是繁体，所以两种写法都查
SOURCE_WORDS = re.compile(
    "资料|資料|来源|來源|素材|文档|文檔|文章|报告|報告|讲座|講座|作者|原文|文字记录|文字記錄"
)
CITATION = re.compile(r"\s*\[\d+(?:\s*[-,，]\s*\d+)*\]")


class NblmError(RuntimeError):
    pass


def mask(value: str) -> None:
    """让 GitHub Actions 在公开日志里把这个值打码。"""
    if value and os.environ.get("GITHUB_ACTIONS"):
        print(f"::add-mask::{value}", flush=True)


def read_inputs() -> dict[str, str]:
    """工作流表单的输入。

    CI 里从事件文件读，不走环境变量：步骤的环境变量会原样出现在公开日志里。
    """
    inputs: dict = {}
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_path and Path(event_path).is_file():
        inputs = json.loads(Path(event_path).read_text(encoding="utf-8")).get("inputs") or {}
    names = ("sources", "focus", "format", "length", "notebook")
    return {name: (os.environ.get(name.upper()) or inputs.get(name) or "").strip() for name in names}


def nblm(*args: str) -> dict:
    """运行 notebooklm 子命令，返回它的 --json 输出。"""
    proc = subprocess.run(["notebooklm", *args, "--json"], capture_output=True, text=True)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    label = " ".join(args[:2])
    if proc.returncode != 0:
        raise NblmError(f"notebooklm {label} 失败（退出码 {proc.returncode}）：{proc.stdout.strip()}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise NblmError(f"notebooklm {label} 的输出不是 JSON：{proc.stdout[:500]}") from None


def compose_focus(episode_focus: str) -> str:
    """节目固定的风格说明（show.toml 的 house_style）加上这一期自己的着重点。"""
    show = tomllib.loads((ROOT / "show.toml").read_text(encoding="utf-8"))
    parts = [show.get("house_style", "").strip()]
    if episode_focus.strip():
        parts.append(f"【这一期的着重点】\n{episode_focus.strip()}")
    return "\n\n".join(part for part in parts if part)


def ensure_audio_quota() -> None:
    usage = nblm("usage")
    if usage.get("status") != "ready":
        print(f"用量表不可用（{usage.get('status')}），跳过余量检查")
        return
    audio = next((a for a in usage["actions"] if a.get("kind") == "audio_overview"), None)
    if audio is None:
        print("用量表里没有音频这一项，跳过余量检查")
        return
    if audio.get("has_sufficient_quota") is False:
        resets = {w["kind"]: w["resets_at"] for w in usage["windows"]}
        raise NblmError(f"音频配额不足，不生成。各窗口重置时间：{resets}")
    print(f"音频配额充足，这次预计消耗 {audio.get('estimated_cost_percent')}%")


def generate(sources: list[str], focus: str, fmt: str, length: str, language: str) -> str:
    """生成一期音频，返回笔记本 ID。"""
    ensure_audio_quota()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    notebook_id = nblm("create", f"episode-{stamp}")["notebook"]["id"]
    # 先把 ID 打出来：生成成功但后面失败时，可以用它重跑而不再消耗配额
    print(f"笔记本 ID：{notebook_id}")

    for index, url in enumerate(sources, 1):
        mask(url)
        source = nblm("source", "add", url, "-n", notebook_id)["source"]
        mask(source.get("title") or "")
        nblm("source", "wait", source["id"], "-n", notebook_id, "--timeout", str(SOURCE_TIMEOUT_S))
        print(f"第 {index} 个来源就绪")

    args = ["generate", "audio", "-n", notebook_id, "--format", fmt, "--length", length]
    args += ["--language", language, "--wait", "--timeout", str(GENERATE_TIMEOUT_S)]
    with tempfile.TemporaryDirectory() as tmp:
        if focus:
            # 走文件而不是位置参数，免得以 "-" 开头的文字被当成选项
            prompt_file = Path(tmp) / "focus.txt"
            prompt_file.write_text(focus, encoding="utf-8")
            args += ["--prompt-file", str(prompt_file)]
        print("开始生成音频，通常要十几分钟……")
        result = nblm(*args)
    # 只打状态：完整结果里带音频的直链，没必要留在公开日志里
    print(f"生成完成：状态 {result.get('status')}，任务 {result.get('task_id')}")
    return notebook_id


def ask(notebook_id: str, prompt: str, source_id: str | None = None) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        prompt_file = Path(tmp) / "prompt.txt"
        prompt_file.write_text(prompt, encoding="utf-8")
        args = ["ask", "--prompt-file", str(prompt_file), "-n", notebook_id]
        if source_id:
            args += ["-s", source_id]
        return CITATION.sub("", nblm(*args)["answer"]).strip()


def parse_json_object(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"回答里没有 JSON：{text[:200]}")
    return json.loads(text[start : end + 1])


def tidy_transcript(text: str) -> str:
    """NotebookLM 的转写每个字之间有空格、段落之间是两个空格，整理成正常文本。"""
    text = text.strip().replace("  ", "\n")
    return re.sub(r"(?<=[^\x00-\x7f]) (?=[^\x00-\x7f])", "", text)


def find_source_mentions(transcript: str, limit: int = 8) -> list[str]:
    """在转写里找暴露出处的词，带上前后文，供人在 PR 里判断。"""
    snippets = []
    for match in SOURCE_WORDS.finditer(transcript):
        start, end = max(match.start() - 14, 0), min(match.end() + 14, len(transcript))
        snippets.append(transcript[start:end].replace("\n", " "))
    return snippets[:limit] + ([f"……共 {len(snippets)} 处"] if len(snippets) > limit else [])


def write_notes(notebook_id: str, audio_path: Path, out_dir: Path) -> dict:
    """把成品音频加回笔记本，让 NotebookLM 按实际内容写标题、简介和质检备注。"""
    sources = nblm("source", "list", "-n", notebook_id)["sources"]
    audio = next((s for s in sources if s["title"] == audio_path.name), None)
    if audio is None:
        added = nblm("source", "add", str(audio_path), "-n", notebook_id, "--timeout", "300")
        audio = added["source"]
    audio_id = audio["id"]
    nblm("source", "wait", audio_id, "-n", notebook_id, "--timeout", str(AUDIO_SOURCE_TIMEOUT_S))

    transcript = tidy_transcript(nblm("source", "fulltext", audio_id, "-n", notebook_id).get("content", ""))
    (out_dir / "transcript.txt").write_text(transcript + "\n", encoding="utf-8")

    answer = ask(notebook_id, NOTES_PROMPT.format(audio=audio_path.name), source_id=audio_id)
    notes = parse_json_object(answer)
    qa = ask(notebook_id, QA_PROMPT.format(audio=audio_path.name))
    return {
        "title": str(notes["title"]).strip(),
        "description": str(notes["description"]).strip(),
        "highlights": [str(h).strip() for h in notes.get("highlights", [])],
        "qa": qa,
        "source_mentions": find_source_mentions(transcript),
    }


def main() -> None:
    inputs = read_inputs()
    sources = inputs["sources"].split()
    focus = compose_focus(inputs["focus"])
    fmt = inputs["format"] or "deep-dive"
    length = inputs["length"] or "default"
    language = os.environ.get("LANGUAGE") or "zh_Hans"
    notebook_id = inputs["notebook"]
    out_dir = Path(os.environ.get("OUT_DIR") or "out")
    audio_path = out_dir / "episode.m4a"

    if not notebook_id:
        if not sources:
            sys.exit("SOURCES 为空：至少给一个来源链接")
        not_links = [s for s in sources if not s.startswith(("http://", "https://"))]
        if not_links:
            sys.exit(f"这些不是链接：{not_links}")

    try:
        if notebook_id:
            print(f"使用已有笔记本 {notebook_id}，不重新生成")
        else:
            notebook_id = generate(sources, focus, fmt, length, language)

        out_dir.mkdir(parents=True, exist_ok=True)
        nblm("download", "audio", str(audio_path), "-n", notebook_id, "--force")
        if not audio_path.is_file() or audio_path.stat().st_size == 0:
            raise NblmError("下载结果为空")
        print(f"已下载：{audio_path}（{audio_path.stat().st_size / 1e6:.1f} MB）")
    except NblmError as err:
        sys.exit(str(err))

    meta = {
        "format": fmt,
        "length": length,
        "language": language,
        "focus": focus,
        "notebook_id": notebook_id,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (out_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    try:
        notes = write_notes(notebook_id, audio_path, out_dir)
        print(f"发布信息已写好：{notes['title']}")
    except (NblmError, ValueError, KeyError) as err:
        # 音频已经到手，写不出发布信息也不让这次运行失败，留给人在 PR 里补
        print(f"::warning::没能自动写出发布信息：{err}")
        notes = {"title": "未命名", "description": "", "highlights": [], "qa": "", "fallback_reason": str(err)}
    (out_dir / "notes.json").write_text(
        json.dumps(notes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
