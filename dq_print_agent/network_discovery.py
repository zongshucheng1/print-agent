"""Bounded, read-only discovery on an explicitly selected local IPv4 interface.

Printer ports receive no application bytes. HTTP is only used on port 80 with
fixed, parameter-free paths, and is never used to print or configure a device.
No proxy, DNS, redirect, JavaScript, SNMP write, or vendor binary is involved.
"""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from html.parser import HTMLParser
import ipaddress
import re
import socket
import threading
import time


MAX_HOSTS = 254
MAX_WORKERS = 16
MAX_SECONDS = 20.0
CONNECT_SECONDS = 0.45
HTTP_SECONDS = 0.8
MAX_HEADER_BYTES = 8192
MAX_BODY_BYTES = 32768
_LAN = tuple(ipaddress.ip_network(value) for value in
             ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '169.254.0.0/16'))


class DiscoveryError(ValueError):
    """A safe message suitable for the desktop UI."""


def _lan_ip(value):
    try:
        address = ipaddress.IPv4Address(value)
    except (ValueError, TypeError):
        raise DiscoveryError('请输入有效的局域网 IPv4 地址。') from None
    if not any(address in network for network in _LAN):
        raise DiscoveryError('只能搜索本地专用网络或链路本地 IPv4 地址。')
    return address


def _targets(interface, network, ip, local_addresses):
    try:
        source = _lan_ip(interface['address'])
        prefix = interface['prefixLength']
        if type(prefix) is not int or not 1 <= prefix <= 30:
            raise ValueError('prefix')
        local = ipaddress.IPv4Interface(f'{source}/{prefix}').network
        if ipaddress.IPv4Network(interface['network'], strict=True) != local:
            raise ValueError('network')
    except (KeyError, TypeError, ValueError):
        raise DiscoveryError('网卡地址或子网信息已变化，请刷新网卡列表。') from None
    excluded = {source}
    for value in local_addresses:
        try:
            excluded.add(ipaddress.IPv4Address(value))
        except (ValueError, TypeError):
            raise DiscoveryError('本机网卡地址无效，请刷新网卡列表。') from None
    if ip is not None:
        address = _lan_ip(ip.strip() if isinstance(ip, str) else ip)
        if address not in local or address in excluded or address in (local.network_address, local.broadcast_address):
            raise DiscoveryError('目标必须是所选网卡同一子网内的其他设备，不能使用本机或广播地址。')
        return str(source), str(address) + '/32', [str(address)]
    try:
        selected = ipaddress.IPv4Network(network.strip(), strict=False)
    except (ValueError, AttributeError, TypeError):
        raise DiscoveryError('搜索范围格式应为 IPv4 网段，例如 192.168.0.0/24。') from None
    if not selected.subnet_of(local):
        raise DiscoveryError('搜索范围必须位于所选网卡的本地子网内。')
    if selected.prefixlen < 24:
        raise DiscoveryError('每次最多搜索 254 个地址；较大子网请分段选择 /24 范围。')
    addresses = [str(value) for value in selected.hosts()
                 if value not in excluded and value not in (local.network_address, local.broadcast_address)]
    if not addresses:
        raise DiscoveryError('搜索范围内没有可检查的其他设备。')
    # Explicitly constrain every address; is_private alone also admits reserved ranges.
    for value in addresses:
        _lan_ip(value)
    if len(addresses) > MAX_HOSTS:
        raise DiscoveryError('每次最多搜索 254 个地址。')
    return str(source), str(selected), addresses


def _remaining(deadline, cancel, clock=time.monotonic):
    if cancel is not None and cancel.is_set():
        raise TimeoutError('cancelled')
    remaining = deadline - clock()
    if remaining <= 0:
        raise TimeoutError('deadline')
    return remaining


def _tcp_open(ip, port, *, source_address, deadline, cancel, socket_factory=socket.socket, clock=time.monotonic):
    """Connect and close only: do not send even a newline to a print port."""
    try:
        with socket_factory(socket.AF_INET, socket.SOCK_STREAM) as stream:
            stream.settimeout(min(CONNECT_SECONDS, _remaining(deadline, cancel, clock)))
            stream.bind((source_address, 0))
            stream.connect((ip, port))
            return True
    except (OSError, TimeoutError):
        return False


