"""Immutable acceptance-day accounting, independent of machine timezone."""
from datetime import datetime, timedelta, timezone

CHINA = timezone(timedelta(hours=8), 'Asia/Shanghai')

def business_day(created_at):
    try:
        value = datetime.fromisoformat(created_at)
        if value.tzinfo is None or value.utcoffset() is None:
            return None
        return value.astimezone(CHINA).date().isoformat()
    except (ValueError, TypeError):
        return None

def job_day(job):
    inferred = business_day(job.get('created_at'))
    fixed = job.get('business_day')
    return inferred if not fixed or fixed == inferred else None
