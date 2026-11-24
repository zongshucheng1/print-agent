"""Initialize the operator ACL on a dedicated empty runtime directory during setup."""
from pathlib import Path


def _operator_sid():
    import win32api
    import win32con
    import win32security as security
    import win32ts

    # UAC can run Setup as a different administrator. The session account is
    # the operator who will open the GUI after Setup finishes.
    session = (win32ts.WTS_CURRENT_SERVER_HANDLE, win32ts.WTS_CURRENT_SESSION)
    username = win32ts.WTSQuerySessionInformation(*session, win32ts.WTSUserName)
    if username:
        domain = win32ts.WTSQuerySessionInformation(*session, win32ts.WTSDomainName)
        account = domain + "\\" + username if domain else username
        return security.LookupAccountName(None, account)[0]
    # Unattended Setup can run without an interactive session.
    token = security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        return security.GetTokenInformation(token, security.TokenUser)[0]
    finally:
        token.Close()


def initialize_data_access(destination: Path) -> None:
    import ntsecuritycon
    import win32con
    import win32file
    import win32security as security
    from .windows_service import _repair_path

    destination = Path(destination)
    if (not destination.is_absolute() or len(destination.drive) != 2 or not destination.name
            or ".." in destination.parts):
        raise ValueError("请选择本机专用的数据文件夹。")
    destination = _repair_path(destination)
    operator = security.ConvertSidToStringSid(_operator_sid())
    acl = security.ACL()
    for identity in dict.fromkeys((operator, "S-1-5-18", "S-1-5-32-544")):
        acl.AddAccessAllowedAceEx(security.ACL_REVISION, security.OBJECT_INHERIT_ACE | security.CONTAINER_INHERIT_ACE,
                                  ntsecuritycon.FILE_ALL_ACCESS, security.ConvertStringSidToSid(identity))
    destination.mkdir(parents=True, exist_ok=True)
    # MAXIMUM_ALLOWED avoids propagating ACLs to descendants. Open the directory
    # itself without following a final junction or sharing delete/rename access.
    handle = win32file.CreateFile(str(destination), win32con.MAXIMUM_ALLOWED,
        win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE, None, win32con.OPEN_EXISTING,
        win32con.FILE_FLAG_BACKUP_SEMANTICS | win32file.FILE_FLAG_OPEN_REPARSE_POINT, None)
    try:
        attributes = win32file.GetFileInformationByHandle(handle)[0]
        if (not attributes & win32con.FILE_ATTRIBUTE_DIRECTORY
                or attributes & win32con.FILE_ATTRIBUTE_REPARSE_POINT or any(destination.iterdir())):
            raise ValueError("首次初始化需要专用的空数据文件夹。")
        security.SetSecurityInfo(handle, security.SE_FILE_OBJECT,
            security.DACL_SECURITY_INFORMATION | security.PROTECTED_DACL_SECURITY_INFORMATION,
            None, None, acl, None)
        descriptor = security.GetSecurityInfo(handle, security.SE_FILE_OBJECT, security.DACL_SECURITY_INFORMATION)
        actual = descriptor.GetSecurityDescriptorDacl()
        if (not descriptor.GetSecurityDescriptorControl()[0] & security.SE_DACL_PROTECTED
                or actual is None or actual.GetAceCount() != acl.GetAceCount()
                or any(actual.GetAce(i) != acl.GetAce(i) for i in range(acl.GetAceCount()))):
            raise ValueError("所选数据目录不能保存 Windows 访问权限。")
    finally:
        handle.Close()
