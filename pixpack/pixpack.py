#!/usr/bin/env python3
"""把一个目录无损打包进一张或多张 PNG，并可以完整还原。

图片只是容器：文件先打成 zip，再按 RGB 像素逐字节写入 PNG。
必须使用 PNG。转成 JPEG、改尺寸、或经过会重新压缩图片的平台后无法还原。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import math
import os
import struct
import sys
import tempfile
import time
import zipfile
import zlib
from collections.abc import Callable
from pathlib import Path

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    if sys.stderr is not None:
        sys.stderr.write("缺少 Pillow。请先执行: python -m pip install -r requirements.txt\n")
    raise

MAGIC = b"PX01"
VERSION = 1
# flags 的最低位：1 表示源是单个文件，0 表示源是文件夹。旧图片这一位是 0。
FLAG_SINGLE_FILE = 1
# magic4 + version2 + index2 + count2 + flags2 + offset8 + total8 + chunk4 + crc4 + sha256
HEADER = struct.Struct("<4sHHHHQQII32s")
HEADER_SIZE = HEADER.size  # 68


class PixpackError(Exception):
    """用户可处理的格式或校验错误。"""


ProgressCb = Callable[[float, str], None]
CancelCb = Callable[[], bool]
LogCb = Callable[[str], None]


def _emit(progress: ProgressCb | None, frac: float, message: str) -> None:
    if progress is None:
        return
    # 负数表示总量还未知，界面改走不定进度，但文字会持续更新。
    if frac >= 0.0:
        frac = min(1.0, frac)
    progress(frac, message)


def _ensure_running(cancel: CancelCb | None) -> None:
    if cancel is not None and cancel():
        raise PixpackError("已取消")


def _log(log: LogCb | None, message: str) -> None:
    if log is not None:
        log(message)
    else:
        print(message, file=sys.stderr)


def _span(progress: ProgressCb | None, start: float, end: float) -> ProgressCb:
    def inner(frac: float, message: str) -> None:
        if frac < 0:
            _emit(progress, frac, message)
        else:
            _emit(progress, start + (end - start) * frac, message)

    return inner


class _Ticker:
    """限制进度刷新频率，避免几十万次回调把界面堵住。"""

    def __init__(self, interval: float = 0.15) -> None:
        self.interval = interval
        self.last = 0.0

    def due(self, force: bool = False) -> bool:
        now = time.monotonic()
        if force or now - self.last >= self.interval:
            self.last = now
            return True
        return False


def _fmt_count(value: int) -> str:
    return f"{value:,}"


def _fmt_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


# 界面只提供这些边长。再往上即使机器还有内存，单张图也没有必要更大。
_SIDE_CHOICES = (1024, 2048, 4096, 8192, 16384, 32768)
_MIN_SAFE_SIDE = 1024


def _darwin_sysctl(name: bytes) -> int:
    """读取 macOS sysctl 整型。失败时返回 0。"""
    import ctypes
    import ctypes.util

    libc_path = ctypes.util.find_library("c")
    if not libc_path:
        return 0
    libc = ctypes.CDLL(libc_path, use_errno=True)
    libc.sysctlbyname.argtypes = [
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.c_void_p,
        ctypes.c_size_t,
    ]
    libc.sysctlbyname.restype = ctypes.c_int
    raw = ctypes.create_string_buffer(8)
    size = ctypes.c_size_t(len(raw))
    if libc.sysctlbyname(name, raw, ctypes.byref(size), None, 0) != 0:
        return 0
    width = int(size.value)
    if width <= 0 or width > 8:
        return 0
    return int.from_bytes(raw.raw[:width], "little")


def _darwin_available_memory() -> int:
    """当前可立刻使用的内存。空闲页和可回收的投机页都算上。"""
    page = _darwin_sysctl(b"hw.pagesize") or 4096
    free_pages = _darwin_sysctl(b"vm.page_free_count")
    speculative = _darwin_sysctl(b"vm.page_speculative_count")
    avail = (free_pages + speculative) * page
    total = _darwin_sysctl(b"hw.memsize")
    if total > 0:
        avail = min(avail, total)
    return max(0, avail)


def _available_memory() -> int:
    """当前还能安全提交的字节数。取物理可用、提交限制和虚拟地址空间里最小的那个。"""
    if os.name == "nt":
        import ctypes

        class _MemoryStatus(ctypes.Structure):
            _fields_ = (
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            )

        status = _MemoryStatus()
        status.dwLength = ctypes.sizeof(status)
        kernel32 = ctypes.windll.kernel32
        kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MemoryStatus)]
        kernel32.GlobalMemoryStatusEx.restype = ctypes.c_int
        if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return 0
        return int(min(status.ullAvailPhys, status.ullAvailPageFile, status.ullAvailVirtual))
    if sys.platform == "darwin":
        avail = _darwin_available_memory()
        if avail > 0:
            return avail
    try:
        page = os.sysconf("SC_PAGE_SIZE")
        avail = os.sysconf("SC_AVPHYS_PAGES") * page
    except (AttributeError, OSError, ValueError):
        return 0
    return max(0, int(avail))


def max_safe_side() -> int:
    """本机此刻允许的最大边长。

    读一张图时，原图、解码缓冲区和像素拷贝会同时留着，大约是三份 RGB。
    这三份加起来不得超过当前剩余内存的四分之一，避免把 Windows 页面文件或 macOS 内存打满。
    """
    avail = _available_memory()
    if avail <= 0:
        return 4096
    raw_budget = avail // 12
    side = int(math.isqrt(raw_budget // 3)) if raw_budget >= 3 else 0
    allowed = [item for item in _SIDE_CHOICES if item <= side]
    if not allowed:
        return _MIN_SAFE_SIDE
    return allowed[-1]


def side_choices() -> list[int]:
    """界面可选的边长，全部不超过本机限制。"""
    limit = max_safe_side()
    return [item for item in _SIDE_CHOICES if item <= limit]


def default_max_side() -> int:
    choices = side_choices()
    if 4096 in choices:
        return 4096
    return choices[-1]


def side_limit_hint() -> str:
    avail = _available_memory()
    limit = max_safe_side()
    mem = _fmt_bytes(avail) if avail else "未知"
    return (
        f"本机当前约剩 {mem} 可用，边长只能选到 {limit}"
        f"（单张约 {_fmt_bytes(limit * limit * 3)}）。更大可能把系统内存打满。"
    )


def _check_max_side(max_side: int) -> None:
    if max_side < 16:
        raise PixpackError("最大边长至少为 16")
    limit = max_safe_side()
    if max_side > limit:
        avail = _available_memory()
        mem = _fmt_bytes(avail) if avail else "未知"
        raise PixpackError(
            f"最大边长不能超过 {limit}。本机当前约剩 {mem} 可用，"
            f"{max_side} 会让单张图片大到把内存打满。"
        )


def _fs(path: str) -> str:
    """Windows 上启用长路径，避免上万层/超长目录直接失败。"""
    if os.name != "nt":
        return path
    path = os.path.abspath(path)
    if path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


# 这些格式本身已经压缩过，再走 deflate 几乎不变小，但会把海量文件拖慢一个数量级。
_STORE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".bmp", ".ico",
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".mp3", ".ogg", ".flac", ".wav",
    ".zip", ".7z", ".rar", ".gz", ".bz2", ".xz", ".zst", ".lz4",
    ".docx", ".xlsx", ".xlsm", ".pptx", ".pdf", ".pyc", ".dll", ".exe",
    ".woff", ".woff2", ".otf", ".ttf",
}


_TEXT_EXTENSIONS = {
    ".txt", ".csv", ".tsv", ".json", ".xml", ".html", ".htm", ".md", ".log",
    ".py", ".cs", ".js", ".ts", ".css", ".svg", ".sql", ".yml", ".yaml",
    ".ini", ".cfg", ".lua", ".java", ".cpp", ".c", ".h", ".hpp", ".vue",
    ".tsx", ".jsx", ".rb", ".go", ".rs", ".php", ".bat", ".ps1", ".sh",
}


def _compression_ratios(level: int) -> tuple[float, float]:
    """返回 (文本, 其他未压缩文件) 的估算压缩后/原始比例。取值偏大，避免少估张数。"""
    if level >= 9:
        return 0.40, 0.82
    if level >= 6:
        return 0.48, 0.88
    return 0.62, 0.94


def _store_file(name: str, level: int, store_compressed: bool) -> bool:
    if level <= 0:
        return True
    if not store_compressed:
        return False
    return os.path.splitext(name)[1].lower() in _STORE_EXTENSIONS


def _skipped(path: str, skip_exact: str | None, skip_prefix: str | None) -> bool:
    if skip_exact is None:
        return False
    folded = os.path.normcase(path)
    return folded == skip_exact or (skip_prefix is not None and folded.startswith(skip_prefix))


def _sha256_file(
    path: Path,
    progress: ProgressCb | None = None,
    cancel: CancelCb | None = None,
) -> bytes:
    total = path.stat().st_size or 1
    done = 0
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            _ensure_running(cancel)
            digest.update(block)
            done += len(block)
            _emit(progress, done / total, "正在校验压缩包…")
    _emit(progress, 1.0, "压缩包校验完成")
    return digest.digest()


def _split_sizes(total: int, parts: int | None, max_chunk: int) -> list[int]:
    if total < 0:
        raise PixpackError("压缩包大小非法")
    if parts is not None:
        if parts < 1:
            raise PixpackError("--parts 必须大于 0")
        if total == 0:
            return [0]
        if parts > total:
            raise PixpackError(f"数据只有 {total} 字节，无法分成 {parts} 张没有空图的图片")
        base, rem = divmod(total, parts)
        sizes = [base + (1 if i < rem else 0) for i in range(parts)]
    else:
        if max_chunk < 1:
            raise PixpackError("单张图片容量太小，请增大 --max-side")
        sizes = []
        left = total
        while left:
            take = min(max_chunk, left)
            sizes.append(take)
            left -= take
        if not sizes:
            sizes = [0]
    for size in sizes:
        if size > max_chunk:
            raise PixpackError(
                f"有一块数据 {size} 字节，超过单张图片容量 {max_chunk} 字节。"
                "请增大 --max-side，或增加 --parts 让每张图更小。"
            )
    return sizes


def _image_size(nbytes: int, max_side: int) -> tuple[int, int]:
    pixels = max(1, math.ceil(nbytes / 3))
    side = max(1, math.ceil(math.sqrt(pixels)))
    width = min(max_side, side)
    height = math.ceil(pixels / width)
    if width > max_side or height > max_side:
        raise PixpackError("图片尺寸超过 --max-side，请增大边长或增加张数")
    return width, height


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _iter_tree(root: str, skip_exact: str | None, skip_prefix: str | None, cancel: CancelCb | None):
    """深度优先遍历。不调用 resolve，只用 scandir 缓存的目录项信息。root 也可以是单个文件。"""
    if os.path.isfile(root):
        _ensure_running(cancel)
        try:
            size = os.path.getsize(_fs(root))
        except OSError:
            size = -1
        yield ("file", root, os.path.basename(root), size)
        return
    stack = [(root, "")]
    while stack:
        _ensure_running(cancel)
        current, rel = stack.pop()
        yield ("enter", current, rel)
        subdirs: list[tuple[str, str]] = []
        files: list[tuple[str, int]] = []
        try:
            with os.scandir(_fs(current)) as iterator:
                for entry in iterator:
                    try:
                        if entry.is_symlink():
                            yield ("link", entry.name)
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            logical = os.path.join(current, entry.name)
                            if _skipped(logical, skip_exact, skip_prefix):
                                continue
                            subdirs.append((logical, entry.name))
                        elif entry.is_file(follow_symlinks=False):
                            try:
                                size = entry.stat(follow_symlinks=False).st_size
                            except OSError:
                                size = -1
                            files.append((entry.name, size))
                    except OSError:
                        continue
        except OSError as exc:
            yield ("error", current, str(exc))
            continue
        if rel and not subdirs and not files:
            yield ("empty", rel)
        for logical, name in reversed(subdirs):
            child_rel = f"{rel}/{name}" if rel else name
            stack.append((logical, child_rel))
        for name, size in files:
            arcname = f"{rel}/{name}" if rel else name
            yield ("file", os.path.join(current, name), arcname, size)


def _count_tree(
    root: str,
    skip_exact: str | None,
    skip_prefix: str | None,
    progress: ProgressCb | None,
    cancel: CancelCb | None,
    log: LogCb | None,
) -> tuple[int, int, int]:
    files = 0
    dirs = 0
    links = 0
    total_bytes = 0
    ticker = _Ticker()
    _emit(progress, -1.0, "正在统计文件数量…")
    for kind, *rest in _iter_tree(root, skip_exact, skip_prefix, cancel):
        if kind == "enter":
            dirs += 1
        elif kind == "file":
            files += 1
            size = rest[2] if len(rest) > 2 and isinstance(rest[2], int) else 0
            if size > 0:
                total_bytes += size
        elif kind == "link":
            links += 1
        elif kind == "error":
            _log(log, f"无法读取目录: {rest[0]} ({rest[1]})")
        if ticker.due():
            _emit(
                progress,
                -1.0,
                "正在统计… "
                f"已发现 {_fmt_count(files)} 个文件，{_fmt_count(dirs)} 个文件夹"
                f"，{_fmt_bytes(total_bytes)}",
            )
    if links:
        _log(log, f"已跳过 {links} 个符号链接")
    _emit(
        progress,
        -1.0,
        f"统计完成：{_fmt_count(files)} 个文件，{_fmt_count(dirs)} 个文件夹，{_fmt_bytes(total_bytes)}",
    )
    return files, dirs, total_bytes


def _pack_progress(
    written: int,
    total_files: int,
    byte_count: int,
    total_bytes: int,
    started: float,
    name: str,
) -> tuple[float, str]:
    """打包阶段的进度。大文件按字节走，不能等整个文件写完才动一格。"""
    if total_bytes > 0:
        frac = min(1.0, byte_count / total_bytes)
    elif total_files:
        frac = min(1.0, written / total_files)
    else:
        frac = 1.0
    elapsed = max(time.monotonic() - started, 0.001)
    text = "正在打包 "
    if name:
        text += f"{name} · "
    text += f"{_fmt_count(written)}/{_fmt_count(total_files)} 个文件 · {_fmt_bytes(byte_count)}"
    if total_bytes > 0:
        text += f" / {_fmt_bytes(total_bytes)}"
    text += f" · {_fmt_bytes(int(byte_count / elapsed))}/秒"
    return frac, text


def _write_zip_file(
    archive: zipfile.ZipFile,
    logical: str,
    arcname: str,
    size: int,
    level: int,
    store_compressed: bool,
    cancel: CancelCb | None,
    on_bytes,
) -> None:
    name = arcname.rsplit("/", 1)[-1]
    info = zipfile.ZipInfo(filename=arcname)
    info.compress_type = (
        zipfile.ZIP_STORED if _store_file(name, level, store_compressed) else zipfile.ZIP_DEFLATED
    )
    # 只有可能超过 4GB 的文件才强制 ZIP64，避免每个小文件都写扩展头。
    force_zip64 = size < 0 or size >= 0xFFFFFFFF
    with archive.open(info, "w", force_zip64=force_zip64) as dest, open(_fs(logical), "rb") as source:
        while True:
            _ensure_running(cancel)
            block = source.read(4 * 1024 * 1024)
            if not block:
                break
            dest.write(block)
            on_bytes(len(block))


def _zip_name(arcname: str) -> bytes:
    data = arcname.replace("\\", "/").encode("utf-8")
    if len(data) > 65535:
        raise PixpackError(f"路径过长，无法放入压缩包: {arcname}")
    return data


def _stored_local(name: bytes, crc: int, size: int) -> bytes:
    if size >= 0xFFFFFFFF:
        extra = struct.pack("<HHQQ", 0x0001, 16, size, size)
        stored = 0xFFFFFFFF
        version = 45
    else:
        extra = b""
        stored = size
        version = 20
    header = struct.pack(
        "<IHHHHHIIIHH",
        0x04034B50,
        version,
        0x0800,
        0,
        0,
        0,
        crc & 0xFFFFFFFF,
        stored,
        stored,
        len(name),
        len(extra),
    )
    return header + name + extra


def _stored_central(name: bytes, crc: int, size: int, offset: int, external: int) -> bytes:
    size64 = size >= 0xFFFFFFFF
    offset64 = offset >= 0xFFFFFFFF
    body = b""
    if size64:
        body += struct.pack("<QQ", size, size)
    if offset64:
        body += struct.pack("<Q", offset)
    extra = struct.pack("<HH", 0x0001, len(body)) + body if body else b""
    version = 45 if extra else 20
    header = struct.pack(
        "<IHHHHHHIIIHHHHHII",
        0x02014B50,
        (3 << 8) | version,
        version,
        0x0800,
        0,
        0,
        0,
        crc & 0xFFFFFFFF,
        0xFFFFFFFF if size64 else size,
        0xFFFFFFFF if size64 else size,
        len(name),
        len(extra),
        0,
        0,
        0,
        external & 0xFFFFFFFF,
        0xFFFFFFFF if offset64 else offset,
    )
    return header + name + extra


def _write_stored_zip(
    zip_path: Path,
    root: str,
    skip_exact: str | None,
    skip_prefix: str | None,
    total_files: int,
    total_bytes: int,
    progress: ProgressCb | None,
    cancel: CancelCb | None,
    log: LogCb | None,
) -> int:
    """顺序写入只存储的 zip。小文件不再来回 seek，适合几十万个文件。"""
    central = bytearray()
    written = 0
    byte_count = 0
    offset = 0
    current_name = ""
    started = time.monotonic()
    ticker = _Ticker()
    comment = b"pixpack-v1"

    def publish(force: bool = False) -> None:
        if not ticker.due(force):
            return
        frac, text = _pack_progress(
            written, total_files, byte_count, total_bytes, started, current_name
        )
        _emit(progress, frac, text)

    _emit(progress, 0.0, f"开始打包 {_fmt_count(total_files)} 个文件…")
    with open(zip_path, "wb", buffering=8 * 1024 * 1024) as handle:
        for kind, *rest in _iter_tree(root, skip_exact, skip_prefix, cancel):
            if kind == "empty":
                name = _zip_name(rest[0] + "/")
                local = _stored_local(name, 0, 0)
                handle.write(local)
                central += _stored_central(name, 0, 0, offset, 0o40755 << 16)
                offset += len(local)
            elif kind == "error":
                _log(log, f"无法读取目录: {rest[0]} ({rest[1]})")
            elif kind != "file":
                continue
            else:
                logical, arcname, size = rest
                _ensure_running(cancel)
                name = _zip_name(arcname)
                current_name = arcname.rsplit("/", 1)[-1]
                if size >= 8 * 1024 * 1024:
                    publish(True)
                if size < 0 or size <= 1024 * 1024:
                    with open(_fs(logical), "rb") as source:
                        data = source.read() if size < 0 else source.read(size)
                    crc = zlib.crc32(data) & 0xFFFFFFFF if data else 0
                    local = _stored_local(name, crc, len(data))
                    handle.write(local)
                    handle.write(data)
                    central += _stored_central(name, crc, len(data), offset, 0)
                    offset += len(local) + len(data)
                    byte_count += len(data)
                else:
                    local = _stored_local(name, 0, size)
                    crc_at = offset + 14
                    handle.write(local)
                    offset += len(local)
                    crc = 0
                    remaining = size
                    with open(_fs(logical), "rb") as source:
                        while remaining:
                            _ensure_running(cancel)
                            block = source.read(min(4 * 1024 * 1024, remaining))
                            if not block:
                                break
                            remaining -= len(block)
                            crc = zlib.crc32(block, crc)
                            handle.write(block)
                            offset += len(block)
                            byte_count += len(block)
                            publish()
                    crc &= 0xFFFFFFFF
                    if remaining:
                        raise PixpackError(f"读取文件时提前结束: {arcname}")
                    handle.flush()
                    here = handle.tell()
                    handle.seek(crc_at)
                    handle.write(struct.pack("<I", crc))
                    handle.flush()
                    handle.seek(here)
                    central += _stored_central(name, crc, size, crc_at - 14, 0)
                written += 1
                publish()
        publish(True)
        _emit(progress, 1.0, f"正在保存 {_fmt_count(written)} 个文件的索引…")
        cd_offset = offset
        handle.write(central)
        cd_size = len(central)
        need64 = written > 0xFFFF or cd_size >= 0xFFFFFFFF or cd_offset >= 0xFFFFFFFF
        if need64:
            record = struct.pack("<HHIIQQQQ", 45, 45, 0, 0, written, written, cd_size, cd_offset)
            handle.write(struct.pack("<IQ", 0x06064B50, len(record)))
            handle.write(record)
            handle.write(struct.pack("<IIQI", 0x07064B50, 0, cd_offset + cd_size, 1))
        handle.write(
            struct.pack(
                "<IHHHHIIH",
                0x06054B50,
                0,
                0,
                min(written, 0xFFFF),
                min(written, 0xFFFF),
                min(cd_size, 0xFFFFFFFF),
                min(cd_offset, 0xFFFFFFFF),
                len(comment),
            )
        )
        handle.write(comment)
    _emit(progress, 1.0, "文件打包完成")
    return written


def build_zip(
    src: Path,
    zip_path: Path,
    level: int,
    skip_root: Path | None,
    progress: ProgressCb | None = None,
    cancel: CancelCb | None = None,
    log: LogCb | None = None,
    store_compressed: bool = True,
) -> int:
    """把目录写成 zip。返回写入的文件数（不含纯目录项）。"""
    root = os.path.normpath(os.path.abspath(src))
    skip_exact = None
    skip_prefix = None
    if skip_root is not None:
        skip_exact = os.path.normcase(os.path.normpath(os.path.abspath(skip_root)))
        skip_prefix = skip_exact + os.sep
    total_files, total_dirs, total_bytes = _count_tree(
        root, skip_exact, skip_prefix, progress, cancel, log
    )
    if log is not None:
        log(
            f"统计完成：{_fmt_count(total_files)} 个文件，{_fmt_count(total_dirs)} 个文件夹，"
            f"{_fmt_bytes(total_bytes)}"
        )
    if level <= 0:
        return _write_stored_zip(
            zip_path,
            root,
            skip_exact,
            skip_prefix,
            total_files,
            total_bytes,
            progress,
            cancel,
            log,
        )
    written = 0
    byte_count = 0
    current_name = ""
    started = time.monotonic()
    ticker = _Ticker()

    def publish(force: bool = False) -> None:
        if not ticker.due(force):
            return
        frac, text = _pack_progress(
            written, total_files, byte_count, total_bytes, started, current_name
        )
        _emit(progress, frac, text)

    def on_bytes(size: int) -> None:
        nonlocal byte_count
        byte_count += size
        publish()

    _emit(progress, 0.0, f"开始打包 {_fmt_count(total_files)} 个文件…")
    with zipfile.ZipFile(
        zip_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=level if level > 0 else 1,
        allowZip64=True,
    ) as archive:
        archive.comment = b"pixpack-v1"
        for kind, *rest in _iter_tree(root, skip_exact, skip_prefix, cancel):
            if kind == "empty":
                info = zipfile.ZipInfo(rest[0] + "/")
                info.external_attr = (0o40755) << 16
                archive.writestr(info, b"")
            elif kind == "link":
                continue
            elif kind == "error":
                _log(log, f"无法读取目录: {rest[0]} ({rest[1]})")
            elif kind == "file":
                logical, arcname, size = rest
                current_name = arcname.rsplit("/", 1)[-1]
                if size >= 8 * 1024 * 1024:
                    publish(True)
                _write_zip_file(
                    archive, logical, arcname, size, level, store_compressed, cancel, on_bytes
                )
                written += 1
                publish()
        publish(force=True)
        _emit(progress, 1.0, f"正在保存 {_fmt_count(written)} 个文件的索引…")
    _emit(progress, 1.0, "文件打包完成")
    return written


def _pack_header(
    index: int,
    count: int,
    offset: int,
    total: int,
    chunk_size: int,
    crc: int,
    digest: bytes,
    flags: int = 0,
) -> bytes:
    return HEADER.pack(
        MAGIC,
        VERSION,
        index,
        count,
        flags & 0xFFFF,
        offset,
        total,
        chunk_size,
        crc & 0xFFFFFFFF,
        digest,
    )


def _iter_exact(handle, size: int, cancel: CancelCb | None, block: int = 1024 * 1024):
    """按块读出恰好 size 字节。读完时文件位置前进 size。"""
    remaining = size
    while remaining:
        _ensure_running(cancel)
        data = handle.read(min(block, remaining))
        if not data:
            raise PixpackError("读取压缩包时提前结束")
        remaining -= len(data)
        yield data


def _crc32_exact(
    handle,
    size: int,
    cancel: CancelCb | None,
    on_bytes: Callable[[int], None] | None = None,
) -> int:
    crc = 0
    done = 0
    for data in _iter_exact(handle, size, cancel):
        crc = zlib.crc32(data, crc)
        done += len(data)
        if on_bytes is not None:
            on_bytes(done)
    return crc & 0xFFFFFFFF


def _header_then_file(header: bytes, handle, size: int, cancel: CancelCb | None):
    yield header
    yield from _iter_exact(handle, size, cancel)


_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _png_chunk(fp, tag: bytes, data: bytes) -> None:
    fp.write(struct.pack(">I", len(data)))
    fp.write(tag)
    fp.write(data)
    fp.write(struct.pack(">I", zlib.crc32(data, zlib.crc32(tag)) & 0xFFFFFFFF))


def _write_rgb_png(
    path: Path,
    width: int,
    height: int,
    blocks,
    *,
    payload_size: int,
    cancel: CancelCb | None = None,
    on_bytes: Callable[[int], None] | None = None,
) -> None:
    """按行把 RGB 字节流写成 PNG。只保留一行，不再做第二次压缩。

    像素里装的是 zip。这些字节几乎压不动，再用 zlib 扫一整张图既占内存又容易在
    写到后半段时把进程打崩。PNG 这里只用存储块把字节原样包进去。
    """
    row_size = width * 3
    expected = row_size * height
    if row_size <= 0 or payload_size > expected:
        raise PixpackError("像素数据超出图片容量")
    # memLevel=1：窗口只要一行。level=0：存储，不再对 zip 字节做 deflate。
    compressor = zlib.compressobj(level=0, memLevel=1)
    row = bytearray(row_size)
    have = 0
    filled = 0
    reported = 0

    def emit(data: bytes | memoryview) -> None:
        view = memoryview(data)
        for start in range(0, len(view), 65536):
            _png_chunk(fp, b"IDAT", view[start : start + 65536])

    def note(force: bool = False) -> None:
        nonlocal reported
        if on_bytes is None:
            return
        if force or filled - reported >= 1024 * 1024:
            reported = filled
            on_bytes(filled)

    def push_row() -> None:
        nonlocal have
        out = compressor.compress(b"\x00")
        if out:
            emit(out)
        out = compressor.compress(row)
        if out:
            emit(out)
        have = 0

    def feed(data: bytes) -> None:
        nonlocal have, filled
        if not data:
            return
        if filled + len(data) > expected:
            raise PixpackError("像素数据超出图片容量")
        view = memoryview(data)
        pos = 0
        while pos < len(view):
            _ensure_running(cancel)
            take = min(row_size - have, len(view) - pos)
            row[have : have + take] = view[pos : pos + take]
            have += take
            pos += take
            filled += take
            if have == row_size:
                push_row()
            note()

    with open(_fs(str(path)), "wb", buffering=1024 * 1024) as fp:
        fp.write(_PNG_SIG)
        _png_chunk(fp, b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        for block in blocks:
            feed(block)
        if filled != payload_size:
            raise PixpackError("读取压缩包时提前结束")
        remain = expected - filled
        zeros = bytes(min(row_size, 1024 * 1024))
        while remain:
            _ensure_running(cancel)
            take = zeros if remain >= len(zeros) else zeros[:remain]
            feed(take)
            remain -= len(take)
        if have:
            raise PixpackError("图片行没有写完")
        tail = compressor.flush()
        if tail:
            emit(tail)
        note(True)
        _png_chunk(fp, b"IEND", b"")
        del compressor


def encode_blob(
    blob_path: Path,
    out_dir: Path,
    *,
    max_side: int,
    parts: int | None,
    prefix: str,
    flags: int = 0,
    progress: ProgressCb | None = None,
    cancel: CancelCb | None = None,
    log: LogCb | None = None,
) -> list[Path]:
    total = blob_path.stat().st_size
    digest = _sha256_file(blob_path, progress=_span(progress, 0.0, 0.12), cancel=cancel)
    max_chunk = max_side * max_side * 3 - HEADER_SIZE
    sizes = _split_sizes(total, parts, max_chunk)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    offset = 0
    image_count = len(sizes)
    width_count = len(str(image_count))
    with blob_path.open("rb") as handle:
        for index, chunk_size in enumerate(sizes):
            _ensure_running(cancel)
            begin = 0.12 + 0.88 * (index / image_count)
            end = 0.12 + 0.88 * ((index + 1) / image_count)
            _emit(progress, begin, f"正在生成第 {index + 1}/{image_count} 张图片…")
            width, height = _image_size(HEADER_SIZE + chunk_size, max_side)
            payload_size = HEADER_SIZE + chunk_size
            mid = begin + (end - begin) * 0.2

            def image_progress(done: int, start: float = begin, stop: float = mid, total_bytes: int = chunk_size) -> None:
                frac = start + (stop - start) * (done / total_bytes if total_bytes else 1)
                _emit(
                    progress,
                    frac,
                    f"正在校验第 {index + 1}/{image_count} 张… {_fmt_bytes(done)} / {_fmt_bytes(total_bytes)}",
                )

            handle.seek(offset)
            crc = _crc32_exact(handle, chunk_size, cancel, on_bytes=image_progress)
            header = _pack_header(
                index, image_count, offset, total, chunk_size, crc, digest, flags
            )
            name = f"{prefix}_{index + 1:0{width_count}d}_of_{image_count:0{width_count}d}.png"
            dest = out_dir / name
            handle.seek(offset)

            def write_progress(done: int, start: float = mid, stop: float = end, total_bytes: int = payload_size) -> None:
                frac = start + (stop - start) * min(1.0, done / total_bytes if total_bytes else 1)
                _emit(
                    progress,
                    frac,
                    f"正在写入第 {index + 1}/{image_count} 张图片… {_fmt_bytes(min(done, total_bytes))} / {_fmt_bytes(total_bytes)}",
                )

            _write_rgb_png(
                dest,
                width,
                height,
                _header_then_file(header, handle, chunk_size, cancel),
                payload_size=payload_size,
                cancel=cancel,
                on_bytes=write_progress,
            )
            written.append(dest)
            line = f"写入 {dest.name}  {width}x{height}  本张数据 {chunk_size} 字节"
            if log is not None:
                log(line)
            else:
                print(line)
            _emit(progress, end, f"已写入 {dest.name}")
            offset += chunk_size
    if offset != total:
        raise PixpackError("写出的字节数与压缩包不一致")
    return written


def _parse_header(raw: bytes) -> dict:
    """只解析文件头。不把后面的数据读进内存。"""
    if len(raw) < HEADER_SIZE:
        raise PixpackError("图片数据太短，不像 pixpack 图片")
    magic, version, index, count, flags, offset, total, chunk_size, crc, digest = HEADER.unpack(
        raw[:HEADER_SIZE]
    )
    if magic != MAGIC:
        raise PixpackError("文件头不是 PX01，这不是 pixpack 图片")
    if version != VERSION:
        raise PixpackError(f"不支持的版本: {version}")
    if count < 1 or index >= count:
        raise PixpackError(f"图片序号非法: {index + 1}/{count}")
    if chunk_size < 0:
        raise PixpackError("图片里声明的数据长度非法")
    return {
        "index": index,
        "count": count,
        "flags": flags,
        "offset": offset,
        "total": total,
        "chunk_size": chunk_size,
        "crc": crc,
        "sha256": digest,
    }


def _paeth(left: int, up: int, up_left: int) -> int:
    estimate = left + up - up_left
    pa = abs(estimate - left)
    pb = abs(estimate - up)
    pc = abs(estimate - up_left)
    if pa <= pb and pa <= pc:
        return left
    if pb <= pc:
        return up
    return up_left


def _unfilter_row(filter_type: int, row: bytearray, prev: bytearray) -> None:
    """把 PNG 扫描行还原成 RGB。row 不含滤波字节，就地修改。"""
    bpp = 3
    size = len(row)
    if filter_type == 0:
        return
    if filter_type == 1:
        for i in range(bpp, size):
            row[i] = (row[i] + row[i - bpp]) & 0xFF
        return
    if filter_type == 2:
        for i in range(size):
            row[i] = (row[i] + prev[i]) & 0xFF
        return
    if filter_type == 3:
        for i in range(size):
            left = row[i - bpp] if i >= bpp else 0
            row[i] = (row[i] + ((left + prev[i]) >> 1)) & 0xFF
        return
    if filter_type == 4:
        for i in range(size):
            left = row[i - bpp] if i >= bpp else 0
            up_left = prev[i - bpp] if i >= bpp else 0
            row[i] = (row[i] + _paeth(left, prev[i], up_left)) & 0xFF
        return
    raise PixpackError(f"不支持的 PNG 滤波类型: {filter_type}")


def _read_png_chunk(handle) -> tuple[bytes, bytes] | None:
    head = handle.read(8)
    if not head:
        return None
    if len(head) != 8:
        raise PixpackError("PNG 不完整")
    length, tag = struct.unpack(">I4s", head)
    if length > 32 * 1024 * 1024:
        raise PixpackError("PNG 数据块过大")
    data = handle.read(length)
    crc_raw = handle.read(4)
    if len(data) != length or len(crc_raw) != 4:
        raise PixpackError("PNG 不完整")
    expect = zlib.crc32(data, zlib.crc32(tag)) & 0xFFFFFFFF
    actual = struct.unpack(">I", crc_raw)[0]
    if expect != actual:
        raise PixpackError("PNG 数据块校验失败，文件可能已损坏")
    return tag, data


def _iter_rgb_rows(path: Path):
    """按行产出还原后的 RGB，不把整张图放进内存。"""
    with open(_fs(str(path)), "rb") as handle:
        if handle.read(8) != _PNG_SIG:
            raise PixpackError(f"{path.name} 不是 PNG")
        width = height = 0
        row_stride = 0
        prev = bytearray()
        pending = bytearray()
        produced = 0
        decompressor = zlib.decompressobj()

        def drain():
            nonlocal produced
            while row_stride and len(pending) >= row_stride and produced < height:
                filter_type = pending[0]
                row = bytearray(pending[1:row_stride])
                del pending[:row_stride]
                _unfilter_row(filter_type, row, prev)
                prev[:] = row
                produced += 1
                yield row

        while True:
            item = _read_png_chunk(handle)
            if item is None:
                break
            tag, data = item
            if tag == b"IHDR":
                if len(data) < 13:
                    raise PixpackError(f"{path.name} 的 PNG 头不完整")
                width, height, bit_depth, color_type, _comp, _filt, interlace = struct.unpack(
                    ">IIBBBBB", data[:13]
                )
                if bit_depth != 8 or color_type != 2 or interlace != 0:
                    raise PixpackError(
                        f"{path.name} 不是 8 位 RGB PNG，图片可能被转换过，无法安全解码。"
                    )
                if width <= 0 or height <= 0:
                    raise PixpackError(f"{path.name} 的尺寸非法")
                row_stride = 1 + width * 3
                prev = bytearray(width * 3)
            elif tag == b"IDAT":
                if row_stride == 0:
                    raise PixpackError(f"{path.name} 缺少 PNG 头")
                src = data
                while src:
                    piece = decompressor.decompress(src, max_length=64 * 1024)
                    src = decompressor.unconsumed_tail
                    if piece:
                        pending.extend(piece)
                        yield from drain()
                    if not piece:
                        break
                if produced >= height:
                    return
            elif tag == b"IEND":
                break
        tail = decompressor.flush()
        if tail:
            pending.extend(tail)
            yield from drain()
        if produced != height:
            raise PixpackError(f"{path.name} 的像素不完整")


def _read_meta(path: Path) -> dict:
    """只读每张图开头的 PX01 头，不解码其余像素。"""
    buf = bytearray()
    try:
        for row in _iter_rgb_rows(path):
            need = HEADER_SIZE - len(buf)
            buf.extend(row[:need])
            if len(buf) >= HEADER_SIZE:
                break
    except PixpackError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PixpackError(f"无法读取 {path.name}: {exc}") from exc
    frame = _parse_header(bytes(buf))
    frame["path"] = path
    return frame


def read_image(path: Path) -> dict:
    return _read_meta(path)


def collect_images(inputs: list[Path]) -> list[Path]:
    found: list[Path] = []
    for item in inputs:
        if item.is_dir():
            found.extend(sorted(p for p in item.iterdir() if p.suffix.lower() == ".png" and p.is_file()))
        elif item.is_file():
            found.append(item)
        else:
            raise PixpackError(f"找不到: {item}")
    if not found:
        raise PixpackError("没有找到 PNG 图片")
    # 去重并保持稳定顺序；真正的顺序由文件头里的 index 决定
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in found:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique


def _collect_parts(
    inputs: list[Path],
    progress: ProgressCb | None = None,
    cancel: CancelCb | None = None,
    log: LogCb | None = None,
) -> list[dict]:
    """读取每张图的文件头。不保存像素。"""
    frames = []
    skipped = []
    paths = collect_images(inputs)
    total = len(paths)
    for index, path in enumerate(paths):
        _ensure_running(cancel)
        _emit(progress, index / total, f"正在读取 {path.name}（{index + 1}/{total}）")
        try:
            frames.append(_read_meta(path))
        except PixpackError as exc:
            skipped.append(f"{path.name}: {exc}")
        _emit(progress, (index + 1) / total, f"已读取 {path.name}")
    if not frames:
        detail = "\n".join(skipped) if skipped else "没有可读图片"
        raise PixpackError(f"没有可用的 pixpack 图片。\n{detail}")
    digests = {frame["sha256"] for frame in frames}
    if len(digests) > 1:
        raise PixpackError("这些图片不是同一组。请分开解码，或只指定同一组文件。")
    frames.sort(key=lambda item: item["index"])
    count = frames[0]["count"]
    payload_total = frames[0]["total"]
    digest = frames[0]["sha256"]
    flags = frames[0]["flags"]
    if len(frames) != count:
        got = [item["index"] + 1 for item in frames]
        raise PixpackError(f"需要 {count} 张图片，实际读到 {len(frames)} 张（序号 {got}）")
    expected_offset = 0
    for order, frame in enumerate(frames):
        if frame["index"] != order:
            raise PixpackError("图片序号不连续，可能缺了其中一张")
        if (
            frame["count"] != count
            or frame["total"] != payload_total
            or frame["sha256"] != digest
            or frame["flags"] != flags
        ):
            raise PixpackError(f"{frame['path'].name} 与其他图片不属于同一组")
        if frame["offset"] != expected_offset:
            raise PixpackError(f"{frame['path'].name} 的数据偏移不正确")
        expected_offset += frame["chunk_size"]
    if expected_offset != payload_total:
        raise PixpackError("拼合后的长度与文件头记录不一致")
    if skipped:
        _log(log, "已跳过不是这一组的图片:\n" + "\n".join(skipped))
    return frames


def _copy_chunk(
    path: Path,
    chunk_size: int,
    output,
    digest: "hashlib._Hash",
    cancel: CancelCb | None,
    on_bytes: Callable[[int], None] | None = None,
) -> int:
    """跳过像素里的文件头，把这一张的数据追加到 zip 文件。返回 CRC。"""
    crc = 0
    skipped = 0
    written = 0
    for row in _iter_rgb_rows(path):
        _ensure_running(cancel)
        view = memoryview(row)
        pos = 0
        if skipped < HEADER_SIZE:
            need = HEADER_SIZE - skipped
            if len(view) <= need:
                skipped += len(view)
                continue
            pos = need
            skipped = HEADER_SIZE
        if written < chunk_size and pos < len(view):
            take = min(chunk_size - written, len(view) - pos)
            block = view[pos : pos + take]
            crc = zlib.crc32(block, crc)
            digest.update(block)
            output.write(block)
            written += take
            if on_bytes is not None:
                on_bytes(written)
        if written >= chunk_size:
            break
    if written != chunk_size:
        raise PixpackError(f"{path.name} 里的数据不完整")
    return crc & 0xFFFFFFFF


def _assemble_zip(
    frames: list[dict],
    dest: Path,
    progress: ProgressCb | None = None,
    cancel: CancelCb | None = None,
) -> None:
    """按顺序把各张图里的数据写成 zip 文件，并核对 CRC 和 SHA-256。"""
    payload_total = frames[0]["total"]
    done = 0
    digest = hashlib.sha256()
    ticker = _Ticker()
    with open(_fs(str(dest)), "wb", buffering=1024 * 1024) as output:
        for frame in frames:
            _ensure_running(cancel)
            base = done

            def on_bytes(written: int, base: int = base, name: str = frame["path"].name) -> None:
                nonlocal done
                done = base + written
                if payload_total and ticker.due(written == frame["chunk_size"]):
                    _emit(
                        progress,
                        done / payload_total,
                        f"正在拼合 {name} · {_fmt_bytes(done)} / {_fmt_bytes(payload_total)}",
                    )

            crc = _copy_chunk(frame["path"], frame["chunk_size"], output, digest, cancel, on_bytes)
            if crc != frame["crc"]:
                raise PixpackError(f"第 {frame['index'] + 1} 张图片 CRC 校验失败，文件可能已损坏或被改过")
    if digest.digest() != frames[0]["sha256"]:
        raise PixpackError("整包 SHA-256 校验失败，数据不完整或已被修改")


def _safe_destination(root: Path, name: str) -> Path:
    pure = name.replace("\\", "/").strip()
    if not pure or pure.endswith("/"):
        raise PixpackError(f"压缩包内路径非法: {name!r}")
    parts = Path(pure).parts
    if pure.startswith("/") or pure.startswith("\\") or ":" in parts[0]:
        raise PixpackError(f"拒绝绝对路径: {name}")
    if any(part in ("..", "") for part in parts):
        raise PixpackError(f"拒绝跨越目录的路径: {name}")
    root_abs = os.path.abspath(root)
    dest_abs = os.path.normpath(os.path.join(root_abs, *parts))
    root_key = os.path.normcase(root_abs)
    dest_key = os.path.normcase(dest_abs)
    if dest_key != root_key and not dest_key.startswith(root_key + os.sep):
        raise PixpackError(f"解压路径逃出目标目录: {name}")
    return Path(dest_abs)


def extract_zip_bytes(
    zip_path: Path,
    dest: Path,
    progress: ProgressCb | None = None,
    cancel: CancelCb | None = None,
) -> int:
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    try:
        archive = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        raise PixpackError("还原出的数据不是有效 zip，图片可能不完整") from exc
    file_count = 0
    made: set[str] = set()
    ticker = _Ticker()
    started = time.monotonic()

    def ensure_dir(folder: Path) -> None:
        key = os.path.normcase(str(folder))
        if key in made:
            return
        os.makedirs(_fs(str(folder)), exist_ok=True)
        made.add(key)

    with archive:
        members = archive.infolist()
        files = [info for info in members if not info.is_dir() and not info.filename.endswith("/")]
        total = max(len(files), 1)
        _emit(progress, 0.0, f"正在还原，共 {_fmt_count(len(files))} 个文件…")
        for info in members:
            name = info.filename
            if not (name.endswith("/") or info.is_dir()):
                continue
            if name in ("./", "."):
                continue
            ensure_dir(_safe_destination(dest, name[:-1] or "."))
        for info in files:
            _ensure_running(cancel)
            name = info.filename
            target = _safe_destination(dest, name)
            ensure_dir(target.parent)
            target_fs = _fs(str(target))
            with archive.open(info, "r") as source, open(target_fs, "wb") as output:
                while True:
                    block = source.read(4 * 1024 * 1024)
                    if not block:
                        break
                    _ensure_running(cancel)
                    output.write(block)
            file_count += 1
            if ticker.due(file_count == len(files)):
                elapsed = max(time.monotonic() - started, 0.001)
                rate = file_count / elapsed
                _emit(
                    progress,
                    file_count / total,
                    "正在还原 "
                    f"{_fmt_count(file_count)}/{_fmt_count(len(files))} 个文件"
                    f" · {rate:,.0f} 文件/秒",
                )
    _emit(progress, 1.0, "文件还原完成")
    return file_count


def extract_one_file(
    zip_path: Path,
    dest: Path,
    progress: ProgressCb | None = None,
    cancel: CancelCb | None = None,
) -> int:
    """把只含一个文件的压缩包直接写成目标文件。"""
    if dest.exists() and dest.is_dir():
        raise PixpackError("还原目标是文件夹。要还原成单个文件，请指定文件路径。")
    try:
        archive = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        raise PixpackError("还原出的数据不是有效 zip，图片可能不完整") from exc
    with archive:
        files = [info for info in archive.infolist() if not info.is_dir() and not info.filename.endswith("/")]
        if len(files) != 1:
            raise PixpackError(
                f"这组图片里有 {len(files)} 个文件，不能直接还原成一个文件。请改选一个文件夹。"
            )
        info = files[0]
        dest.parent.mkdir(parents=True, exist_ok=True)
        total = max(info.file_size, 1)
        done = 0
        ticker = _Ticker()
        _emit(progress, 0.0, f"正在还原 {dest.name}")
        with archive.open(info, "r") as source, open(_fs(str(dest)), "wb") as output:
            while True:
                _ensure_running(cancel)
                block = source.read(4 * 1024 * 1024)
                if not block:
                    break
                output.write(block)
                done += len(block)
                if ticker.due():
                    _emit(progress, done / total, f"正在还原 {dest.name}")
        _emit(progress, 1.0, "文件还原完成")
    return 1


class PreviewResult:
    """打包前的规模估算。极速模式下张数和像素尺寸按文件大小计算。"""

    def __init__(
        self,
        *,
        files: int,
        dirs: int,
        raw_bytes: int,
        payload_bytes: int,
        exact: bool,
        groups: list[tuple[int, int, int, int, int, int]],
        warnings: list[str],
        error: str | None = None,
        mode_label: str = "",
    ) -> None:
        self.files = files
        self.dirs = dirs
        self.raw_bytes = raw_bytes
        self.payload_bytes = payload_bytes
        self.exact = exact
        self.groups = groups
        self.warnings = warnings
        self.error = error
        self.mode_label = mode_label

    @property
    def image_count(self) -> int:
        return sum(count for _, count, *_rest in self.groups)

    @property
    def png_bytes(self) -> int:
        return sum(count * png for _, count, _w, _h, _data, png in self.groups)


def _sample_png_ratio(largest: list[tuple[int, str]], cancel: CancelCb | None) -> float:
    """用最大的几个文件抽样，估计像素写进 PNG 后还能再小多少。"""
    blob = bytearray()
    budget = 512 * 1024
    for _size, logical in largest:
        _ensure_running(cancel)
        if len(blob) >= budget:
            break
        try:
            with open(_fs(logical), "rb") as handle:
                blob += handle.read(min(64 * 1024, budget - len(blob)))
        except OSError:
            continue
    if not blob:
        return 1.0
    compressed = zlib.compress(bytes(blob), 1)
    return min(1.05, max(0.02, len(compressed) / len(blob)))


def _estimate_png_file_size(width: int, height: int, ratio: float = 1.0) -> int:
    """按每行 1 字节滤波估算。ratio 来自内容抽样，1 表示几乎压不小。"""
    raw = height * (width * 3 + 1)
    return 128 + int(raw * min(1.05, max(0.02, ratio)))


def _entry_overhead(arcname: str, size: int) -> int:
    name_len = len(arcname.encode("utf-8"))
    extra = 40 if size >= 0xFFFFFFFF else 0
    return 76 + 2 * name_len + extra


def preview_pack(
    src: Path | str,
    *,
    max_side: int = 4096,
    parts: int | None = None,
    level: int = 0,
    store_compressed: bool = True,
    out_dir: Path | str | None = None,
    mode_label: str = "",
    progress: ProgressCb | None = None,
    cancel: CancelCb | None = None,
) -> PreviewResult:
    """只扫描大小，不写文件。返回大概会生成多少张图、每张多大。"""
    src = Path(src)
    if not src.exists() or not (src.is_file() or src.is_dir()):
        raise PixpackError(f"源文件或目录不存在: {src}")
    _check_max_side(max_side)
    if not 0 <= level <= 9:
        raise PixpackError("压缩级别必须在 0 到 9 之间")
    if parts is not None and parts < 1:
        raise PixpackError("张数至少为 1")
    root = os.path.normpath(os.path.abspath(src))
    skip_exact = None
    skip_prefix = None
    if out_dir and src.is_dir():
        out_abs = os.path.normpath(os.path.abspath(out_dir))
        if os.path.normcase(out_abs).startswith(os.path.normcase(root) + os.sep):
            skip_exact = os.path.normcase(out_abs)
            skip_prefix = skip_exact + os.sep

    files = 0
    dirs = 0
    entries = 0
    raw_bytes = 0
    store_bytes = 0
    text_bytes = 0
    bin_bytes = 0
    overhead = 32  # 结尾目录记录和注释
    largest: list[tuple[int, str]] = []
    ticker = _Ticker()
    _emit(progress, -1.0, "正在预览，统计文件大小…")
    for kind, *rest in _iter_tree(root, skip_exact, skip_prefix, cancel):
        if kind == "enter":
            dirs += 1
        elif kind == "empty":
            entries += 1
            overhead += _entry_overhead(rest[0] + "/", 0)
        elif kind == "file":
            logical, arcname, size = rest
            if size < 0:
                size = 0
            files += 1
            entries += 1
            raw_bytes += size
            overhead += _entry_overhead(arcname, size)
            if size > 0 and (len(largest) < 8 or size > largest[-1][0]):
                largest.append((size, logical))
                largest.sort(reverse=True)
                del largest[8:]
            if _store_file(arcname, level, store_compressed):
                store_bytes += size
            elif os.path.splitext(arcname)[1].lower() in _TEXT_EXTENSIONS:
                text_bytes += size
            else:
                bin_bytes += size
        if ticker.due():
            _emit(
                progress,
                -1.0,
                f"正在预览… 已发现 {_fmt_count(files)} 个文件，{_fmt_bytes(raw_bytes)}",
            )
    stored_ratio = _sample_png_ratio(largest, cancel) if level <= 0 else 1.0
    if entries > 0xFFFF or raw_bytes + overhead >= 0xFFFFFFFF:
        overhead += 76 + entries * 12
    exact = level <= 0
    if exact:
        payload = raw_bytes + overhead
    else:
        text_ratio, bin_ratio = _compression_ratios(level)
        payload = store_bytes + int(text_bytes * text_ratio) + int(bin_bytes * bin_ratio) + overhead
    _emit(progress, 1.0, "预览计算完成")

    warnings: list[str] = []
    groups: list[tuple[int, int, int, int, int, int]] = []
    error = None
    max_chunk = max_side * max_side * 3 - HEADER_SIZE
    try:
        sizes = _split_sizes(payload, parts, max_chunk)
    except PixpackError as exc:
        error = str(exc)
        if max_side < max_safe_side():
            error += " 可以增大最大边长，或增加张数。"
        else:
            error += " 已经是本机允许的最大边长，请增加张数，或留空让程序自动拆开。"
        return PreviewResult(
            files=files,
            dirs=dirs,
            raw_bytes=raw_bytes,
            payload_bytes=payload,
            exact=exact,
            groups=[],
            warnings=warnings,
            error=error,
            mode_label=mode_label,
        )

    index = 1
    for chunk in sizes:
        payload_len = HEADER_SIZE + chunk
        width, height = _image_size(payload_len, max_side)
        png_bytes = _estimate_png_file_size(width, height, stored_ratio if exact else 1.0)
        if groups and groups[-1][2:] == (width, height, chunk, png_bytes):
            start, count, *_rest = groups[-1]
            groups[-1] = (start, count + 1, width, height, chunk, png_bytes)
        else:
            groups.append((index, 1, width, height, chunk, png_bytes))
        index += 1

    image_count = index - 1
    if image_count >= 100:
        warnings.append(f"将生成 {_fmt_count(image_count)} 张图片，数量很多。增大「最大边长」可以明显减少张数。")
    elif image_count >= 20:
        warnings.append(f"将生成 {image_count} 张图片，数量偏多。如果希望更少，可以增大「最大边长」。")
    biggest = max((png for _s, _c, _w, _h, _d, png in groups), default=0)
    if biggest >= 32 * 1024 * 1024:
        warnings.append(
            f"最大的一张约 {_fmt_bytes(biggest)}。觉得太大时，把最大边长改小，就会拆成更多、更小的图片。"
        )
    if parts and image_count < parts:
        warnings.append("指定张数大于可拆出的图片数，实际不会生成空图。")
    return PreviewResult(
        files=files,
        dirs=dirs,
        raw_bytes=raw_bytes,
        payload_bytes=payload,
        exact=exact,
        groups=groups,
        warnings=warnings,
        mode_label=mode_label,
    )


def format_preview(result: PreviewResult) -> str:
    lines = [
        f"文件 {_fmt_count(result.files)} 个，文件夹 {_fmt_count(result.dirs)} 个，原始大小 {_fmt_bytes(result.raw_bytes)}",
    ]
    if result.mode_label:
        lines.append(f"当前速度：{result.mode_label}")
    if result.exact:
        lines.append(f"打包后的数据 {_fmt_bytes(result.payload_bytes)}（极速不压缩，这个大小是按文件算出来的）")
    else:
        lines.append(f"打包后的数据大约 {_fmt_bytes(result.payload_bytes)}（按经验压缩率估算，实际通常更小）")
    if result.error:
        lines.append("")
        lines.append("按当前配置无法生成：")
        lines.append(result.error)
        return "\n".join(lines)
    lines.append(f"将生成 {result.image_count} 张 PNG，合计大约 {_fmt_bytes(result.png_bytes)}")
    lines.append("")
    for start, count, width, height, data_bytes, png_bytes in result.groups:
        if count == 1:
            title = f"第 {start} 张"
        else:
            title = f"第 {start}–{start + count - 1} 张，每张"
        lines.append(
            f"{title}：{width}×{height}，数据 {_fmt_bytes(data_bytes)}，PNG 大约 {_fmt_bytes(png_bytes)}"
        )
    if result.groups and result.image_count > 1:
        _start, _count, width, height, _data, png_bytes = result.groups[0]
        lines.append("")
        lines.append(f"其中最多的一张是 {width}×{height}，大约 {_fmt_bytes(png_bytes)}。")
    lines.append("")
    if result.exact:
        lines.append("张数和像素尺寸已按当前文件确定。PNG 体积按内容抽样估算：重复内容会小很多，图片、视频这类文件会接近像素上限。")
    else:
        lines.append("压缩模式的张数是偏多的估计，实际不会比这里更多。压缩后的数据很难再变小，PNG 体积接近像素上限。")
    if result.warnings:
        lines.append("")
        lines.append("请注意：")
        lines.extend(f"- {item}" for item in result.warnings)
    return "\n".join(lines)


def pack_directory(
    src: Path | str,
    out_dir: Path | str,
    *,
    max_side: int = 4096,
    parts: int | None = None,
    level: int = 6,
    prefix: str = "pixpack",
    store_compressed: bool = True,
    progress: ProgressCb | None = None,
    cancel: CancelCb | None = None,
    log: LogCb | None = None,
) -> tuple[int, list[Path]]:
    """把目录打包成 PNG。返回 (文件数, 图片路径)。"""
    src = Path(src)
    out_dir = Path(out_dir)
    if not src.is_file() and not src.is_dir():
        raise PixpackError(f"源文件或目录不存在: {src}")
    _check_max_side(max_side)
    if not 0 <= level <= 9:
        raise PixpackError("压缩级别必须在 0 到 9 之间")
    prefix = prefix.strip() or "pixpack"
    if prefix != Path(prefix).name or any(ch in prefix for ch in '<>:"/\\|?*'):
        raise PixpackError("文件名前缀不能包含路径或特殊字符")
    src_resolved = src.resolve()
    out_resolved = out_dir.resolve()
    if out_resolved == src_resolved:
        raise PixpackError("输出目录不能就是源文件或源目录")
    skip = out_resolved if src.is_dir() and _is_relative_to(out_resolved, src_resolved) else None
    flags = FLAG_SINGLE_FILE if src.is_file() else 0
    _ensure_running(cancel)
    _emit(progress, 0.0, "正在扫描并压缩文件…")
    _log(log, "来源是单个文件" if flags else "来源是文件夹")
    with tempfile.TemporaryDirectory(prefix="pixpack-") as temp_dir:
        zip_path = Path(temp_dir) / "payload.zip"
        file_count = build_zip(
            src,
            zip_path,
            level,
            skip,
            progress=_span(progress, 0.0, 0.70),
            cancel=cancel,
            log=log,
            store_compressed=store_compressed,
        )
        _ensure_running(cancel)
        images = encode_blob(
            zip_path,
            out_dir,
            max_side=max_side,
            parts=parts,
            prefix=prefix,
            flags=flags,
            progress=_span(progress, 0.70, 1.0),
            cancel=cancel,
            log=log,
        )
    _emit(progress, 1.0, "打包完成")
    return file_count, images


def _is_single_payload(frames: list[dict]) -> bool:
    return bool(frames[0]["flags"] & FLAG_SINGLE_FILE)


def _single_member_name(zip_path: Path) -> str:
    """单个文件的压缩包里，原来的文件名。"""
    try:
        archive = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        raise PixpackError("还原出的数据不是有效 zip，图片可能不完整") from exc
    with archive:
        files = [
            info.filename
            for info in archive.infolist()
            if not info.is_dir() and not info.filename.endswith("/")
        ]
    if len(files) != 1:
        raise PixpackError(f"标记为单个文件，但压缩包里有 {len(files)} 个文件")
    base = Path(files[0].replace("\\", "/")).name
    if not base or base in (".", ".."):
        raise PixpackError("压缩包里的文件名无效")
    return base


def unpack_directory(
    images: Path | str | list[Path | str],
    out_dir: Path | str,
    *,
    progress: ProgressCb | None = None,
    cancel: CancelCb | None = None,
    log: LogCb | None = None,
) -> tuple[int, int]:
    """从 PNG 还原到文件夹。按文件头标记决定写成原文件，还是按原路径展开。返回 (图片张数, 文件数)。"""
    if isinstance(images, (str, Path)):
        image_list = [Path(images)]
    else:
        image_list = [Path(item) for item in images]
    out = Path(out_dir)
    if out.exists() and not out.is_dir():
        raise PixpackError("还原位置必须是文件夹")
    _emit(progress, 0.0, "正在读取图片…")
    frames = _collect_parts(image_list, progress=_span(progress, 0.0, 0.08), cancel=cancel, log=log)
    single = _is_single_payload(frames)
    with tempfile.TemporaryDirectory(prefix="pixpack-unpack-") as temp_dir:
        zip_path = Path(temp_dir) / "payload.zip"
        _emit(progress, 0.08, "正在拼合图片…")
        _assemble_zip(frames, zip_path, progress=_span(progress, 0.08, 0.62), cancel=cancel)
        _ensure_running(cancel)
        _emit(progress, 0.70, "正在还原文件…")
        if single:
            out.mkdir(parents=True, exist_ok=True)
            target = out / _single_member_name(zip_path)
            _log(log, f"来源是单个文件，还原为 {target.name}")
            count = extract_one_file(zip_path, target, progress=_span(progress, 0.70, 1.0), cancel=cancel)
        else:
            _log(log, "来源是文件夹，按原来的路径展开")
            count = extract_zip_bytes(zip_path, out, progress=_span(progress, 0.70, 1.0), cancel=cancel)
    _emit(progress, 1.0, "还原完成")
    return len(frames), count


def cmd_preview(args: argparse.Namespace) -> int:
    result = preview_pack(
        args.src,
        max_side=args.max_side,
        parts=args.parts,
        level=args.level,
        store_compressed=not getattr(args, "no_store_compressed", False),
    )
    print(format_preview(result))
    return 1 if result.error else 0


def cmd_pack(args: argparse.Namespace) -> int:
    file_count, images = pack_directory(
        args.src,
        args.out,
        max_side=args.max_side,
        parts=args.parts,
        level=args.level,
        prefix=args.prefix,
        store_compressed=not getattr(args, "no_store_compressed", False),
    )
    print(f"完成: {file_count} 个文件 -> {len(images)} 张 PNG，输出目录 {Path(args.out).resolve()}")
    return 0


def cmd_unpack(args: argparse.Namespace) -> int:
    if len(args.images) < 2:
        raise PixpackError("用法: pixpack unpack <图片或目录...> <还原目录>")
    out = args.images[-1]
    frame_count, count = unpack_directory(
        [Path(item) for item in args.images[:-1]],
        out,
    )
    print(f"完成: 从 {frame_count} 张图片还原 {count} 个文件到 {Path(out).resolve()}")
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    frames = _collect_parts([Path(item) for item in args.images])
    digest = frames[0]["sha256"].hex()
    print(f"张数: {len(frames)}")
    print(f"来源: {'单个文件' if _is_single_payload(frames) else '文件夹'}")
    print(f"压缩包大小: {frames[0]['total']} 字节")
    print(f"SHA-256: {digest}")
    for frame in frames:
        print(
            f"  [{frame['index'] + 1}/{frame['count']}] {frame['path'].name}"
            f"  偏移 {frame['offset']}  数据 {frame['chunk_size']} 字节"
        )
    with tempfile.TemporaryDirectory(prefix="pixpack-info-") as temp_dir:
        zip_path = Path(temp_dir) / "payload.zip"
        _assemble_zip(frames, zip_path)
        with zipfile.ZipFile(zip_path) as archive:
            names = [info.filename for info in archive.infolist() if not info.is_dir()]
    print(f"包含文件: {len(names)}")
    for name in names:
        print(f"  {name}")
    return 0


def _snapshot(root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        filenames.sort()
        for name in filenames:
            full = Path(dirpath) / name
            rel = full.relative_to(root).as_posix()
            files[rel] = full.read_bytes()
    return files


def cmd_selftest(_: argparse.Namespace) -> int:
    with tempfile.TemporaryDirectory(prefix="pixpack-test-") as temp_dir:
        root = Path(temp_dir)
        src = root / "src"
        nested = src / "子目录"
        empty = nested / "empty"
        empty.mkdir(parents=True)
        (src / "hello.txt").write_text("你好 pixpack\n", encoding="utf-8")
        (nested / "data.bin").write_bytes(bytes(range(256)) * 50)
        (src / "空文件.dat").write_bytes(b"")
        original = _snapshot(src)
        one = root / "one"
        many = root / "many"
        inside = src / "图片输出"
        back_one = root / "back-one"
        back_many = root / "back-many"
        back_inside = root / "back-inside"
        pack_ns = argparse.Namespace(src=str(src), out=str(one), max_side=128, parts=None, level=6, prefix="pixpack")
        cmd_pack(pack_ns)
        images = list(one.glob("*.png"))
        if len(images) != 1:
            raise PixpackError(f"期望自动打成 1 张，实际 {len(images)} 张")
        cmd_unpack(argparse.Namespace(images=[str(one), str(back_one)]))
        pack_ns.out = str(many)
        pack_ns.parts = 3
        pack_ns.max_side = min(4096, max_safe_side())
        cmd_pack(pack_ns)
        if len(list(many.glob("*.png"))) != 3:
            raise PixpackError("期望 --parts 3 生成 3 张图片")
        cmd_unpack(argparse.Namespace(images=[str(many), str(back_many)]))
        inside.mkdir()
        (inside / "不要打包.txt").write_text("decoy", encoding="utf-8")
        pack_ns.out = str(inside)
        pack_ns.parts = None
        pack_ns.max_side = 256
        cmd_pack(pack_ns)
        cmd_unpack(argparse.Namespace(images=[str(inside), str(back_inside)]))
        if not (back_one / "子目录" / "empty").is_dir():
            raise PixpackError("空目录没有还原")
        if _snapshot(back_one) != original or _snapshot(back_many) != original:
            raise PixpackError("还原结果与原目录不一致")
        if _snapshot(back_inside) != original:
            raise PixpackError("输出目录放在源目录内时，把不该打包的文件也打进去了")
        cmd_info(argparse.Namespace(images=[str(many)]))
        if read_image(next(one.glob("*.png")))["flags"] & FLAG_SINGLE_FILE:
            raise PixpackError("文件夹打包不应标成单个文件")
        single = src / "hello.txt"
        single_out = root / "single-img"
        single_back = root / "hello-restored"
        pack_directory(single, single_out, max_side=128, level=0)
        if not read_image(next(single_out.glob("*.png")))["flags"] & FLAG_SINGLE_FILE:
            raise PixpackError("单个文件打包没有写下标记")
        unpack_directory(single_out, single_back)
        if (single_back / "hello.txt").read_bytes() != single.read_bytes():
            raise PixpackError("单个文件没有按标记自动还原")
    print("自检通过: 单张与多张往返还原一致")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pixpack",
        description="把目录无损打包成一张或多张 PNG，再从这些 PNG 还原文件。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    device_side = max_safe_side()
    preferred_side = default_max_side()
    pack = sub.add_parser("pack", help="把文件或目录打包成 PNG")
    pack.add_argument("src", help="要打包的文件或目录")
    pack.add_argument("out", help="PNG 输出目录")
    pack.add_argument(
        "--max-side",
        type=int,
        default=preferred_side,
        help=f"单张图片最大边长。默认 {preferred_side}，本机当前不能超过 {device_side}",
    )
    pack.add_argument("--parts", type=int, default=None, help="指定张数；省略则在边长限制内尽量少张")
    pack.add_argument("--level", type=int, default=1, help="zip 压缩级别 0-9。0 只打包不压缩，海量小文件最快；默认 1")
    pack.add_argument(
        "--no-store-compressed",
        action="store_true",
        help="图片、视频、压缩包等也重新压缩。文件很多时会慢很多",
    )
    pack.add_argument("--prefix", default="pixpack", help="输出文件名前缀，默认 pixpack")
    pack.set_defaults(func=cmd_pack)

    preview = sub.add_parser("preview", help="按当前参数预估会生成多少张图片、每张多大")
    preview.add_argument("src", help="要打包的文件或目录")
    preview.add_argument(
        "--max-side",
        type=int,
        default=preferred_side,
        help=f"单张图片最大边长。默认 {preferred_side}，本机当前不能超过 {device_side}",
    )
    preview.add_argument("--parts", type=int, default=None, help="指定张数；省略则自动")
    preview.add_argument("--level", type=int, default=0, help="压缩级别 0-9，默认 0（与界面极速一致）")
    preview.add_argument("--no-store-compressed", action="store_true", help="已压缩格式也重新压缩")
    preview.set_defaults(func=cmd_preview)

    unpack = sub.add_parser("unpack", help="从 PNG 还原目录")
    unpack.add_argument(
        "images",
        nargs="+",
        help="一张或多张 PNG，或存放它们的目录；最后一个参数是还原文件夹",
    )
    unpack.set_defaults(func=cmd_unpack)

    info = sub.add_parser("info", help="查看一组 PNG 的内容和校验")
    info.add_argument("images", nargs="+", help="一张 PNG、多张 PNG，或存放它们的目录")
    info.set_defaults(func=cmd_info)

    test = sub.add_parser("selftest", help="用临时文件做往返自检")
    test.set_defaults(func=cmd_selftest)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except PixpackError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
