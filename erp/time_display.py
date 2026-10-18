"""Local display only; original timestamp values remain in audit evidence."""
from datetime import datetime
from django.utils import timezone
from django.utils.dateparse import parse_datetime


def local_timestamp(value,*,detailed=True):
    try:
        parsed=parse_datetime(value) if isinstance(value,str) else value
        if not isinstance(parsed,datetime):return None
        if timezone.is_naive(parsed):
            return parsed.isoformat(sep=' ',timespec='seconds')+'（时区未记录）'
        local=timezone.localtime(parsed)
        return local.isoformat(sep=' ',timespec='seconds') if detailed else local.strftime('%Y-%m-%d %H:%M')
    except (ValueError,OverflowError):
        return None
