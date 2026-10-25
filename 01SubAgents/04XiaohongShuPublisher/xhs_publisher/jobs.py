"""Validated local manifests and durable protection against duplicate handoffs."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

PLATFORM = "xiaohongshu"
TITLE_LIMIT = 20
BLOCKED_STATES = frozenset({
    "preparation_interrupted", "awaiting_user_publish", "filled",
    "manual_publication_unverified", "publish_attempted", "published",
})
ATTEMPT_FILENAME = "manual-handoff.json"
REPREPARING = "repreparing_after_user_confirmed_not_published"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path: Path, value: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def fingerprint(path: Path) -> dict:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    stat = Path(path).stat()
    return {"sha256": digest.hexdigest(), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def metadata(title: str, description: str = "", tags: str | list | None = None) -> dict:
    if not isinstance(title, str):
        raise ValueError("标题必须是文字。")
    title = title.strip()
    if not title or "\n" in title or "\r" in title or utf16_length(title) > TITLE_LIMIT:
        raise ValueError(f"标题须为 1–{TITLE_LIMIT} 字且不能换行；emoji 按 UTF-16 长度计数。")
    if not isinstance(description, str):
        raise ValueError("简介必须是文字。")
    if tags is None:
        tags = []
    elif isinstance(tags, str):
        tags = re.split(r"[,，\s]+", tags.strip())
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        raise ValueError("tags 必须是文字列表，或逗号分隔的文字。")
    normalized: list[str] = []
    for item in tags:
        tag = item.strip().lstrip("#")
        if not tag:
            continue
        if not re.fullmatch(r"[\w\u3400-\u9fff]+", tag):
            raise ValueError(f"话题含不支持的空格或标点：{tag}")
        if tag not in normalized:
            normalized.append(tag)
    return {"title": title, "description": description.strip(), "tags": normalized}


def validate_cover(path: Path) -> dict:
    from PIL import Image, UnidentifiedImageError

    path = Path(path)
    if not path.is_file():
        raise ValueError(f"封面不存在：{path}")
    try:
        with Image.open(path) as picture:
            width, height = picture.size
            picture.verify()
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        raise ValueError(f"封面不是可读取的图片：{path}") from exc
    if width <= 0 or height <= 0 or width * 4 != height * 3:
        raise ValueError(f"请使用已有的 3:4 竖版封面；当前尺寸为 {width}×{height}。")
    return {"width": width, "height": height, **fingerprint(path)}


def _local_path(value: object, name: str, base: Path) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"任务文件缺少 {name} 路径。")
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def load_job(path: Path, *, verify_video: bool = True) -> dict:
    path = Path(path).expanduser().resolve()
    job = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(job, dict) or job.get("schema_version") != 1 or job.get("platform") != PLATFORM:
        raise ValueError("不支持的任务文件；需要 schema_version=1、platform=xiaohongshu。")
    job.update(metadata(job.get("title"), job.get("description", ""), job.get("tags")))
    source = job.get("source")
    if not isinstance(source, dict) or not re.fullmatch(r"[0-9a-f]{64}", str(source.get("sha256", ""))):
        raise ValueError("任务文件缺少有效的视频 source.sha256。")
    video = _local_path(job.get("video"), "video", path.parent)
    if not video.is_file():
        raise ValueError(f"视频不存在：{video}")
    if verify_video and fingerprint(video)["sha256"] != source["sha256"]:
        raise ValueError("原视频已更换，请重新 prepare，避免上传错误文件。")
    cover = _local_path(job.get("cover"), "cover", path.parent)
    cover_info = validate_cover(cover)
    saved_cover = job.get("cover_source")
    if saved_cover is not None and (
        not isinstance(saved_cover, dict) or saved_cover.get("sha256") != cover_info["sha256"]
    ):
        raise ValueError("封面已更换，请重新 prepare，避免上传错误封面。")
    output = _local_path(job.get("output_dir"), "output_dir", path.parent)
    if not isinstance(job.get("state"), str) or not isinstance(job.get("history"), list):
        raise ValueError("任务文件缺少 state/history。")
    job.update(video=str(video), cover=str(cover), output_dir=str(output))
    return job


def event(job: dict, state: str, details: dict | None = None) -> None:
    timestamp = now()
    job["state"] = state
    job["updated_at"] = timestamp
    job.setdefault("history", []).append({"time": timestamp, "state": state, "details": details or {}})


class DuplicateSubmission(ValueError):
    """A previous upload or manual handoff requires read-only confirmation first."""


def _authorized_repreparation(job: dict, record: dict | None) -> bool:
    authorization = job.get("repreparation_authorization")
    saved = (record or {}).get("repreparation_authorization")
    return bool(
        isinstance(authorization, dict) and isinstance(saved, dict)
        and authorization.get("authorization_id")
        and authorization.get("authorization_id") == saved.get("authorization_id")
        and authorization.get("source_sha256") == job["source"]["sha256"]
        and authorization.get("user_confirmation", {}).get("confirmed_not_published") is True
        and (record or {}).get("source_sha256") == job["source"]["sha256"]
        and (record or {}).get("output_dir") == job["output_dir"]
        and (record or {}).get("status") == REPREPARING
        and job.get("state") in {REPREPARING, "preparing"}
    )


def authorize_repreparation(profile: Path, job: dict, *, user_confirmation: dict,
                            verification_path: Path) -> dict:
    """Archive one failed handoff and authorize exactly one new upload.

    This is an explicit operator action following the user's confirmation, not
    an automatic retry. The existing ledger row remains present throughout.
    Call outside an existing preparation_guard; this function acquires its lock.
    """
    confirmation = dict(user_confirmation)
    if (confirmation.get("source") != "user_reply"
            or confirmation.get("confirmed_not_published") is not True
            or not isinstance(confirmation.get("quote"), str) or not confirmation["quote"].strip()
            or not isinstance(confirmation.get("confirmed_at"), str) or not confirmation["confirmed_at"].strip()):
        raise ValueError("恢复上传需要用户明确确认未发布的原话、来源和时间。")
    proof_path = Path(verification_path).expanduser().resolve()
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    if (proof.get("result") != "not_found_in_current_manager"
            or proof.get("source_sha256") != job["source"]["sha256"]
            or proof.get("target_title") != job["title"]
            or proof.get("publication_verified") is not False
            or proof.get("original_prepared_tab_closed") is not True):
        raise ValueError("缺少与本条标题一致的只读未找到结果及原表单关闭证据。")
    if fingerprint(Path(job["video"]))["sha256"] != job["source"]["sha256"]:
        raise ValueError("定稿视频已更换，不能复用本条恢复授权。")
    output = Path(job["output_dir"])
    profile = Path(profile).expanduser().resolve()
    with profile_lock(profile):
        ledger_path = profile.parent / f"{profile.name}-handoff-ledger.json"
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        previous = ledger.get(job["source"]["sha256"])
        if (not isinstance(previous, dict)
                or previous.get("status") != "manual_publication_unverified"
                or job.get("state") != "manual_publication_unverified"
                or previous.get("output_dir") != str(output)
                or previous.get("publication_verified") is True
                or (output / "publish-attempt.json").exists()):
            raise DuplicateSubmission("现有记录不属于已核对、由用户确认未发布的人工交接，不能恢复。")
        authorization_id = uuid.uuid4().hex
        archive = output / "archive" / ("before-reprepare-" + authorization_id)
        archive.mkdir(parents=True, exist_ok=False)
        for child in output.iterdir():
            if child.is_file() and not child.is_symlink() and child.suffix in {".json", ".png", ".md"}:
                shutil.copy2(child, archive / child.name)
        marker = output / ATTEMPT_FILENAME
        if not marker.is_file() or fingerprint(marker)["sha256"] != fingerprint(archive / ATTEMPT_FILENAME)["sha256"]:
            raise ValueError("原交接标记未完整归档，不能开始新尝试。")
        authorization = {
            "authorization_id": authorization_id, "authorized_at": now(),
            "source_sha256": job["source"]["sha256"], "user_confirmation": confirmation,
            "verification_path": str(proof_path), "verification_sha256": fingerprint(proof_path)["sha256"],
            "archive_dir": str(archive), "previous_record": previous,
            "scope": "one_repreparation_only_final_publish_by_user",
        }
        write_json(archive / "repreparation-authorization.json", authorization)
        write_json(output / "repreparation-authorization.json", authorization)
        job["repreparation_authorization"] = authorization
        event(job, REPREPARING, {"authorization_id": authorization_id, "archive_dir": str(archive)})
        write_json(output / "manifest.json", job)
        ledger[job["source"]["sha256"]] = {
            **previous, "status": REPREPARING, "time": now(),
            "repreparation_authorization": authorization,
        }
        write_json(ledger_path, ledger)
        marker.unlink()
        return authorization


def begin_upload(job: dict, profile: Path) -> None:
    """Durably mark the upload immediately before assigning the video file input.

    Call only inside ``preparation_guard``, while its profile lock is held. The
    ledger is written here, before browser upload starts, so even a killed process
    cannot evade duplicate protection by choosing a different output directory.
    """
    profile = Path(profile).expanduser().resolve()
    output = Path(job["output_dir"])
    output.mkdir(parents=True, exist_ok=True)
    marker = output / ATTEMPT_FILENAME
    ledger_path = profile.parent / f"{profile.name}-handoff-ledger.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else {}
    key = job["source"]["sha256"]
    if not isinstance(ledger, dict):
        raise ValueError(f"浏览器任务记录格式异常：{ledger_path}")
    if key in ledger and not _authorized_repreparation(job, ledger[key]):
        raise DuplicateSubmission("这条视频已有上传记录，请先只读核对平台结果。")
    record = {
        "job_id": job.get("job_id", key[:16]), "source_sha256": key,
        "title": job["title"], "output_dir": str(output),
        "status": "upload_started", "time": now(),
    }
    if _authorized_repreparation(job, ledger.get(key)):
        record["repreparation_authorization"] = job["repreparation_authorization"]
    try:
        with marker.open("x", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise DuplicateSubmission("此任务已有视频上传标记，请先只读核对平台结果。") from exc
    ledger[key] = record
    write_json(ledger_path, ledger)


@contextlib.contextmanager
def profile_lock(profile: Path) -> Iterator[None]:
    profile = Path(profile).expanduser().resolve()
    profile.parent.mkdir(parents=True, exist_ok=True)
    lock_path = profile.parent / f"{profile.name}.agent.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("另一个任务正在使用此浏览器 profile，请先完成该任务。") from exc
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def preparation_guard(profile: Path, job: dict) -> Iterator[dict]:
    """Lock a profile and remember uploads once browser interaction can be ambiguous.

    Records contain video identifiers and states only, never browser cookies or
    credentials. A successful handoff remains unverified until a separate read-only
    inspection confirms what the user did in the platform UI. The browser calls
    ``begin_upload`` immediately before setting the video input;
    login or navigation failures before that point remain safe to retry.
    """
    profile = Path(profile).expanduser().resolve()
    with profile_lock(profile):
        ledger_path = profile.parent / f"{profile.name}-handoff-ledger.json"
        ledger = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else {}
        if not isinstance(ledger, dict):
            raise ValueError(f"浏览器任务记录格式异常：{ledger_path}")
        key = job["source"]["sha256"]
        output = Path(job["output_dir"])
        attempt = output / ATTEMPT_FILENAME
        if ((key in ledger and not _authorized_repreparation(job, ledger[key]))
                or attempt.exists() or (output / "publish-attempt.json").exists()
                or job.get("state") in BLOCKED_STATES):
            raise DuplicateSubmission(
                "这条视频已有上传、人工交接或发布记录，已阻止重复操作。"
                "请先在小红书作品管理只读核对；不能把填好页面视为已发布。"
                f"\nprofile 记录：{ledger_path}\n任务记录：{attempt}"
            )
        report: dict = {}
        try:
            yield report
        finally:
            status = report.get("status")
            if attempt.exists() or status in {
                "filled", "awaiting_user_publish", "published", "publish_attempted",
                "manual_publication_unverified",
            }:
                if status == "filled":
                    status = "awaiting_user_publish"
                elif status in {"published", "publish_attempted"}:
                    status = "manual_publication_unverified"
                if status not in {"awaiting_user_publish", "manual_publication_unverified"}:
                    status = "preparation_interrupted"
                record = {
                    **ledger.get(key, {}),
                    "job_id": job.get("job_id", key[:16]), "source_sha256": key,
                    "title": job["title"], "output_dir": str(output),
                    "status": status, "time": now(),
                }
                # Keep the browser's marker and evidence intact; it owns its
                # creation and may update it while the handoff is live.
                if not attempt.exists():
                    write_json(attempt, record)
                ledger[key] = record
                write_json(ledger_path, ledger)


# Kept as a clear migration name for callers familiar with the Douyin helper.
submission_guard = preparation_guard
