"""Bounded Windows inventory and explicit RAW queue creation; never submit jobs."""
from __future__ import annotations

import base64
import ipaddress
import json
import os
from pathlib import Path
import subprocess


class NetworkSetupError(RuntimeError):
    pass


_PRIVATE = tuple(ipaddress.ip_network(v) for v in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '169.254.0.0/16'))


def lan_address(value):
    try:
        result = ipaddress.IPv4Address(value)
    except (ValueError, TypeError):
        raise NetworkSetupError('请输入有效的局域网 IPv4 地址。') from None
    if not any(result in net for net in _PRIVATE):
        raise NetworkSetupError('仅支持本地专用网络或链路本地 IPv4 地址。')
    return result


_COMMON = r'''
$ErrorActionPreference='Stop'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
function Finish($value) { $value | ConvertTo-Json -Depth 7 -Compress; exit 0 }
function Fail($code) { Finish @{ok=$false;code=$code} }
'''

_INVENTORY = r'''
try {
  $up=@(Get-NetAdapter | Where-Object Status -eq 'Up' | ForEach-Object ifIndex)
  $interfaces=@(Get-NetIPAddress -AddressFamily IPv4 | Where-Object {
    $_.InterfaceIndex -in $up -and $_.AddressState -eq 'Preferred'
  } | Select-Object -First 32 | ForEach-Object {
    @{id=[string]$_.InterfaceIndex;name=$_.InterfaceAlias;address=$_.IPAddress;prefixLength=[int]$_.PrefixLength}
  })
  $ports=@{}
  Get-PrinterPort | ForEach-Object { $ports[$_.Name]=$_ }
  $queues=@(Get-Printer | Select-Object -First 512 | ForEach-Object {
    $port=$ports[$_.PortName]
    @{name=$_.Name;driver=$_.DriverName;ip=$port.PrinterHostAddress;port=$port.PortNumber;protocol=$port.Protocol}
  })
  $drivers=@(Get-PrinterDriver | Select-Object -First 256 | ForEach-Object Name | Sort-Object -Unique)
  Finish @{ok=$true;interfaces=$interfaces;drivers=$drivers;queues=$queues}
} catch { Fail 'INVENTORY_FAILED' }
'''

# All user values arrive as JSON on stdin. They never become PowerShell source,
# executable names, flags, expressions, or a command passed to another shell.
_ADD_QUEUE = r'''
try {
  $request=[Console]::In.ReadToEnd() | ConvertFrom-Json
  $target=[string]$request.ip
  $name=[string]$request.queueName
  $driver=[string]$request.driverName
  $parsed=$null
  if(-not [Net.IPAddress]::TryParse($target,[ref]$parsed) -or $parsed.AddressFamily -ne 'InterNetwork' -or [int]$request.port -ne 9100) { Fail 'INVALID_TARGET' }
  if(-not @(Get-PrinterDriver | Where-Object Name -eq $driver).Count) { Fail 'DRIVER_MISSING' }
  $ports=@(Get-PrinterPort)
  $queues=@(Get-Printer)
  $matching=@($ports | Where-Object { $_.PrinterHostAddress -eq $target -and $_.Protocol -eq 1 -and $_.PortNumber -eq 9100 })
  $matchingNames=@($matching | ForEach-Object Name)
  $existing=@($queues | Where-Object PortName -in $matchingNames)
  if($existing.Count) {
    $preferred=@($existing | Where-Object DriverName -eq $driver | Sort-Object Name)
    if(-not $preferred.Count) { Fail 'TARGET_DRIVER_CONFLICT' }
    $selected=$preferred | Where-Object Name -eq $name | Select-Object -First 1
    if(-not $selected) { $selected=$preferred[0] }
    Finish @{ok=$true;queueName=$selected.Name;created=$false;driverName=$selected.DriverName;requiresServiceVerification=$true}
  }
  if(@($queues | Where-Object Name -eq $name).Count) { Fail 'NAME_CONFLICT' }
  if($matching.Count) { $portName=($matching | Sort-Object Name | Select-Object -First 1).Name }
  else {
    $portName='IP_'+$target
    if(@($ports | Where-Object Name -eq $portName).Count) { Fail 'PORT_CONFLICT' }
    Add-PrinterPort -Name $portName -PrinterHostAddress $target -PortNumber 9100
  }
  # Recheck immediately before use; never redirect an existing port or queue.
  $port=Get-PrinterPort -Name $portName
  if($port.PrinterHostAddress -ne $target -or $port.Protocol -ne 1 -or $port.PortNumber -ne 9100) { Fail 'PORT_CONFLICT' }
  Add-Printer -Name $name -DriverName $driver -PortName $portName
  $queue=Get-Printer -Name $name
  $port=Get-PrinterPort -Name $queue.PortName
  if($queue.DriverName -ne $driver -or $port.PrinterHostAddress -ne $target -or $port.Protocol -ne 1 -or $port.PortNumber -ne 9100) { Fail 'VERIFY_FAILED' }
  Finish @{ok=$true;queueName=$queue.Name;created=$true;driverName=$queue.DriverName;requiresServiceVerification=$true}
} catch {
  if($_.CategoryInfo.Category -eq 'PermissionDenied' -or $_.Exception.HResult -eq -2147024891) { Fail 'PERMISSION' }
  Fail 'QUEUE_FAILED'
}
'''

