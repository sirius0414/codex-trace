"""持久化证据的纯函数工具；未知值保留为空，不用默认值补成事实。"""
from datetime import datetime
import hashlib
import json
import math


def identity(*parts):
    """根据明确的来源身份生成稳定键，不依赖内容相似度或采集时间。"""
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()[:32]


def timestamp(value):
    """只接受带时区且非负的记录时间，避免按运行机器时区解释历史。"""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return round(parsed.timestamp() * 1000) if parsed.tzinfo and parsed.timestamp() >= 0 else None
    except (ValueError, OverflowError):
        return None


def milliseconds(value):
    """输出 JSON 可安全表达的整数毫秒；布尔值和非法时间保持未知。"""
    return round(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 9007199254740991 else None


def source_ref(row, association='explicitMetadata'):
    """复制已校验的来源引用，允许调用方标注本次关联的证据类型。"""
    # SourceRef 只含标量元数据；原始正文不能混入可公开的来源引用。
    source = dict(row['source'])
    source['association'] = association
    return source


def state(value, scope, scope_id, source, at, explanation, *, confirmed=True):
    """保留指定作用域的状态证据；未确认时仅记录 lastKnown。

    缺失时间保持 null，零只表示明确记录的零；历史启动记录也不能证明仍在运行。
    """
    evidence = {'scope': scope, 'scopeId': scope_id, 'status': value, 'basis': 'persistedLifecycle',
                'observedAt': milliseconds(at), 'source': source, 'explanation': explanation}
    return {'value': value if confirmed else 'unknown', 'evidence': [evidence],
            'lastKnown': value, 'explanation': explanation}
