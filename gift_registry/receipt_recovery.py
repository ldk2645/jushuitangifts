"""Persisted evidence required before a read-only missing-receipt lookup."""
from datetime import datetime, timedelta, timezone

from .workflow import PlatformOperationError


def recovery_context(item):
    evidence = item.get('evidence', {})
    if (item.get('submission_status') == 'not_submitted'
            or evidence.get('submit_clicked') is not True or evidence.get('receipt')):
        raise PlatformOperationError('缺少可补读回执的提交依据；禁止重提')
    previous = evidence.get('banniu_before_task_ids')
    if previous is not None:
        if (not isinstance(previous, list) or len(set(previous)) != len(previous)
                or any(not isinstance(value, str) or not value.isdecimal() for value in previous)):
            raise PlatformOperationError('班牛提交前任务基线无效，不能补读回执')
        return {'previous_ids': previous}
    # Older jobs did not persist the task baseline. Only a unique task in the
    # recorded click/failure window can be adopted, followed by full detail and
    # new JST ID validation. Large clock differences fail closed.
    clicked = None
    try:
        for event in item.get('events', []):
            if event.get('state') == 'submitting':
                clicked = datetime.fromisoformat(event['at'])
            elif event.get('state') == 'failed' and clicked is not None:
                failed = datetime.fromisoformat(event['at'])
                if (clicked.tzinfo is None or failed.tzinfo is None
                        or not timedelta(0) <= failed - clicked <= timedelta(minutes=10)):
                    break
                china = timezone(timedelta(hours=8))
                return {'created_from': (clicked.astimezone(china) - timedelta(seconds=120)).replace(tzinfo=None).isoformat(),
                        'created_to': (failed.astimezone(china) + timedelta(seconds=120)).replace(tzinfo=None).isoformat()}
    except (KeyError, ValueError, TypeError):
        pass
    raise PlatformOperationError('缺少班牛任务基线或有效提交时间范围，不能补读回执')
