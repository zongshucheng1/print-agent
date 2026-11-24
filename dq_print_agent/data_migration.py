"""Offline relocation of a machine's print-agent data during installation."""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

from .config import HostConfig
from .store import instance_lock


LOCK_NAMES = {"desktop-config.lock", "host.lock", "agent.lock"}


def _check_entry(path: Path):
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or not stat.S_ISDIR(info.st_mode) and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1)):
        raise ValueError(f"数据迁移不能经过链接或非常规文件：{path}")
    return info


def _directory(value: Path) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or path.parent == path:
        raise ValueError("请选择绝对路径的专用数据文件夹")
    if os.name == "nt" and (len(path.drive) != 2 or path.is_reserved()):
        raise ValueError("请选择本机磁盘中的数据目录")
    for part in (*reversed(path.parents), path):
        try:
            info = _check_entry(part)
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"数据路径必须是文件夹：{part}")
    return path


def _entries(directory: Path):
    for item in directory.iterdir():
        info = _check_entry(item)
        yield item
        if stat.S_ISDIR(info.st_mode):
            yield from _entries(item)


def _copy_permissions(source: Path, target: Path):
    if os.name != "nt":
        shutil.copymode(source, target)
        return
    import win32security as sec
    descriptor = sec.GetFileSecurity(str(source), sec.DACL_SECURITY_INFORMATION)
    acl = descriptor.GetSecurityDescriptorDacl()
    if acl is None:
        raise ValueError(f"源数据权限不受限制：{source}")
    # A protected DACL keeps grants inherited from the destination's parent
    # out of the copied tree, even when publishing under a shared volume.
    sec.SetNamedSecurityInfo(str(target), sec.SE_FILE_OBJECT,
        sec.DACL_SECURITY_INFORMATION | sec.PROTECTED_DACL_SECURITY_INFORMATION,
        None, None, acl, None)
    actual = sec.GetFileSecurity(str(target), sec.DACL_SECURITY_INFORMATION)
    installed = actual.GetSecurityDescriptorDacl()
    def grants(dacl):
        for index in range(dacl.GetAceCount()):
            (kind, flags), *values = dacl.GetAce(index)
            # On a protected copy Windows turns inherited grants into explicit
            # grants. The provenance bit is the only expected difference.
            yield (kind, flags & ~sec.INHERITED_ACE, *values)
    if (not actual.GetSecurityDescriptorControl()[0] & sec.SE_DACL_PROTECTED or installed is None
            or list(grants(installed)) != list(grants(acl))):
        raise ValueError("新数据目录无法保留原 Windows 访问权限，请选择支持 NTFS 权限的目录")


def _permission_key(path: Path):
    if os.name != "nt":
        return stat.S_IMODE(path.stat().st_mode)
    import win32security as sec
    info = sec.DACL_SECURITY_INFORMATION
    descriptor = sec.GetFileSecurity(str(path), info)
    return sec.ConvertSecurityDescriptorToStringSecurityDescriptor(
        descriptor, sec.SDDL_REVISION_1, info)


def _relocated_config(file: Path, source: Path, destination: Path):
    raw = json.loads(file.read_text(encoding="utf-8-sig"))
    host = HostConfig.from_dict(raw, file)
    def relocate(path: Path):
        if not path.is_relative_to(source):
            raise ValueError(f"配置引用了原数据目录以外的位置，请核对 {file}")
        return str(destination / path.relative_to(source))
    raw["state_dir"] = relocate(host.state_dir)
    raw["credential_file"] = relocate(host.credential_file)
    for row, printer in zip(raw["printers"], host.printers):
        row["state_dir"] = relocate(printer.state_dir)
    return raw


def _matches(staged: Path, destination: Path) -> bool:
    def tree(root):
        return {item.relative_to(root) for item in _entries(root)}
    expected = tree(staged)
    if expected != tree(destination):
        return False
    for name in (Path("."), *expected):
        first, second = staged / name, destination / name
        if first.is_dir() != second.is_dir() or _permission_key(first) != _permission_key(second):
            return False
        if first.is_file():
            with first.open("rb") as a, second.open("rb") as b:
                if hashlib.file_digest(a, "sha256").digest() != hashlib.file_digest(b, "sha256").digest():
                    return False
    return True


def migrate_data_directory(source: Path, destination: Path) -> None:
    """Copy to a new or empty root. Accept an exact previous copy on retry."""
    source, destination = _directory(source), _directory(destination)
    if source == destination:
        return
    if source.is_relative_to(destination) or destination.is_relative_to(source):
        raise ValueError("原数据目录与新目录不能互相包含")
    if not source.exists() or not any(source.iterdir()):
        if destination.exists() and any(destination.iterdir()):
            raise ValueError("新数据目录已有文件，请选择空文件夹")
        return

    with ExitStack() as guards:
        desktop_lock = source / "desktop-config.lock"
        if os.path.lexists(desktop_lock):
            _check_entry(desktop_lock)
        guards.enter_context(instance_lock(desktop_lock))
        entries = list(_entries(source))
        # A foreground worker takes desktop-config.lock before host.lock.
        # Existing workers retain host/agent locks for their entire lifetime.
        for path in entries:
            if path.name.casefold() in {"host.lock", "agent.lock"}:
                guards.enter_context(instance_lock(path))
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".deqin-migrate-") as temporary:
            staged = Path(temporary) / "data"
            staged.mkdir()
            _copy_permissions(source, staged)
            for path in entries:
                relative = path.relative_to(source)
                if path.name.casefold() in LOCK_NAMES:
                    continue
                target = staged / relative
                if path.is_dir():
                    target.mkdir()
                    _copy_permissions(path, target)
                    continue
                is_config = (relative in (Path("config.json"), Path("host-origin.json"))
                             or relative.parent == Path("host-profiles") and path.suffix.lower() == ".json")
                # Install the file's own DACL before putting any bytes in it.
                target.touch()
                _copy_permissions(path, target)
                if is_config:
                    value = _relocated_config(path, source, destination)
                    target.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
                else:
                    shutil.copyfile(path, target)
                shutil.copystat(path, target)
            if destination.exists():
                if any(destination.iterdir()):
                    if _matches(staged, destination):
                        return
                    raise ValueError("新数据目录已有不同的文件或权限，请选择空文件夹")
                destination.rmdir()  # Fails safely if the directory gained any files.
            staged.rename(destination)
