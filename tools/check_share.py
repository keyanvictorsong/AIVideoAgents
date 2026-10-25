#!/usr/bin/env python3
"""Scan either a pristine distribution or the exact bytes in a Git index."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "MANIFEST.json"
MAX_FILE_BYTES = 10 * 1024 * 1024
PRIVATE_PARTS = {
    ".git", ".local", ".venv", "venv", "__pycache__", ".pytest_cache",
    ".ruff_cache", ".cache", "node_modules", "runs", "output", "outputs",
    "browser-profile", "profiles", "logs", "research", "02examplevideo",
    ".ssh", ".aws", ".azure", ".config", "appdata", "library",
    "browser-state", "storage-state", "sessions", "downloads", "screenshots",
}
PRIVATE_NAMES = {
    ".ds_store", "cookies", "cookies.json", "cookies.txt", "login data", "history", "local state",
    ".xhs-auth-session.json", "storage-state.json", "auth-state.json", "credentials.json",
    "credential.json", "credential-store.json", "token.json", "tokens.json", "token-cache.json",
    "session.json", "sessions.json", "browser-session.json", "session-state.json",
    "auth.json", "authentication.json", "preferences", "preferences.md", "preferences.json",
    "preference.md", "preference.json", ".netrc", ".npmrc", ".pypirc",
}
SUFFIXES = {".md", ".py", ".toml", ".txt", ".json", ".swift", ".html", ".js", ".sh", ".command", ".ps1", ".yaml", ".yml"}
EXTENSIONLESS = {".gitignore", ".env.example", "config.env.example", "xhs-publisher", "guider"}
PATTERNS = {
    "疑似服务密钥": r"\b(?:sk-[A-Za-z0-9_-]{20,}|hf_[A-Za-z0-9]{25,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{30,}|xox[baprs]-[A-Za-z0-9-]{20,})\b",
    "疑似认证令牌": r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{16,}\b",
    "私钥": "-----BEGIN " + r"(?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----",
    "机器用户主目录": "/" + r"(?:Users|home)/[^/\s\"'<>]+/",
    "Windows 用户主目录": r"[A-Za-z]:\\+(?:Users|Documents and Settings)\\+[^\\\s]+\\+",
}


class ShareError(ValueError):
    """A safe diagnostic that never embeds scanned content or Git stderr."""


@dataclass(frozen=True)
class SharedFile:
    data: bytes
    mode: str = "100644"


def safe_relative(relative: object) -> str:
    if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative:
        raise ShareError("含不安全路径")
    if any(ord(c) < 32 or ord(c) == 127 for c in relative):
        raise ShareError("含不安全路径")
    parts = relative.split("/")
    if (PurePosixPath(relative).is_absolute() or PureWindowsPath(relative).drive
            or any(part in {"", ".", ".."} or part.endswith((".", " ")) for part in parts)):
        raise ShareError("含不安全路径")
    devices = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
    if any(part.split(".")[0].casefold() in devices for part in parts):
        raise ShareError("含不安全路径")
    return relative


def display_path(relative: str) -> str:
    try:
        safe_relative(relative)
    except ShareError:
        return "[不安全路径]"
    if any(re.search(pattern, relative) for pattern in PATTERNS.values()):
        return "[文件名含敏感特征]"
    return relative


def path_problems(relative: str) -> list[str]:
    safe_relative(relative)
    parts = PurePosixPath(relative).parts
    folded = [part.casefold() for part in parts]
    name = folded[-1]
    problems = []
    if any(part in PRIVATE_PARTS or part.endswith(".egg-info") for part in folded):
        problems.append("包含运行数据/私人目录")
    state_snapshot = name.startswith(("storage_state", "storage-state", "auth_state", "auth-state")) and name.endswith(".json")
    if name in PRIVATE_NAMES or state_snapshot or (name.startswith(".env") and name != ".env.example"):
        problems.append("包含私有配置或浏览器文件")
    if folded[0] == "02videoworkspace" and relative != "02VideoWorkSpace/README.md":
        problems.append("包含任务数据")
    if PurePosixPath(relative).suffix not in SUFFIXES and parts[-1] not in EXTENSIONLESS:
        problems.append("未审核的文件类型")
    for label, pattern in PATTERNS.items():
        if re.search(pattern, relative):
            problems.append(label)
    return problems


def inspect_bytes(relative: str, data: bytes, mode: str = "100644") -> list[str]:
    problems = path_problems(relative)
    if mode not in {"100644", "100755"}:
        return problems + ["不允许符号链接、子模块或非普通文件"]
    if len(data) > MAX_FILE_BYTES:
        return problems + ["文件超过源码大小限制"]
    try:
        body = data.decode("utf-8")
    except UnicodeError:
        return problems + ["非 UTF-8 文本"]
    if "\x00" in body:
        problems.append("包含二进制内容")
    for label, pattern in PATTERNS.items():
        if re.search(pattern, body):
            problems.append(label)
    return problems


def read_regular(root: Path, relative: str) -> SharedFile:
    safe_relative(relative)
    path = root
    try:
        for part in PurePosixPath(relative).parts:
            path /= part
            if path.is_symlink():
                raise ShareError("不允许文件或父目录为符号链接")
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ShareError("不是普通文件")
        if info.st_size > MAX_FILE_BYTES:
            raise ShareError("文件超过源码大小限制")
        return SharedFile(path.read_bytes(), "100755" if info.st_mode & 0o111 else "100644")
    except OSError as exc:
        raise ShareError("文件缺失或无法读取") from exc


def inspect_file(relative: str, path: Path) -> list[str]:
    """Compatibility helper; repository checks use inspect_bytes on Git blobs."""
    if path.is_symlink():
        return ["不允许符号链接"]
    try:
        return inspect_bytes(relative, path.read_bytes())
    except OSError:
        return ["文件缺失或无法读取"]


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ShareError("清单含重复 JSON 字段")
        result[key] = value
    return result


def parse_manifest(data: bytes) -> tuple[dict, dict[str, dict]]:
    issues = inspect_bytes(MANIFEST, data)
    if issues:
        raise ShareError("清单：" + "、".join(issues))
    try:
        manifest = json.loads(data, object_pairs_hook=_unique_object)
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
            raise ShareError("清单版本或结构无效")
        entries = manifest["files"]
        if not isinstance(entries, list):
            raise ShareError("清单 files 必须是列表")
        by_path = {}
        seen = {MANIFEST.casefold()}
        for entry in entries:
            if not isinstance(entry, dict):
                raise ShareError("清单文件项必须是对象")
            relative = safe_relative(entry.get("path"))
            if relative.casefold() in seen:
                raise ShareError("清单含重复路径或大小写冲突")
            seen.add(relative.casefold())
            issues = path_problems(relative)
            if issues:
                raise ShareError(display_path(relative) + "：" + "、".join(issues))
            size, digest = entry.get("bytes"), entry.get("sha256")
            if type(size) is not int or not 0 <= size <= MAX_FILE_BYTES:
                raise ShareError("清单文件大小无效")
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ShareError("清单 SHA256 无效")
            by_path[relative] = entry
        return manifest, by_path
    except (ValueError, KeyError, TypeError, UnicodeError) as exc:
        if isinstance(exc, ShareError):
            raise
        raise ShareError("清单无法解析") from exc


def _git(root: Path, arguments: list[str], data: bytes | None = None) -> bytes:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment["GIT_TERMINAL_PROMPT"] = "0"
    try:
        result = subprocess.run(["git", "-C", str(root), *arguments], input=data,
                                capture_output=True, check=False, env=environment)
    except OSError as exc:
        raise ShareError("无法运行 Git") from exc
    if result.returncode:
        raise ShareError("无法读取 Git 索引；请在包根初始化仓库并暂存文件")
    return result.stdout


def read_index(root: Path) -> dict[str, SharedFile]:
    """Read staged modes and object bytes; never substitute working-tree files."""
    try:
        top = Path(os.fsdecode(_git(root, ["rev-parse", "--show-toplevel"]).rstrip(b"\n"))).resolve()
    except (UnicodeError, ValueError) as exc:
        raise ShareError("无法确定 Git 仓库根目录") from exc
    if top != root.resolve():
        raise ShareError("包根必须是 Git 仓库根目录，不能借用上级仓库")
    records = []
    seen = set()
    for record in _git(root, ["ls-files", "--stage", "-z"]).split(b"\0"):
        if not record:
            continue
        try:
            header, raw_path = record.split(b"\t", 1)
            mode, oid, stage = header.decode("ascii").split()
            relative = safe_relative(raw_path.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise ShareError("Git 索引含不安全路径或记录") from exc
        if stage != "0":
            raise ShareError("Git 索引有未解决冲突")
        if relative.casefold() in seen:
            raise ShareError("Git 索引含重复路径或大小写冲突")
        seen.add(relative.casefold())
        if mode not in {"100644", "100755"}:
            raise ShareError(display_path(relative) + "：不允许符号链接或子模块")
        issues = path_problems(relative)
        if issues:
            raise ShareError(display_path(relative) + "：" + "、".join(issues))
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid):
            raise ShareError("Git 对象编号无效")
        records.append((relative, mode, oid))
    if not records:
        raise ShareError("Git 索引为空；请先暂存明确要分享的文件")
    # Query sizes before reading any blob so accidentally staged media stays bounded.
    queries = b"".join(oid.encode("ascii") + b"\n" for _, _, oid in records)
    sizes = _git(root, ["cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"], queries).splitlines()
    if len(sizes) != len(records):
        raise ShareError("无法读取完整 Git 对象信息")
    for info in sizes:
        fields = info.split()
        if len(fields) != 3 or fields[1] != b"blob" or not fields[2].isdigit() or int(fields[2]) > MAX_FILE_BYTES:
            raise ShareError("Git 索引含非文本对象或过大文件")
    raw = _git(root, ["cat-file", "--batch"], queries)
    cursor, files = 0, {}
    for relative, mode, oid in records:
        end = raw.find(b"\n", cursor)
        if end < 0:
            raise ShareError("Git 对象内容不完整")
        header = raw[cursor:end].split()
        if len(header) != 3 or header[0] != oid.encode("ascii") or header[1] != b"blob" or not header[2].isdigit():
            raise ShareError("Git 对象内容无效")
        size = int(header[2])
        cursor = end + 1
        if cursor + size >= len(raw) or raw[cursor + size:cursor + size + 1] != b"\n":
            raise ShareError("Git 对象内容不完整")
        files[relative] = SharedFile(raw[cursor:cursor + size], mode)
        cursor += size + 1
    if cursor != len(raw):
        raise ShareError("Git 对象存在额外内容")
    return files


def check_files(files: dict[str, SharedFile]) -> tuple[int, list[str]]:
    if MANIFEST not in files:
        raise ShareError("缺少 MANIFEST.json，无法核对交付文件")
    _, entries = parse_manifest(files[MANIFEST].data)
    problems = []
    expected = set(entries) | {MANIFEST}
    for relative in sorted(expected - files.keys()):
        problems.append(display_path(relative) + "：文件缺失")
    for relative in sorted(files.keys() - expected):
        problems.append(display_path(relative) + "：额外文件，未列入交付清单")
    for relative, item in files.items():
        if relative in entries:
            entry = entries[relative]
            if len(item.data) != entry["bytes"] or hashlib.sha256(item.data).hexdigest() != entry["sha256"]:
                problems.append(display_path(relative) + "：内容与交付清单不同")
        for issue in inspect_bytes(relative, item.data, item.mode):
            problems.append(display_path(relative) + "：" + issue)
    return len(entries), problems


def check_distribution(root: Path) -> tuple[int, list[str]]:
    manifest_file = read_regular(root, MANIFEST)
    _, entries = parse_manifest(manifest_file.data)
    files = {MANIFEST: manifest_file}
    problems = []
    for relative in entries:
        try:
            files[relative] = read_regular(root, relative)
        except ShareError as exc:
            problems.append(display_path(relative) + "：" + str(exc))
    count, file_problems = check_files(files)
    problems.extend(file_problems)
    expected = set(entries) | {MANIFEST}
    for item in root.rglob("*"):
        relative = item.relative_to(root).as_posix()
        if item.is_symlink() or (item.is_file() and relative not in expected):
            problems.append(display_path(relative) + "：额外文件/链接，未列入交付清单")
        elif item.is_dir() and any(part.casefold() in PRIVATE_PARTS or part.casefold().endswith(".egg-info") for part in item.relative_to(root).parts):
            problems.append(display_path(relative) + "：额外运行目录")
    return count, problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", action="store_true", help="Only scan staged Git blobs and the staged MANIFEST; ignore local runtime files")
    parser.add_argument("--root", type=Path, default=ROOT, help="Package root (default: this tool's parent package)")
    args = parser.parse_args(argv)
    root = args.root.expanduser().resolve()
    try:
        count, problems = check_files(read_index(root)) if args.repo else check_distribution(root)
    except ShareError as exc:
        count, problems = 0, [str(exc)]
    if problems:
        print("检查未通过（只显示文件及问题类型，不显示凭据）：")
        for problem in dict.fromkeys(problems):
            print("- " + problem)
        return 1
    scope = "Git 索引" if args.repo else "完整分发目录"
    print(f"检查通过：{scope}中 {count} 个文件哈希匹配，未发现禁止的数据或凭据特征。")
    print("此结果为静态筛查，不检查历史提交；推送前仍需人工审阅暂存内容。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
