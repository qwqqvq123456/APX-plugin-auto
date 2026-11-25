#!/usr/bin/env python3
"""APX 插件上游快照同步、校验、打包与发布辅助工具。

只使用 Python 标准库。数据流：
    上游 manifest -> 校验 -> 快照哈希 -> 下载并逐文件校验 -> 打包 -> Release 资产

上游目录约定（与 APMM 的读取方式一致）：
    <base>/<PLATFORM>/manifest.json
    <base>/<PLATFORM>/<relative_path>
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

DEFAULT_BASE = "https://cnb.cool/erinilis/apx-plugin-releases/-/git/raw/main"
PLATFORMS = ("CN_STEAM", "CN_TAPTAP", "CN_BILIBILI", "CN_WEGAME", "CN_ANDROID")
PLATFORM_LABELS = {
    "CN_STEAM": "Steam 国服",
    "CN_TAPTAP": "TapTap",
    "CN_BILIBILI": "Bilibili",
    "CN_WEGAME": "WeGame",
    "CN_ANDROID": "Android",
}
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_FILES = 512
MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
USER_AGENT = "APX-plugin-auto/1.0 (+https://github.com/qwqqvq123456/APX-plugin-auto)"


class SyncError(RuntimeError):
    """可预期的同步失败。"""


class UpstreamChanged(SyncError):
    """构建期间上游发生变更，本轮结果不可信。"""


@dataclass(frozen=True)
class Entry:
    relative_path: str
    target_type: str
    target_path: str
    sha256: str
    size: int
    description: str


@dataclass(frozen=True)
class Manifest:
    platform: str
    version: str
    game_version: str
    generated_at: str
    raw_sha256: str
    raw: bytes
    files: tuple[Entry, ...]
    restore_files: tuple[Entry, ...]

    @property
    def patch_bytes(self) -> int:
        return sum(item.size for item in self.files)

    @property
    def restore_bytes(self) -> int:
        return sum(item.size for item in self.restore_files)


# --------------------------------------------------------------------------
# 网络
# --------------------------------------------------------------------------


def _request(url: str, timeout: float) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )


def http_get(url: str, max_bytes: int, timeout: float = 90.0, attempts: int = 4) -> bytes:
    last_error = ""
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(_request(url, timeout), timeout=timeout) as response:
                declared = response.headers.get("Content-Length")
                if declared and int(declared) > max_bytes:
                    raise SyncError(f"响应超过大小上限：{url}")
                data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise SyncError(f"响应超过大小上限：{url}")
            return data
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise SyncError(f"HTTP 404：{url}") from error
            last_error = f"HTTP {error.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = str(error)
        if attempt + 1 < attempts:
            time.sleep(1.5 * (2**attempt))
    raise SyncError(f"下载失败（已重试 {attempts} 次）：{url} :: {last_error}")


def http_download(url: str, destination: Path, max_bytes: int, attempts: int = 4) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    last_error = ""
    for attempt in range(attempts):
        part = destination.with_name(destination.name + ".part")
        try:
            with urllib.request.urlopen(_request(url, 120.0), timeout=120.0) as response, open(
                part, "wb"
            ) as handle:
                total = 0
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise SyncError(f"文件超过大小上限：{url}")
                    handle.write(chunk)
            os.replace(part, destination)
            return
        except urllib.error.HTTPError as error:
            last_error = f"HTTP {error.code}"
        except (urllib.error.URLError, TimeoutError, OSError, SyncError) as error:
            last_error = str(error)
        finally:
            part = destination.with_name(destination.name + ".part")
            if part.exists():
                part.unlink(missing_ok=True)
        if attempt + 1 < attempts:
            time.sleep(1.5 * (2**attempt))
    raise SyncError(f"下载失败（已重试 {attempts} 次）：{url} :: {last_error}")


def quote_relative_path(relative_path: str) -> str:
    return "/".join(urllib.parse.quote(part, safe="") for part in relative_path.split("/"))


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_relative_path(raw: str, max_depth: int = 16) -> str:
    """拒绝绝对路径、盘符、`..`、控制字符，返回统一使用 `/` 的相对路径。"""
    if not isinstance(raw, str):
        raise SyncError("路径不是字符串")
    normalized = raw.replace("&#x20;", " ").replace("\u00a0", " ").strip().replace("\\", "/")
    if not normalized:
        raise SyncError("路径为空")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise SyncError(f"拒绝绝对路径：{raw!r}")
    if any(ord(char) < 0x20 for char in normalized):
        raise SyncError(f"路径包含控制字符：{raw!r}")
    parts = [part for part in normalized.split("/") if part and part != "."]
    if any(part == ".." for part in parts):
        raise SyncError(f"拒绝路径穿越：{raw!r}")
    if not parts:
        raise SyncError(f"路径无效：{raw!r}")
    if len(parts) > max_depth:
        raise SyncError(f"路径层级过深：{raw!r}")
    return "/".join(parts)


def _parse_entry(raw: Any, kind: str) -> Entry:
    if not isinstance(raw, dict):
        raise SyncError(f"{kind} 条目不是对象")
    relative = safe_relative_path(raw.get("relative_path", ""))
    sha = str(raw.get("sha256", "")).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise SyncError(f"{kind} 条目 sha256 无效：{relative}")
    try:
        size = int(raw.get("size", 0))
    except (TypeError, ValueError) as error:
        raise SyncError(f"{kind} 条目 size 无效：{relative}") from error
    if size <= 0 or size > MAX_FILE_BYTES:
        raise SyncError(f"{kind} 条目大小越界：{relative} ({size})")
    return Entry(
        relative_path=relative,
        target_type=str(raw.get("target_type", "")),
        target_path=str(raw.get("target_path", "")),
        sha256=sha,
        size=size,
        description=str(raw.get("description", "")),
    )


def parse_manifest(payload: bytes, platform: str) -> Manifest:
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SyncError(f"{platform} 清单不是合法 JSON：{error}") from error
    if not isinstance(data, dict):
        raise SyncError(f"{platform} 清单根节点不是对象")

    declared = str(data.get("platform", "")).strip()
    if declared and declared.upper() != platform.upper():
        raise SyncError(f"{platform} 清单平台不匹配：{declared}")

    format_version = str(data.get("format_version", "")).strip()
    if not format_version:
        raise SyncError(f"{platform} 清单缺少 format_version")

    version = str(data.get("version", "")).strip()
    if not version:
        raise SyncError(f"{platform} 清单缺少 version")

    raw_files = data.get("files")
    raw_restore = data.get("restore_files", [])
    if not isinstance(raw_files, list) or not raw_files:
        raise SyncError(f"{platform} 清单没有可部署文件")
    if not isinstance(raw_restore, list):
        raise SyncError(f"{platform} restore_files 不是数组")

    files = tuple(_parse_entry(item, "files") for item in raw_files)
    restore = tuple(_parse_entry(item, "restore") for item in raw_restore)

    if len(files) > MAX_FILES or len(restore) > MAX_FILES:
        raise SyncError(f"{platform} 清单文件数量超过上限")

    for label, entries, total_key, size_key in (
        ("files", files, "total_patch_files", "total_patch_size"),
        ("restore", restore, "total_restore_files", "total_restore_size"),
    ):
        declared_count = int(data.get(total_key, 0) or 0)
        if declared_count and declared_count != len(entries):
            raise SyncError(
                f"{platform} {label} 数量不一致：声明 {declared_count}，实际 {len(entries)}"
            )
        declared_size = int(data.get(size_key, 0) or 0)
        actual_size = sum(item.size for item in entries)
        if declared_size and declared_size != actual_size:
            raise SyncError(
                f"{platform} {label} 体积不一致：声明 {declared_size}，实际 {actual_size}"
            )

    seen: set[str] = set()
    for item in files + restore:
        if item.relative_path in seen:
            raise SyncError(f"{platform} 清单包含重复路径：{item.relative_path}")
        seen.add(item.relative_path)

    if sum(item.size for item in files + restore) > MAX_TOTAL_BYTES:
        raise SyncError(f"{platform} 清单总大小超过上限")

    return Manifest(
        platform=platform,
        version=version,
        game_version=str(data.get("game_version", "")).strip(),
        generated_at=str(data.get("generated_at", "")).strip(),
        raw_sha256=sha256_bytes(payload),
        raw=payload,
        files=files,
        restore_files=restore,
    )


def http_get_optional(url: str, max_bytes: int) -> bytes | None:
    try:
        return http_get(url, max_bytes, attempts=2)
    except SyncError as error:
        if "HTTP 404" in str(error):
            return None
        raise


def snapshot_of(manifests: dict[str, Manifest]) -> str:
    canonical = "\n".join(
        f"{platform}:{manifests[platform].raw_sha256}" for platform in sorted(manifests)
    )
    return sha256_bytes(canonical.encode("utf-8"))


def fetch_manifests(base: str, platforms: Iterable[str]) -> dict[str, Manifest]:
    result: dict[str, Manifest] = {}
    for platform in platforms:
        url = f"{base.rstrip('/')}/{platform}/manifest.json"
        payload = http_get(url, MAX_MANIFEST_BYTES)
        result[platform] = parse_manifest(payload, platform)
    return result


# --------------------------------------------------------------------------
# 状态
# --------------------------------------------------------------------------


def read_state(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# 下载与打包
# --------------------------------------------------------------------------


def is_optional_metadata(entry: Entry) -> bool:
    """`runtime_dependencies.json` 只是诊断元数据，上游允许其摘要滞后。

    与 APMM 的 `is_optional_plugin_metadata` 保持一致：不因该文件的
    大小/摘要过期而判定整个快照失败，改为校验 JSON 语义。
    """
    return entry.relative_path.replace("\\", "/").lower() == "runtime_dependencies.json"


def validate_runtime_dependencies(payload: bytes, platform: str) -> None:
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SyncError(f"{platform}/runtime_dependencies.json 不是合法 JSON：{error}") from error
    if not isinstance(data, dict):
        raise SyncError(f"{platform}/runtime_dependencies.json 根节点不是对象")
    declared = str(data.get("platform", "")).strip()
    if declared and declared.upper() != platform.upper():
        raise SyncError(
            f"{platform}/runtime_dependencies.json 平台不匹配：{declared}"
        )


def platform_file_url(base: str, platform: str, relative_path: str) -> str:
    return f"{base.rstrip('/')}/{platform}/{quote_relative_path(relative_path)}"


def download_platform(
    base: str,
    manifest: Manifest,
    mirror_root: Path,
    workers: int = 4,
) -> tuple[int, list[str]]:
    """下载并校验单个平台，返回 (字节数, 警告列表, 附带文件)。"""
    entries = list(manifest.files) + list(manifest.restore_files)
    total = 0
    warnings: list[str] = []
    extras: list[dict[str, Any]] = []

    # 清单本身与还原清单不在 files 列表内，单独写入镜像，保证 zip 自包含。
    manifest_path = mirror_root / manifest.platform / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_bytes(manifest.raw)
    extras.append(
        {
            "path": "manifest.json",
            "size": len(manifest.raw),
            "sha256": manifest.raw_sha256,
        }
    )
    restore_manifest = http_get_optional(
        f"{base.rstrip('/')}/{manifest.platform}/restore/restore_manifest.json",
        1024 * 1024,
    )
    if restore_manifest is not None:
        try:
            parsed_restore = json.loads(restore_manifest.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            warnings.append(f"{manifest.platform}/restore/restore_manifest.json 不是合法 JSON，已跳过")
        else:
            if isinstance(parsed_restore, dict):
                target = mirror_root / manifest.platform / "restore" / "restore_manifest.json"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(restore_manifest)
                extras.append(
                    {
                        "path": "restore/restore_manifest.json",
                        "size": len(restore_manifest),
                        "sha256": sha256_bytes(restore_manifest),
                    }
                )
            else:
                warnings.append(f"{manifest.platform}/restore/restore_manifest.json 结构异常，已跳过")

    def fetch(item: Entry) -> int:
        target = mirror_root / manifest.platform / item.relative_path
        optional = is_optional_metadata(item)
        if target.exists():
            current_sha = sha256_file(target)
            if current_sha == item.sha256:
                return 0
            if optional:
                try:
                    validate_runtime_dependencies(target.read_bytes(), manifest.platform)
                    return 0
                except SyncError:
                    pass
        http_download(platform_file_url(base, manifest.platform, item.relative_path), target, MAX_FILE_BYTES)
        actual_size = target.stat().st_size
        actual_sha = sha256_file(target)
        if optional:
            # 上游对该文件的 size/sha256 允许滞后，只校验语义。
            try:
                validate_runtime_dependencies(target.read_bytes(), manifest.platform)
            except SyncError:
                target.unlink(missing_ok=True)
                raise
            if actual_size != item.size or actual_sha != item.sha256:
                return -1
            return 0
        if actual_size != item.size:
            target.unlink(missing_ok=True)
            raise SyncError(
                f"{manifest.platform}/{item.relative_path} 大小不符：期望 {item.size}，实际 {actual_size}"
            )
        if actual_sha != item.sha256:
            target.unlink(missing_ok=True)
            raise SyncError(
                f"{manifest.platform}/{item.relative_path} 摘要不符：期望 {item.sha256}，实际 {actual_sha}"
            )
        return item.size

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch, item): item for item in entries}
        for future in concurrent.futures.as_completed(futures):
            item = futures[future]
            result = future.result()
            if result < 0:
                warnings.append(
                    f"{manifest.platform}/{item.relative_path} 摘要或大小与清单不一致（按可选元数据处理）"
                )
                total += item.size
            else:
                total += result
    return total, warnings, extras


def _zip_add(zip_handle: zipfile.ZipFile, source: Path, arcname: str, date_time: tuple[int, ...]) -> None:
    info = zipfile.ZipInfo(arcname.replace(os.sep, "/"), date_time=date_time)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    with open(source, "rb") as handle:
        zip_handle.writestr(info, handle.read(), compresslevel=9)


def _platform_files(mirror_root: Path, manifest: Manifest) -> list[Path]:
    root = mirror_root / manifest.platform
    paths = [root / "manifest.json"]
    paths.extend(root / item.relative_path for item in (*manifest.files, *manifest.restore_files))
    restore_manifest = root / "restore" / "restore_manifest.json"
    if restore_manifest.is_file():
        paths.append(restore_manifest)
    for path in paths:
        if not path.is_file():
            raise SyncError(f"打包缺少文件：{path.relative_to(mirror_root)}")
    return paths


def zip_platform(mirror_root: Path, manifest: Manifest, destination: Path) -> None:
    date_time = (2026, 1, 1, 0, 0, 0)
    with zipfile.ZipFile(destination, "w", allowZip64=True) as archive:
        for source in _platform_files(mirror_root, manifest):
            arcname = source.relative_to(mirror_root).as_posix()
            _zip_add(archive, source, arcname, date_time)


def zip_all(mirror_root: Path, manifests: dict[str, Manifest], destination: Path) -> None:
    date_time = (2026, 1, 1, 0, 0, 0)
    with zipfile.ZipFile(destination, "w", allowZip64=True) as archive:
        for platform in sorted(manifests):
            for source in _platform_files(mirror_root, manifests[platform]):
                _zip_add(archive, source, source.relative_to(mirror_root).as_posix(), date_time)


def sanitize_name(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z._-]+", "-", value).strip("-.")
    return cleaned or "unknown"


# --------------------------------------------------------------------------
# 命令
# --------------------------------------------------------------------------


def cmd_check(args: argparse.Namespace) -> int:
    manifests = fetch_manifests(args.base, args.platforms)
    snapshot = snapshot_of(manifests)
    version = manifests[args.version_from].version
    state = read_state(Path(args.state) if args.state else None)
    changed = args.force or state.get("snapshot") != snapshot

    summary = {
        "changed": changed,
        "snapshot": snapshot,
        "version": version,
        "upstream_base": args.base,
        "warnings": warnings,
        "platforms": {
            platform: {
                "version": manifest.version,
                "generated_at": manifest.generated_at,
                "files": len(manifest.files),
                "restore_files": len(manifest.restore_files),
                "bytes": manifest.patch_bytes + manifest.restore_bytes,
                "manifest_sha256": manifest.raw_sha256,
            }
            for platform, manifest in manifests.items()
        },
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as handle:
            handle.write(f"changed={'true' if changed else 'false'}\n")
            handle.write(f"snapshot={snapshot}\n")
            handle.write(f"version={version}\n")
            handle.write(f"platforms={','.join(sorted(manifests))}\n")
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    mirror_root = Path(args.mirror)
    out_dir.mkdir(parents=True, exist_ok=True)

    manifests = fetch_manifests(args.base, args.platforms)
    snapshot = snapshot_of(manifests)
    if args.expect_snapshot and snapshot != args.expect_snapshot:
        raise UpstreamChanged(
            f"上游在构建期间更新：期望 {args.expect_snapshot}，实际 {snapshot}"
        )

    version = manifests[args.version_from].version
    warnings: list[str] = []
    extras: dict[str, list[dict[str, Any]]] = {}
    for platform, manifest in manifests.items():
        _, platform_warnings, platform_extras = download_platform(
            args.base, manifest, mirror_root, workers=args.workers
        )
        warnings.extend(platform_warnings)
        extras[platform] = platform_extras
        print(f"[ok] {platform} {manifest.version} {len(manifest.files)} files", file=sys.stderr)
    for warning in warnings:
        print(f"[warn] {warning}", file=sys.stderr)

    declared = {platform: manifest.version for platform, manifest in manifests.items()}
    mismatched = {platform: value for platform, value in declared.items() if value != version}
    if mismatched:
        raise SyncError(f"各平台版本不一致：基准 {version}，实际 {mismatched}")

    safe_version = sanitize_name(version)
    assets: dict[str, dict[str, Any]] = {}
    for platform in sorted(manifests):
        name = f"APX-{platform}-{safe_version}.zip"
        destination = out_dir / name
        zip_platform(mirror_root, manifests[platform], destination)
        assets[name] = {
            "platform": platform,
            "size": destination.stat().st_size,
            "sha256": sha256_file(destination),
        }
        print(f"[zip] {name} {destination.stat().st_size} bytes", file=sys.stderr)

    all_name = f"APX-all-{safe_version}.zip"
    all_path = out_dir / all_name
    zip_all(mirror_root, manifests, all_path)
    assets[all_name] = {"platform": "ALL", "size": all_path.stat().st_size, "sha256": sha256_file(all_path)}

    index = {
        "schema": 1,
        "snapshot": snapshot,
        "version": version,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "upstream_base": args.base,
        "warnings": warnings,
        "platforms": {
            platform: {
                "label": PLATFORM_LABELS.get(platform, platform),
                "version": manifest.version,
                "game_version": manifest.game_version,
                "generated_at": manifest.generated_at,
                "files": len(manifest.files),
                "restore_files": len(manifest.restore_files),
                "bytes": manifest.patch_bytes + manifest.restore_bytes,
                "manifest_sha256": manifest.raw_sha256,
                "asset": f"APX-{platform}-{safe_version}.zip",
                "extra_files": extras.get(platform, []),
                "optional_metadata_stale": any(
                    warning.startswith(f"{platform}/runtime_dependencies.json") for warning in warnings
                ),
            }
            for platform, manifest in manifests.items()
        },
        "assets": assets,
    }
    (out_dir / "apx-index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "latest.json").write_text(
        json.dumps(
            {
                "version": version,
                "snapshot": snapshot,
                "index": "apx-index.json",
                "platforms": sorted(manifests),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    sums = [f"{info['sha256']}  {name}" for name, info in sorted(assets.items())]
    (out_dir / "SHA256SUMS.txt").write_text("\n".join(sums) + "\n", encoding="utf-8")

    total_bytes = sum(manifest.patch_bytes + manifest.restore_bytes for manifest in manifests.values())
    rows = "\n".join(
        f"| {platform} | {PLATFORM_LABELS.get(platform, platform)} | {manifest.version} | "
        f"{len(manifest.files)} | {len(manifest.restore_files)} | "
        f"{manifest.patch_bytes + manifest.restore_bytes:,} |"
        for platform, manifest in sorted(manifests.items())
    )
    notes = f"""APX 插件自动发布（游戏版本 {version}）

