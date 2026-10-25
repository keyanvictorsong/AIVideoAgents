"""Editable publishing jobs and durable duplicate-submission protection."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def fingerprint(video: Path) -> dict:
    digest = hashlib.sha256()
    with video.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    stat = video.stat()
    return {"sha256": digest.hexdigest(), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def metadata(title: str, description: str | None = None, hashtags: str | list | None = None) -> dict:
    title = title.strip()
    if not title or "\n" in title or utf16_length(title) > 30:
        raise ValueError("作品标题须为 1–30 字；请用 --title 指定，不能包含换行。")
    if hashtags is None:
        hashtags = []
    elif isinstance(hashtags, str):
        hashtags = re.split(r"[,，\s]+", hashtags.strip())
    tags: list[str] = []
    for tag in hashtags:
        tag = str(tag).strip().lstrip("#")
        if not tag:
            continue
        if not re.fullmatch(r"[\w\u3400-\u9fff]+", tag):
            raise ValueError(f"话题含不支持的空格或标点：{tag}")
        if tag not in tags:
            tags.append(tag)
    if description is None:
        description = ""
    text = description.strip()
    caption = text + ("\n" if text and tags else "") + " ".join(f"#{t}" for t in tags)
    if utf16_length(caption) > 1000:
        raise ValueError("简介加话题超过 1000 字，请缩短。")
    return {"title": title, "description": text, "hashtags": tags}


def load_job(path: Path, *, verify_video: bool = True) -> dict:
    job = json.loads(path.read_text(encoding="utf-8"))
    if job.get("schema_version") != 1:
        raise ValueError("不支持的任务文件版本。")
    job.update(metadata(job["title"], job["description"], job["hashtags"]))
    video = Path(job["video"])
    if not video.is_file():
        raise ValueError(f"视频不存在：{video}")
    if verify_video and fingerprint(video)["sha256"] != job["source"]["sha256"]:
        raise ValueError("原视频已更换，请重新 prepare，避免上传错误文件。")
    for kind in ("vertical", "horizontal"):
        cover = Path(job["covers"][kind])
        if not cover.is_file():
            raise ValueError(f"缺少 {kind} 封面：{cover}")
    if not isinstance(job.get("collection"), dict):
        raise ValueError("任务文件缺少 collection 配置。")
    return job


def event(job: dict, state: str, details: dict | None = None) -> None:
    job["state"] = state
    job["updated_at"] = now()
    job.setdefault("history", []).append({"time": now(), "state": state, "details": details or {}})


class DuplicateSubmission(ValueError):
    pass


@contextlib.contextmanager
def submission_guard(profile: Path, job: dict) -> Iterator[dict]:
    """Lock the profile and remember all attempted videos across output folders."""
    profile.parent.mkdir(parents=True, exist_ok=True)
    ledger_path = profile.parent / f"{profile.name}-publish-ledger.json"
    lock_path = profile.parent / f"{profile.name}.agent.lock"
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("另一个发布任务正在使用此浏览器，请先结束它。") from exc
        ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {}
        key = job["source"]["sha256"]
        attempt = Path(job["output_dir"]) / "publish-attempt.json"
        if key in ledger or attempt.exists() or job.get("state") in {"publish_attempted", "published"}:
            raise DuplicateSubmission(
                "这条视频已经发布或曾经提交，已拦截重复发布。请先在抖音作品管理确认结果。"
                f"\n本地记录：{ledger_path}\n任务记录：{attempt}"
            )
        result: dict = {}
        try:
            yield result
        finally:
            # The browser creates the sentinel *before* the irreversible click.
            # A timeout after that click remains ambiguous and must never auto-retry.
            if attempt.exists() or result.get("status") == "published":
                ledger[key] = {
                    "job_id": job["job_id"], "title": job["title"], "time": now(),
                    "status": "published" if result.get("status") == "published" else "publish_attempted",
                    "output_dir": job["output_dir"],
                }
                write_json(ledger_path, ledger)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
