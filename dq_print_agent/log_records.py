"""Read service-log records without exposing credentials or splitting tracebacks."""
from datetime import datetime
import re


_RECORD = re.compile(
    r'^\[?(\d{4}[-/]\d{2}[-/]\d{2})[ T](\d{2}:\d{2}:\d{2}(?:[.,]\d+)?'
    r'(?:Z|[+-]\d{2}:?\d{2})?)\]?\s+(?:\|\s*)?\[?'
    r'(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL|NOTSET)\]?(?=\s|$)'
    r'(?:\s\|)?[ \t]?(.*)$', re.IGNORECASE)
_CREDENTIAL = re.compile(
    r'authorization|credential|(?:access.?|api.?)?token[\s"\x27:=]|password|secret|signature|pa1\.[1-9]\d*\.', re.I)
_LINK = re.compile(r'https?://\S+', re.I)


def redact_log_line(line):
    if _CREDENTIAL.search(line):
        # The verified timestamp and level still identify the hidden message.
        match = _RECORD.match(line)
        prefix = line[:match.start(4)] if match else ''
        return prefix + '[含凭据字段的日志已隐藏]'
    return _LINK.sub('[链接已隐藏]', line)


def latest_log_entries(text):
    """Yield recent records in descending time order, keeping their sanitized detail."""
    records = []
    partial = []
    for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        match = _RECORD.match(line)
        if match:
            day, clock, level, message = match.groups()
            try:
                moment = datetime.fromisoformat(day.replace('/', '-') + 'T' + clock.replace(',', '.'))
                if moment.tzinfo is not None:
                    moment = moment.astimezone().replace(tzinfo=None)
            except (ValueError, OverflowError, OSError):
                pass  # An unrecognized header is still available as raw text.
            else:
                records.append((moment, level.upper(), message, [line]))
                continue
        if records:
            records[-1][3].append(line)
        else:
            partial.append(line)

    # Windows can write several events in one millisecond. For ties show the
    # most recently appended record first, keeping each traceback intact.
    for moment, level, message, lines in sorted(reversed(records), key=lambda record: record[0], reverse=True):
        yield moment.date().isoformat(), moment.strftime('%H:%M:%S'), level, message, '\n'.join(lines)
    for line in partial:
        yield '', '', '', line, line