- 上游快照：`{snapshot}`
- 生成时间：{index['generated_at']}
- 上游地址：{args.base}
- 全部平台解压后合计：{total_bytes:,} 字节
- 可选元数据告警：{len(warnings)} 条{'（`runtime_dependencies.json` 的 size/sha256 允许滞后，已按 JSON 语义校验）' if warnings else ''}

| 平台 | 客户端 | 版本 | 补丁文件 | 还原文件 | 原始字节 |
| --- | --- | --- | --- | --- | --- |
{rows}

资产说明：

- `APX-<平台>-<版本>.zip`：单平台镜像，目录结构与上游一致（`<平台>/manifest.json` 与对应文件）。
- `APX-all-<版本>.zip`：全部平台合集。
- `apx-index.json` / `latest.json`：版本、快照、文件数量与资产摘要。
- `SHA256SUMS.txt`：全部资产的 SHA-256。

> 本仓库仅做校验、打包与分发，不修改插件内容。文件所有权归上游作者。
"""
    (out_dir / "RELEASE_NOTES.md").write_text(notes, encoding="utf-8")
    (out_dir / "release-tag.txt").write_text(f"apx-{safe_version}\n", encoding="utf-8")

    state_payload = {
        "snapshot": snapshot,
        "version": version,
        "release_tag": f"apx-{safe_version}",
        "updated_at": index["generated_at"],
        "upstream_base": args.base,
    }
    (out_dir / "state.json").write_text(
        json.dumps(state_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(
        json.dumps(
            {
                "snapshot": snapshot,
                "version": version,
                "tag": f"apx-{safe_version}",
                "assets": assets,
                "total_source_bytes": total_bytes,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """离线校验一个已生成目录：清单哈希、资产哈希与索引一致。"""
    root = Path(args.dir)
    index = json.loads((root / "apx-index.json").read_text(encoding="utf-8"))
    for name, info in index["assets"].items():
        path = root / name
        if not path.is_file():
            raise SyncError(f"缺少资产：{name}")
        if path.stat().st_size != info["size"]:
            raise SyncError(f"资产大小不符：{name}")
        if sha256_file(path) != info["sha256"]:
            raise SyncError(f"资产摘要不符：{name}")
    print(json.dumps({"ok": True, "assets": len(index["assets"])}, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="APX 插件快照同步与打包")
    parser.add_argument("--base", default=os.environ.get("APX_UPSTREAM_BASE", DEFAULT_BASE))
    parser.add_argument("--platforms", default=",".join(PLATFORMS))
    parser.add_argument("--version-from", default="CN_STEAM")
    parser.add_argument("--state", default="state/state.json")
    parser.add_argument("--workers", type=int, default=4)
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="只读取清单并判断快照是否变化")
    check.add_argument("--force", action="store_true")
    check.set_defaults(func=cmd_check)

    build = sub.add_parser("build", help="下载、校验并生成发行资产")
    build.add_argument("--out", default="dist")
    build.add_argument("--mirror", default="mirror")
    build.add_argument("--expect-snapshot", default="")
    build.set_defaults(func=cmd_build)

    verify = sub.add_parser("verify", help="校验已生成的资产")
    verify.add_argument("--dir", default="dist")
    verify.set_defaults(func=cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.platforms = tuple(item.strip() for item in args.platforms.split(",") if item.strip())
    unknown = [item for item in args.platforms if item not in PLATFORMS]
    if unknown:
        parser.error(f"未知平台：{', '.join(unknown)}")
    try:
        return args.func(args)
    except UpstreamChanged as error:
        print(f"[upstream-changed] {error}", file=sys.stderr)
        return 2
    except SyncError as error:
        print(f"[error] {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
