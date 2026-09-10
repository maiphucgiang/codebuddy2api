"""凭据导入目录读取与私有文件原子更新。"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

MAX_CREDENTIAL_BYTES = 1024 * 1024


class CredentialFileError(ValueError):
    """可安全返回给客户端的文件输入错误。"""


def _valid_name(name: str) -> bool:
    return (bool(name) and name.endswith(".info") and name not in {".info", "..info"}
            and not any(c in name for c in ("/", "\\", ":", "\x00"))
            and not any(ord(c) < 32 for c in name))


def read_import_file(directory: Path, requested_path: str) -> tuple[str, bytes]:
    """从服务端导入目录选择普通 .info 文件，限量读取一次。"""
    if not isinstance(requested_path, str) or not requested_path or len(requested_path) > 4096:
        raise CredentialFileError("path 必须是导入目录中的 .info 文件名或绝对路径")
    root = directory.resolve(strict=True)
    # 请求只用于选择服务端枚举的文件，不参与构造文件系统路径。
    for entry in root.glob("*.info"):
        if requested_path not in (entry.name, str(entry)):
            continue
        if not _valid_name(entry.name):
            raise CredentialFileError("凭据文件名无效")
        before = entry.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise CredentialFileError("只允许普通 .info 文件，不允许符号链接")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_BINARY", 0)
        fd = os.open(entry, flags)
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (not stat.S_ISREG(opened.st_mode)
                    or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)):
                raise CredentialFileError("导入文件在读取前发生变化，请重试")
            if opened.st_size > MAX_CREDENTIAL_BYTES:
                raise CredentialFileError("凭据文件不能超过 1 MiB")
            content = stream.read(MAX_CREDENTIAL_BYTES + 1)
        if len(content) > MAX_CREDENTIAL_BYTES:
            raise CredentialFileError("凭据文件不能超过 1 MiB")
        return entry.name, content
    raise CredentialFileError("文件不存在或不在允许的导入目录中")


def atomic_write_credential(directory: Path, name: str, content: bytes) -> Path:
    """将已校验内容以 0600 临时文件原子写入，保留同名更新语义。"""
    if not isinstance(name, str) or not _valid_name(name):
        raise CredentialFileError("凭据文件名无效")
    if len(content) > MAX_CREDENTIAL_BYTES:
        raise CredentialFileError("凭据文件不能超过 1 MiB")
    root = directory.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = root / name
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise CredentialFileError("凭据目标必须是普通文件，不允许符号链接")
    fd, temporary = tempfile.mkstemp(prefix=".credential-", suffix=".tmp", dir=root)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        # 替换目录项而不是打开目标写入，避免跟随校验后被换入的链接。
        os.replace(temporary, target)
        return target
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