def _http_get(ip, path, *, source_address, deadline, cancel, socket_factory=socket.socket, clock=time.monotonic):
    """Read a small HTML document with an absolute wall-clock deadline.

    A per-socket idle timeout is insufficient against a slowly streamed header
    or body, so all reads are explicit recv calls with a shared deadline.
    """
    if path not in ('/', '/port_stat.htm'):
        raise DiscoveryError('只读发现不允许请求该设备路径。')
    # The path and address can never become request headers or a write query.
    ip = str(_lan_ip(ip))
    deadline = min(deadline, clock() + HTTP_SECONDS)
    with socket_factory(socket.AF_INET, socket.SOCK_STREAM) as stream:
        stream.settimeout(min(CONNECT_SECONDS, _remaining(deadline, cancel, clock)))
        stream.bind((source_address, 0))
        stream.connect((ip, 80))
        request = (f'GET {path} HTTP/1.0\r\nHost: {ip}\r\nConnection: close\r\n'
                   'Accept: text/html\r\nAccept-Encoding: identity\r\n\r\n').encode('ascii')
        stream.settimeout(_remaining(deadline, cancel, clock))
        stream.sendall(request)
        data = bytearray()
        header_end = None
        content_length = None
        while True:
            stream.settimeout(min(0.25, _remaining(deadline, cancel, clock)))
            chunk = stream.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
            if header_end is None:
                marker = data.find(b'\r\n\r\n')
                if marker < 0:
                    if len(data) > MAX_HEADER_BYTES:
                        raise ValueError('oversized header')
                    continue
                header_end = marker + 4
                if header_end > MAX_HEADER_BYTES:
                    raise ValueError('oversized header')
                lines = bytes(data[:marker]).decode('iso-8859-1').split('\r\n')
                if not re.fullmatch(r'HTTP/1\.[01] 200(?: .*)?', lines[0]):
                    raise ValueError('non-success response; redirects are not followed')
                headers = {}
                for line in lines[1:]:
                    key, separator, value = line.partition(':')
                    if not separator:
                        raise ValueError('malformed header')
                    key = key.strip().lower()
                    if key in headers:
                        raise ValueError('duplicate header')
                    headers[key] = value.strip()
                if headers.get('content-encoding', 'identity').lower() != 'identity' or 'transfer-encoding' in headers:
                    raise ValueError('encoded response unsupported')
                if 'content-type' in headers and 'text/html' not in headers['content-type'].lower():
                    raise ValueError('not HTML')
                if 'content-length' in headers:
                    if not re.fullmatch(r'\d{1,8}', headers['content-length']):
                        raise ValueError('invalid length')
                    content_length = int(headers['content-length'])
                    if content_length > MAX_BODY_BYTES:
                        raise ValueError('oversized body')
            body_length = len(data) - header_end
            if body_length > MAX_BODY_BYTES:
                raise ValueError('oversized body')
            if content_length is not None and body_length >= content_length:
                if body_length != content_length:
                    raise ValueError('unexpected body bytes')
                break
        _remaining(deadline, cancel, clock)
        if header_end is None:
            raise ValueError('incomplete header')
        body = bytes(data[header_end:])
        if content_length is not None and len(body) != content_length:
            raise ValueError('incomplete body')
        return body.decode('utf-8', errors='replace')


