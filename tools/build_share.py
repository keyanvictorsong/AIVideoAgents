#!/usr/bin/env python3
"""Update a reviewed manifest and export only explicit or Git-index files."""
from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import tempfile
import zipfile

from check_share import (MANIFEST, ROOT, ShareError, SharedFile, check_files, display_path,
                         inspect_bytes, parse_manifest, read_index, read_regular, safe_relative)


def select_files(root: Path, includes: list[str]) -> dict[str, SharedFile]:
    if (root / ".git").exists() or (root / ".git").is_symlink():
        files = read_index(root)
        for relative in includes:
            safe_relative(relative)
            if relative not in files:
                raise ShareError("--include 文件尚未暂存；请先 git add 明确文件")
        files.pop(MANIFEST, None)
    else:
        _, entries = parse_manifest(read_regular(root, MANIFEST).data)
        names = set(entries)
        for relative in includes:
            relative = safe_relative(relative)
            if relative.casefold() == MANIFEST.casefold():
                raise ShareError("MANIFEST.json 由工具生成，无需 --include")
            names.add(relative)
        if len({name.casefold() for name in names}) != len(names):
            raise ShareError("白名单含大小写冲突")
        files = {relative: read_regular(root, relative) for relative in sorted(names)}
    for relative, item in files.items():
        problems = inspect_bytes(relative, item.data, item.mode)
        if problems:
            raise ShareError(display_path(relative) + "：" + "、".join(problems))
    return files


def manifest_bytes(files: dict[str, SharedFile], prepared_date: date) -> bytes:
    manifest = {
        "schema_version": 1,
        "package": "AIVideoAgents",
        "prepared_date": prepared_date.isoformat(),
        "description": "Sanitized reusable video workflow sources; no sender credentials or media.",
        "files": [{"path": relative, "bytes": len(item.data), "sha256": hashlib.sha256(item.data).hexdigest()}
                  for relative, item in sorted(files.items())],
    }
    return (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _write_manifest(root: Path, data: bytes) -> None:
    target = root / MANIFEST
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise ShareError("MANIFEST.json 必须是普通文件")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".share-manifest-", dir=root, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def build(root: Path, *, includes: list[str] | None = None, prepared_date: date | None = None,
          output: Path | None = None, overwrite: bool = False) -> int:
    root = root.expanduser().resolve()
    if output is not None:
        output = output.expanduser().resolve()
        if output.is_relative_to(root):
            raise ShareError("ZIP 必须写入包目录之外")
        if output.suffix.lower() != ".zip":
            raise ShareError("导出文件扩展名必须是 .zip")
        if output.exists() and not overwrite:
            raise ShareError("ZIP 已存在；需要替换时明确传 --overwrite")
    files = select_files(root, includes or [])
    data = manifest_bytes(files, prepared_date or date.today())
    files[MANIFEST] = SharedFile(data)
    _, problems = check_files(files)
    if problems:
        raise ShareError("生成内容检查失败：" + "；".join(problems))
    # Keep ZIPs portable and deterministic; preserve executable bits without host metadata.
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(prefix=".share-export-", suffix=".zip", dir=output.parent, delete=False) as handle:
                temporary = Path(handle.name)
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for relative, item in sorted(files.items()):
                    info = zipfile.ZipInfo("AIVideoAgents/" + relative, date_time=(2020, 1, 1, 0, 0, 0))
                    info.create_system = 3
                    info.external_attr = (0o100755 if item.mode == "100755" else 0o100644) << 16
                    info.compress_type = zipfile.ZIP_DEFLATED
                    archive.writestr(info, item.data)
            if output.exists() and not overwrite:
                raise ShareError("ZIP 已存在，未替换")
            os.replace(temporary, output)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    _write_manifest(root, data)
    return len(files) - 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="Package root")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--manifest-only", action="store_true", help="Only refresh MANIFEST; stage it afterwards")
    mode.add_argument("--output", type=Path, help="ZIP destination outside the package")
    parser.add_argument("--include", action="append", default=[], help="Explicit relative file to add before Git initialization; repeat as needed")
    parser.add_argument("--date", type=date.fromisoformat, default=None, metavar="YYYY-MM-DD", help="Explicit prepared_date; default: local current date")
    parser.add_argument("--overwrite", action="store_true", help="Replace an existing ZIP")
    args = parser.parse_args(argv)
    try:
        count = build(args.root, includes=args.include, prepared_date=args.date, output=args.output, overwrite=args.overwrite)
    except (ShareError, OSError) as exc:
        print("构建未通过：" + (str(exc) if isinstance(exc, ShareError) else "无法写入清单或包外 ZIP"))
        return 1
    print(f"已更新 MANIFEST.json：{count} 个审核文件" + ("；已导出包外 ZIP。" if args.output else "。"))
    print("Git 仓库使用暂存字节；更新后请 git add MANIFEST.json，再运行 tools/check_share.py --repo。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
