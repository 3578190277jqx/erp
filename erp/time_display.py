"""Local display only; original timestamp values remain in audit evidence."""
from datetime import datetime
from django.utils import timezone
from django.utils.dateparse import parse_datetime


def local_timestamp(value):
    try:
        parsed=parse_datetime(value) if isinstance(value,str) else value
        if not isinstance(parsed,datetime):return None
        if timezone.is_naive(parsed):
            return parsed.isoformat(sep=' ',timespec='seconds')+'（时区未记录）'
        return timezone.localtime(parsed).isoformat(sep=' ',timespec='seconds')
    except (ValueError,OverflowError):
        return None