class _ModulePage(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.recognized = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag.lower() == 'meta' and str(attrs.get('name', '')).lower() == 'description':
            self.recognized = str(attrs.get('content', '')).strip().casefold() == 'jk-e02 ethernet interface module'


class _StatusPage(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.cells, self.cell = [], [], None
        self.ignore = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.ignore += 1
        if self.ignore:
            return
        if tag == 'tr':
            self.cells = []
        elif tag == 'td':
            self.cell = []

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.ignore = max(0, self.ignore - 1)
        if self.ignore:
            return
        if tag == 'td' and self.cell is not None:
            self.cells.append(' '.join(''.join(self.cell).split()))
            self.cell = None
        elif tag == 'tr':
            if len(self.cells) == 2:
                self.rows.append(self.cells)
            self.cells = []

    def handle_data(self, data):
        if self.cell is not None and not self.ignore:
            self.cell.append(data)


def _mac(value):
    parts = value.strip().replace(':', '-').split('-')
    if len(parts) != 6:
        return ''
    try:
        # JK-E02 renders decimal octets with hyphens, including the case where
        # every decimal octet happens to have two digits. Colons/hex letters
        # remain unambiguous hexadecimal representations.
        if ':' not in value and all(re.fullmatch(r'\d{1,3}', part) for part in parts):
            values = [int(part, 10) for part in parts]
        elif all(re.fullmatch(r'[\da-fA-F]{2}', part) for part in parts):
            values = [int(part, 16) for part in parts]
        else:
            return ''
        if any(value > 255 for value in values) or not any(values) or values[0] & 1:
            return ''
        return '-'.join(f'{value:02X}' for value in values)
    except ValueError:
        return ''


def _parse_status(html):
    parser = _StatusPage()
    parser.feed(html)
    result = {}
    keys = {'macaddress': 'mac', 'ipaddress': 'reportedIp', 'subnetmask': 'mask', 'gateway': 'gateway', 'dhcp': 'dhcp'}
    for key, value in parser.rows:
        field = keys.get(re.sub(r'\s+', '', key).lower())
        if field == 'mac':
            if normalized := _mac(value):
                result[field] = normalized
        elif field in ('reportedIp', 'mask', 'gateway'):
            try:
                normalized = str(ipaddress.IPv4Address(value))
                if field == 'mask':
                    ipaddress.IPv4Network('0.0.0.0/' + normalized)
                result[field] = normalized
            except (ValueError, TypeError):
                pass
        elif field == 'dhcp' and value.lower() in ('enabled', 'disabled'):
            result[field] = value.lower() == 'enabled'
    return result


def _probe_device(ip, *, source_address, deadline, cancel, tcp_probe=None, http_get=None):
    tcp_probe = tcp_probe or _tcp_open
    http_get = http_get or _http_get
    options = {'source_address': source_address, 'deadline': deadline, 'cancel': cancel}
    ports = [port for port in (9100, 631) if tcp_probe(ip, port, **options)]
    if not ports:
        return None
    row = {'ip': ip, 'ports': ports, 'kind': 'raw-tcp' if 9100 in ports else 'ipp-candidate',
           'displayName': '网络打印端口（型号待确认）' if 9100 in ports else 'IPP 服务端口（待确认）',
           'model': '', 'mac': '', 'mask': '', 'gateway': '', 'queueNames': []}
    try:
        _remaining(deadline, cancel)
        parser = _ModulePage()
        parser.feed(http_get(ip, '/', **options))
        if parser.recognized:
            row['kind'] = 'jk-e02'
            row['displayName'] = 'JK-E02 网络打印模块（型号待确认）'
            row['module'] = 'JK-E02'
            row.update(_parse_status(http_get(ip, '/port_stat.htm', **options)))
            if row.get('reportedIp') not in (None, ip):
                row['identityWarning'] = '设备网页报告地址与访问地址不同，请核对设备。'
    except (OSError, TimeoutError, ValueError):
        # A web failure never hides the observed port or claims a model identity.
        pass
    return row


def _deduplicate(rows):
    result = {}
    for row in sorted(rows, key=lambda item: (9100 not in item['ports'], int(ipaddress.IPv4Address(item['ip'])))):
        key = row.get('mac') or row['ip']
        if key not in result:
            result[key] = row
        elif row['ip'] != result[key]['ip']:
            result[key].setdefault('alternateIps', []).append(row['ip'])
    return sorted(result.values(), key=lambda item: int(ipaddress.IPv4Address(item['ip'])))


def discover(interface: dict, network: str, cancel: threading.Event | None = None, ip: str | None = None,
             *, local_addresses=(), timeout_seconds=MAX_SECONDS, max_workers=MAX_WORKERS, probe_fn=None):
    """Discover at most 254 local hosts, returning partial results on cancellation.

    The caller must refresh and validate the selected interface against actual
    Windows inventory before invoking this function. No queue is created here.
    """
    source, selected, addresses = _targets(interface, network, ip, local_addresses)
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= MAX_SECONDS:
        raise DiscoveryError('搜索超时必须在 0 至 20 秒之间。')
    if type(max_workers) is not int or not 1 <= max_workers <= MAX_WORKERS:
        raise DiscoveryError('搜索并发上限为 16。')
    cancel = cancel or threading.Event()
    deadline = time.monotonic() + timeout_seconds
    rows, scanned, failed = [], 0, 0
    work = probe_fn or _probe_device
    pool = ThreadPoolExecutor(max_workers=min(max_workers, len(addresses)), thread_name_prefix='printer-discovery')
    pending = set()

    def run(address):
        _remaining(deadline, cancel)
        return work(address, source_address=source, deadline=deadline, cancel=cancel)

    try:
        if not cancel.is_set():
            pending = {pool.submit(run, address) for address in addresses}
        while pending and not cancel.is_set() and time.monotonic() < deadline:
            completed, pending = wait(pending, timeout=min(0.05, max(0, deadline-time.monotonic())), return_when=FIRST_COMPLETED)
            for future in completed:
                scanned += 1
                try:
                    if row := future.result():
                        rows.append(row)
                except (OSError, TimeoutError, ValueError):
                    failed += 1
    finally:
        for future in pending:
            future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
    return {'devices': _deduplicate(rows), 'scanned': scanned, 'total': len(addresses),
            'cancelled': cancel.is_set(), 'timedOut': bool(pending and time.monotonic() >= deadline),
            'failed': failed, 'network': selected}
