#!/usr/bin/env python3
"""按明确文件清单验证并打包干净模板；不收集个人工作区。"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
from urllib.parse import unquote
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = PROJECT / "模板/个人AI协作区"
MANIFEST = Path(__file__).with_name("发行清单.json")
CHECKSUM_NAME = "中枢/模板/出厂文件清单.json"
SECRET_PATTERNS = {
    "token": r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b",
    "private_key": r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----",
    "cloud_key": r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    "credential_url": r"https?://[^\s/@:]+:[^\s/@]+@",
    "drive_path": r"\b[A-Za-z]:[\\/]",
    "user_home_path": r"(?i)(?:[A-Za-z]:[\\/]Users[\\/][^\\/\s]+|/(?:Users|home)/[^/\s]+)",
}


class BuildError(Exception):
    pass


def ensure_plain(path: Path) -> None:
    if path.is_symlink() or (path.exists() and getattr(path.lstat(), "st_file_attributes", 0) & 0x400):
        raise BuildError(f"不纳入链接或重解析点：{path.name}")
    if path.is_file() and path.stat().st_nlink > 1:
        raise BuildError(f"不纳入硬链接文件：{path.name}")


def load_manifest() -> dict:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not data.get("files"):
        raise BuildError("发行清单格式错误。")
    files = data["files"]
    if len(files) != len(set(files)):
        raise BuildError("发行清单包含重复文件。")
    for name in [data["archive_root"], *files]:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
            raise BuildError("发行清单必须使用安全相对路径。")
    return data


def source_paths(source: Path) -> list[Path]:
    absolute = Path(os.path.abspath(source))
    for ancestor in (*reversed(absolute.parents), absolute):
        ensure_plain(ancestor)
    result = []
    for directory, dirs, names in os.walk(source, followlinks=False):
        base = Path(directory)
        for name in dirs:
            ensure_plain(base / name)
        for name in names:
            path = base / name
            ensure_plain(path)
            if not path.is_file():
                raise BuildError("模板包含非常规文件。")
            result.append(path)
    return result


def markdown_targets(text: str) -> list[str]:
    prose = re.sub(r"```.*?```|~~~.*?~~~", "", text, flags=re.DOTALL)
    inline = re.findall(r"\[[^\]\n]*\]\(\s*(<[^>]+>|[^\s)]+)(?:\s+[\"'][^\n]*?[\"'])?\s*\)", prose)
    references = re.findall(r"(?m)^\s{0,3}\[[^\]\n]+\]:\s*(<[^>]+>|\S+)", prose)
    html = re.findall(r"(?i)(?:href|src)\s*=\s*[\"']([^\"']+)[\"']", prose)
    autolinks = re.findall(r"<([A-Za-z][A-Za-z0-9+.-]*:[^<>\s]+)>", prose)
    return [target.strip().removeprefix("<").removesuffix(">") for target in inline + references + html + autolinks]


def verify(source: Path) -> tuple[dict, list[dict], dict[str, bytes]]:
    manifest = load_manifest()
    if not source.is_dir():
        raise BuildError("模板来源目录不存在。")
    paths = source_paths(source)
    actual = {path.relative_to(source).as_posix() for path in paths}
    expected = set(manifest["files"])
    if actual != expected:
        raise BuildError("模板文件与发行清单不符：" + json.dumps({"missing": sorted(expected - actual), "extra": sorted(actual - expected)}, ensure_ascii=False))
    info = json.loads((source / "中枢/模板/模板信息.json").read_text(encoding="utf-8"))
    if info.get("template_version") != manifest["template_version"]:
        raise BuildError("模板版本与发行清单不一致。")
    records = []
    payload = {}
    root = source.resolve()
    for name in sorted(expected):
        raw = (source / name).read_bytes()
        text = raw.decode("utf-8-sig")
        for kind, pattern in SECRET_PATTERNS.items():
            if re.search(pattern, text):
                raise BuildError(f"发现需人工检查的 {kind}：{name}")
        if name.endswith(".py"):
            ast.parse(text, filename=name, feature_version=(3, 11))
        if name.endswith(".md"):
            for href in markdown_targets(text):
                if href.startswith("#") or re.match(r"^https?://", href, flags=re.IGNORECASE):
                    continue
                decoded = unquote(href)
                if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", decoded) or decoded.startswith(("/", "\\")):
                    raise BuildError(f"不允许本地 URI、绝对路径或非网页协议：{name}；{href}")
                relative = decoded.split("#", 1)[0].split("?", 1)[0]
                target = ((source / name).parent / relative).resolve()
                if not target.is_relative_to(root):
                    raise BuildError(f"非独立的文档链接：{name}；{href}")
                normalized = target.relative_to(root).as_posix()
                is_archived = normalized in expected or any(item.startswith(normalized.rstrip("/") + "/") for item in expected)
                if normalized == ".":
                    is_archived = True
                if not target.exists() or not is_archived:
                    raise BuildError(f"目标不会随包交付的文档链接：{name}；{href}")
        payload[name] = raw
        records.append({"path": name, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    memory = payload["中枢/记忆/记忆.md"].decode("utf-8-sig")
    begin = "<!-- BEGIN GENERATED MEMORY LINKS -->"
    end = "<!-- END GENERATED MEMORY LINKS -->"
    if memory.count(begin) != 1 or memory.count(end) != 1 or memory.index(begin) >= memory.index(end):
        raise BuildError("记忆生成标记必须唯一且顺序正确。")
    if memory.split(begin, 1)[1].split(end, 1)[0].strip():
        raise BuildError("干净模板的记忆生成区必须为空。")
    return manifest, records, payload


def archive_bytes(manifest: dict, records: list[dict], payload: dict[str, bytes]) -> bytes:
    import io
    buffer = io.BytesIO()
    root = manifest["archive_root"]
    checksums = {"schema_version": 1, "template_version": manifest["template_version"], "files": records}
    all_files = {**payload, CHECKSUM_NAME: (json.dumps(checksums, ensure_ascii=False, indent=2) + "\n").encode("utf-8")}
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(all_files.items()):
            entry = zipfile.ZipInfo(f"{root}/{name}", date_time=(2026, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            entry.create_system = 3
            archive.writestr(entry, data)
    return buffer.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output-dir", type=Path, default=PROJECT / "发行")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--json", action="store_true")
    options = parser.parse_args()
    try:
        manifest, records, payload = verify(options.source)
        result = {"ok": True, "template_version": manifest["template_version"], "source_files": len(records), "checked": True}
        if not options.check:
            output = options.output_dir
            if output.resolve().is_relative_to(options.source.resolve()):
                raise BuildError("发行目录不能位于模板来源内部。")
            for path in (output, *output.parents):
                ensure_plain(path)
            output.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            name = f"ai-workspace-{manifest['template_version']}-{stamp}.zip"
            data = archive_bytes(manifest, records, payload)
            archive_path = output / name
            with archive_path.open("xb") as stream:
                stream.write(data)
            digest = hashlib.sha256(data).hexdigest()
            with archive_path.with_suffix(".zip.sha256").open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(f"{digest}  {name}\n")
            result.update({"archive": str(archive_path), "sha256": digest, "archive_entries": len(records) + 1})
    except (BuildError, OSError, ValueError, SyntaxError, UnicodeError) as exc:
        result = {"ok": False, "error": str(exc)}
    if options.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2), file=sys.stdout if result["ok"] else sys.stderr)
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
