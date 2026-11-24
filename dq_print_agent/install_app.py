"""Prepare the service program directory before Setup extracts executable code."""
from pathlib import Path

import ntsecuritycon as rights
import pywintypes
import win32con
import win32file
import win32security as sec


ADMINISTRATORS = "S-1-5-32-544"
TRUSTED = {ADMINISTRATORS, "S-1-5-18",
           "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"}  # TrustedInstaller
APP_SECURITY = "O:BAD:P(A;OICI;FA;;;BA)(A;OICI;FA;;;SY)(A;OICI;GRGX;;;BU)"
OWNER_DACL = sec.OWNER_SECURITY_INFORMATION | sec.DACL_SECURITY_INFORMATION


def _inspect(path: Path, *, ancestor=False, empty_destination=False):
    access = win32con.MAXIMUM_ALLOWED if empty_destination else rights.READ_CONTROL | rights.FILE_READ_ATTRIBUTES
    # Keep the inspected directory in place. Opening the link itself also
    # permits rejection of a reparse point without reading its target.
    handle = win32file.CreateFile(str(path), access, win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
        None, win32con.OPEN_EXISTING, win32con.FILE_FLAG_BACKUP_SEMANTICS | win32file.FILE_FLAG_OPEN_REPARSE_POINT, None)
    try:
        info = win32file.GetFileInformationByHandle(handle)
        directory = bool(info[0] & win32con.FILE_ATTRIBUTE_DIRECTORY)
        if (info[0] & win32con.FILE_ATTRIBUTE_REPARSE_POINT or not directory and info[7] != 1
                or (ancestor or empty_destination) and not directory):
            raise ValueError("程序路径含有文件链接、联接或无效的文件夹。")
        if empty_destination:
            if any(path.iterdir()):
                raise ValueError("请为新安装选择空程序目录。")
            descriptor = sec.ConvertStringSecurityDescriptorToSecurityDescriptor(APP_SECURITY, sec.SDDL_REVISION_1)
            sec.SetSecurityInfo(handle, sec.SE_FILE_OBJECT, OWNER_DACL | sec.PROTECTED_DACL_SECURITY_INFORMATION,
                                descriptor.GetSecurityDescriptorOwner(), None, descriptor.GetSecurityDescriptorDacl(), None)
        data = sec.GetSecurityInfo(handle, sec.SE_FILE_OBJECT, OWNER_DACL)
        owner = data.GetSecurityDescriptorOwner()
        acl = data.GetSecurityDescriptorDacl()
        if owner is None or sec.ConvertSidToStringSid(owner) not in TRUSTED or acl is None:
            raise ValueError("程序目录及其上级目录须由系统或管理员管理。")
        unsafe = rights.WRITE_DAC | rights.WRITE_OWNER | rights.FILE_DELETE_CHILD | rights.GENERIC_ALL
        if path != path.parent:
            unsafe |= rights.DELETE
        if not ancestor:
            unsafe |= (rights.FILE_WRITE_DATA | rights.FILE_APPEND_DATA | rights.FILE_WRITE_ATTRIBUTES
                       | rights.FILE_WRITE_EA | rights.GENERIC_WRITE)
        for index in range(acl.GetAceCount()):
            (kind, flags), mask, *principals = acl.GetAce(index)
            if kind == sec.ACCESS_DENIED_ACE_TYPE:
                continue
            if kind != sec.ACCESS_ALLOWED_ACE_TYPE:
                raise ValueError("程序目录具有无法验证的特殊权限。")
            sid = sec.ConvertSidToStringSid(principals[-1])
            if flags & sec.INHERIT_ONLY_ACE and (ancestor or sid == "S-1-3-0"):
                continue
            if sid not in TRUSTED and mask & unsafe:
                raise ValueError("现有文件夹可被普通账户修改，请改选管理员管理的空目录。")
        if directory and not ancestor and not empty_destination:
            for child in path.iterdir():
                _inspect(child)
    finally:
        handle.Close()


def protect_application(application: Path, previous_app: Path | None) -> None:
    path = Path(application)
    if not path.is_absolute() or len(path.drive) != 2 or not path.name or ".." in path.parts:
        raise ValueError("请选择本机的专用程序目录。")
    attributes = pywintypes.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = sec.ConvertStringSecurityDescriptorToSecurityDescriptor(APP_SECURITY, sec.SDDL_REVISION_1)
    for directory in (*reversed(path.parents), path):
        # Check each existing ancestor before using its children. New parents
        # receive their protected owner/DACL atomically with directory creation.
        if not directory.exists():
            win32file.CreateDirectory(str(directory), attributes)
        if directory == path and path == previous_app:
            from .setup_directories import _registered_install
            if path != _registered_install():
                raise ValueError("不能确认旧程序目录的安装记录。")
        _inspect(directory, ancestor=directory != path, empty_destination=directory == path and path != previous_app)
