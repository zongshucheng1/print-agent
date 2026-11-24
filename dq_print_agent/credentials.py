"""Windows machine DPAPI, with a restricted runtime-directory ACL.

Only the installing user, administrators and SYSTEM can read the credential file.
Machine scope permits the Windows service to run as SYSTEM without an interactive login.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import csv
import io
import os
from pathlib import Path
import stat
import subprocess


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _transform(data: bytes, protect: bool) -> bytes:
    if os.name != "nt":
        raise RuntimeError("凭据保护需要 Windows DPAPI")
    buffer = ctypes.create_string_buffer(data)
    source = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    result = _Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if protect:
        function = crypt.CryptProtectData
        function.argtypes = [ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.POINTER(_Blob),
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
        ok = function(ctypes.byref(source), "Deqin print agent", None, None, None, 5, ctypes.byref(result))
    else:
        function = crypt.CryptUnprotectData
        function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob),
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
        ok = function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))


def secure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)
        return
    flags = subprocess.CREATE_NO_WINDOW
    identity = subprocess.run(["whoami.exe", "/user", "/fo", "csv", "/nh"],
                              capture_output=True, text=True, check=True, creationflags=flags)
    sid = next(csv.reader(io.StringIO(identity.stdout.strip())))[1]
    if not sid.startswith("S-1-"):
        raise RuntimeError("无法确认本机用户 SID")
    subprocess.run(["icacls.exe", str(path), "/inheritance:r", "/grant:r",
                    f"*{sid}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F"],
                   capture_output=True, check=True, creationflags=flags)


def ensure_runtime_access(path: Path, trusted_root: Path) -> None:
    """Create/repair a printer subtree using its already protected host ACL.

    The service runs as SYSTEM, so deriving this ACL from its current identity
    would remove the desktop operator. Never inherit permissions from ProgramData
    or another host, and never replace or remove journal/credential contents.
    Repairing an old SYSTEM-only subtree requires an already authorized caller.
    """
    path = Path(os.path.abspath(path))
    trusted_root = Path(os.path.abspath(trusted_root))
    if path == trusted_root or not path.is_relative_to(trusted_root):
        raise ValueError("打印机数据目录必须位于已授权的客户端目录内")

    def reject_link(candidate):
        attributes = candidate.lstat()
        if (stat.S_ISLNK(attributes.st_mode)
                or getattr(attributes, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024)
                or not stat.S_ISDIR(attributes.st_mode) and attributes.st_nlink > 1):
            raise ValueError("客户端数据目录不能包含链接或目录联接")

    # Check lexical ancestors before resolving paths, so junctions cannot redirect
    # a repair to another host or outside the application-owned subtree.
    for candidate in (path, *path.parents):
        if candidate.exists() or candidate.is_symlink():
            reject_link(candidate)
    if not trusted_root.is_dir():
        raise ValueError("已授权的客户端根目录不存在")
    if os.name != "nt":
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)
        return

    import win32security as security
    import win32con
    import win32file
    descriptor = security.GetFileSecurity(str(trusted_root), security.DACL_SECURITY_INFORMATION)
    control, _ = descriptor.GetSecurityDescriptorControl()
    original = descriptor.GetSecurityDescriptorDacl()
    if not control & security.SE_DACL_PROTECTED or original is None:
        raise ValueError("客户端根目录没有受保护的权限，不能用于修复打印机目录")
    grants = []
    identities = set()
    for index in range(original.GetAceCount()):
        (kind, flags), mask, sid = original.GetAce(index)
        identity = security.ConvertSidToStringSid(sid)
        if (kind != security.ACCESS_ALLOWED_ACE_TYPE or flags & (security.INHERITED_ACE | security.INHERIT_ONLY_ACE)
                or identity not in {"S-1-5-18", "S-1-5-32-544"} and not identity.startswith("S-1-5-21-")):
            raise ValueError("客户端根目录包含未确认的权限，不能扩展到打印机目录")
        identities.add(identity)
        grants.append((mask, sid))
    if not {"S-1-5-18", "S-1-5-32-544"}.issubset(identities):
        raise ValueError("客户端根目录缺少系统或管理员权限")

    def apply(candidate):
        reject_link(candidate)
        dacl = security.ACL()
        # MAXIMUM_ALLOWED intentionally disables SetSecurityInfo's automatic
        # propagation. Each descendant is checked independently before touching
        # its ACL, so a junction/hard link cannot alter an outside object's ACL.
        handle = win32file.CreateFile(str(candidate), win32con.MAXIMUM_ALLOWED,
            win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE,
            None, win32con.OPEN_EXISTING,
            win32con.FILE_FLAG_BACKUP_SEMANTICS | win32file.FILE_FLAG_OPEN_REPARSE_POINT, None)
        try:
            info = win32file.GetFileInformationByHandle(handle)
            directory = bool(info[0] & win32con.FILE_ATTRIBUTE_DIRECTORY)
            if info[0] & win32con.FILE_ATTRIBUTE_REPARSE_POINT or not directory and info[7] > 1:
                raise ValueError("客户端数据目录不能包含链接或目录联接")
            flags = security.OBJECT_INHERIT_ACE | security.CONTAINER_INHERIT_ACE if directory else 0
            for mask, sid in grants:
                dacl.AddAccessAllowedAceEx(security.ACL_REVISION, flags, mask, sid)
            security.SetSecurityInfo(handle, security.SE_FILE_OBJECT,
                security.DACL_SECURITY_INFORMATION | security.PROTECTED_DACL_SECURITY_INFORMATION,
                None, None, dacl, None)
        finally:
            handle.Close()
        if directory:
            # Repair each directory before listing it, including protected ACLs
            # left by older service versions. Never follow reparse points.
            for child in candidate.iterdir():
                apply(child)

    path.mkdir(parents=True, exist_ok=True)
    apply(path)


def save_token(path: Path, token: str) -> None:
    if len(token) < 32 or any(c.isspace() for c in token):
        raise ValueError("客户端密钥格式无效")
    secure_directory(path.parent)
    encrypted = _transform(token.encode(), True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(encrypted)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def load_token(path: Path) -> str:
    return _transform(path.read_bytes(), False).decode()
