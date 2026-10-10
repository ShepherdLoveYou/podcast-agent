"""建笔记本、加来源、生成并下载一期音频。由 new-episode 工作流调用。

输入走环境变量：
  SOURCES   来源链接，空白分隔
  FOCUS     本期着重的方面，可空
  FORMAT    deep-dive | brief | critique | debate
  LENGTH    default | short | long
  LANGUAGE  默认 zh_Hans
  NOTEBOOK  已有笔记本 ID；给了就只下载，不重新生成
  OUT_DIR   默认 out
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SOURCE_TIMEOUT_S = 300
GENERATE_TIMEOUT_S = 2400


def nblm(*args: str) -> dict:
    """运行 notebooklm 子命令，返回它的 --json 输出。"""
    proc = subprocess.run(["notebooklm", *args, "--json"], capture_output=True, text=True)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    label = " ".join(args[:2])
    if proc.returncode != 0:
        sys.exit(f"notebooklm {label} 失败（退出码 {proc.returncode}）：{proc.stdout.strip()}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        sys.exit(f"notebooklm {label} 的输出不是 JSON：{proc.stdout[:500]}")


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
        sys.exit(f"音频配额不足，不生成。各窗口重置时间：{resets}")
    print(f"音频配额充足，这次预计消耗 {audio.get('estimated_cost_percent')}%")


def generate(sources: list[str], focus: str, fmt: str, length: str, language: str) -> dict:
    ensure_audio_quota()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    notebook_id = nblm("create", f"episode-{stamp}")["notebook"]["id"]
    # 先把 ID 打出来：生成成功但后面失败时，可以用它只重新下载，不再消耗配额
    print(f"笔记本 ID：{notebook_id}")

    added = []
    for url in sources:
        source = nblm("source", "add", url, "-n", notebook_id)["source"]
        nblm("source", "wait", source["id"], "-n", notebook_id, "--timeout", str(SOURCE_TIMEOUT_S))
        print(f"来源就绪：{source.get('title')}")
        added.append(source)

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
    print(f"生成结果：{json.dumps(result, ensure_ascii=False)}")

    return {"notebook_id": notebook_id, "sources": added}


def main() -> None:
    sources = os.environ.get("SOURCES", "").split()
    focus = os.environ.get("FOCUS", "").strip()
    fmt = os.environ.get("FORMAT") or "deep-dive"
    length = os.environ.get("LENGTH") or "default"
    language = os.environ.get("LANGUAGE") or "zh_Hans"
    notebook_id = os.environ.get("NOTEBOOK", "").strip()
    out_dir = Path(os.environ.get("OUT_DIR") or "out")

    meta: dict = {"format": fmt, "length": length, "language": language, "focus": focus}
    if notebook_id:
        print(f"使用已有笔记本 {notebook_id}，只下载不生成")
        meta["notebook_id"] = notebook_id
    else:
        if not sources:
            sys.exit("SOURCES 为空：至少给一个来源链接")
        not_links = [s for s in sources if not s.startswith(("http://", "https://"))]
        if not_links:
            sys.exit(f"这些不是链接：{not_links}")
        meta.update(generate(sources, focus, fmt, length, language))
        notebook_id = meta["notebook_id"]

    out_dir.mkdir(parents=True, exist_ok=True)
    audio_path = out_dir / "episode.m4a"
    nblm("download", "audio", str(audio_path), "-n", notebook_id, "--force")
    if not audio_path.is_file() or audio_path.stat().st_size == 0:
        sys.exit("下载结果为空")
    print(f"已下载：{audio_path}（{audio_path.stat().st_size / 1e6:.1f} MB）")

    meta["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    (out_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
