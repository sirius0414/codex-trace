"""Small pure helpers shared by persisted evidence reducers."""
from datetime import datetime
import hashlib
import json
import math


def identity(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()[:32]


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return round(parsed.timestamp() * 1000) if parsed.tzinfo and parsed.timestamp() >= 0 else None
    except (ValueError, OverflowError):
        return None


def milliseconds(value):
    return round(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 9007199254740991 else None


def source_ref(row, association='explicitMetadata'):
    # Validated SourceRef has scalar fields only; payloads never enter this copy.
    source = dict(row['source'])
    source['association'] = association
    return source


def state(value, scope, scope_id, source, at, explanation, *, confirmed=True):
    """A missing evidence timestamp stays null; zero always means recorded zero."""
    evidence = {'scope': scope, 'scopeId': scope_id, 'status': value, 'basis': 'persistedLifecycle',
                'observedAt': milliseconds(at), 'source': source, 'explanation': explanation}
    return {'value': value if confirmed else 'unknown', 'evidence': [evidence],
            'lastKnown': value, 'explanation': explanation}