_ERRORS = {
    'INVENTORY_FAILED': '无法读取 Windows 网卡、驱动或打印队列，请检查系统打印服务。',
    'DRIVER_MISSING': '所选驱动已不可用，请刷新列表；先安装对应型号的 Windows 驱动。',
    'TARGET_DRIVER_CONFLICT': '此地址已有使用其他驱动的队列，请先在 Windows 中核对原队列，避免重复添加。',
    'NAME_CONFLICT': '这个队列名称已被其他设备使用，请换一个名称。',
    'PORT_CONFLICT': '已有同名端口指向其他设备，未修改原端口。请在 Windows 中核对。',
    'PERMISSION': 'Windows 拒绝创建打印队列。请以管理员身份重新打开客户端后重试。',
    'QUEUE_FAILED': 'Windows 未完成添加。请刷新队列后核对；如仍失败，请以管理员身份重试。',
    'VERIFY_FAILED': '添加结果与目标不一致，请刷新并在 Windows 中核对，暂勿重复添加。',
}


def _powershell(script, payload=None, *, timeout=15):
    if os.name != 'nt':
        raise NetworkSetupError('网络队列管理仅支持 Windows。')
    executable = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    encoded = base64.b64encode((_COMMON + script).encode('utf-16le')).decode('ascii')
    try:
        result = subprocess.run([str(executable), '-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
            input=json.dumps(payload or {}, ensure_ascii=False), capture_output=True, encoding='utf-8',
            errors='replace', timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired:
        raise NetworkSetupError('Windows 操作超时。请先刷新列表核对结果，暂勿重复添加。') from None
    except OSError:
        raise NetworkSetupError('无法启动 Windows 队列管理工具。') from None
    try:
        if result.returncode or len(result.stdout) > 1024 * 1024:
            raise ValueError()
        value = json.loads(result.stdout.lstrip('\ufeff'))
        if not isinstance(value, dict):
            raise ValueError()
    except (ValueError, TypeError):
        raise NetworkSetupError('Windows 未返回有效结果，请刷新列表并核对系统打印服务。') from None
    if not value.get('ok'):
        raise NetworkSetupError(_ERRORS.get(value.get('code'), '网络队列操作未完成，请核对输入和 Windows 打印服务。'))
    return value


def inventory():
    raw = _powershell(_INVENTORY)
    interfaces = []
    for item in raw.get('interfaces', []):
        try:
            address = lan_address(item['address'])
            interface = ipaddress.IPv4Interface(f"{address}/{int(item['prefixLength'])}")
            if interface.network.prefixlen > 30:
                continue
            scan = ipaddress.IPv4Network(f"{address}/{max(24, interface.network.prefixlen)}", strict=False)
            interfaces.append({'id': str(item['id']), 'name': str(item['name']), 'address': str(address),
                'prefixLength': interface.network.prefixlen, 'network': str(interface.network), 'scanNetwork': str(scan)})
        except (ValueError, KeyError, TypeError, NetworkSetupError):
            continue
    queues = []
    for item in raw.get('queues', []):
        try:
            address = str(lan_address(item.get('ip')))
            port = int(item.get('port'))
            if int(item.get('protocol')) != 1:
                continue
        except (ValueError, TypeError, NetworkSetupError):
            continue
        queues.append({'name': str(item['name']), 'driver': str(item['driver']), 'ip': address, 'port': port})
    return {'interfaces': interfaces, 'drivers': sorted({x for x in raw.get('drivers', []) if isinstance(x, str) and x}),
            'queues': queues, 'serviceVerified': False}


def resolve_interface(selected, current):
    if not isinstance(selected, dict):
        raise NetworkSetupError('请选择本机网络接口。')
    for item in current['interfaces']:
        if all(str(item.get(k)) == str(selected.get(k)) for k in ('id', 'address', 'prefixLength', 'network')):
            return item
    raise NetworkSetupError('本机网卡配置已变化，请刷新网络列表后重试。')


def add_queue(fields, *, current=None):
    if not isinstance(fields, dict):
        raise NetworkSetupError('网络打印机参数无效。')
    address = lan_address(fields.get('ip'))
    if fields.get('port') != 9100:
        raise NetworkSetupError('当前自动添加支持 RAW TCP 9100；其他类型请使用 Windows 添加打印机。')
    current = current if current is not None else inventory()
    local = [ipaddress.IPv4Interface(f"{v['address']}/{v['prefixLength']}") for v in current['interfaces']]
    if any(address == x.ip for x in local) or not any(address in x.network and address not in (x.network.network_address, x.network.broadcast_address) for x in local):
        raise NetworkSetupError('目标必须是所选电脑可直达的本地子网设备，不能是本机或广播地址。')
    driver, name = fields.get('driverName'), fields.get('queueName')
    if driver not in current['drivers']:
        raise NetworkSetupError('请先选择已安装的对应型号 Windows 驱动。')
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 180 or any(ord(x) < 32 or x in '\\,/*?[]' for x in name):
        raise NetworkSetupError('请输入有效的队列名称（最多 180 字，不含斜线、逗号、通配符或控制字符）。')
    value = _powershell(_ADD_QUEUE, {'ip': str(address), 'port': 9100, 'driverName': driver, 'queueName': name.strip()})
    return {k: value[k] for k in ('queueName', 'created', 'driverName', 'requiresServiceVerification')}
