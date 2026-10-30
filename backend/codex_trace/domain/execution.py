"""保留轮次的执行包络，供前端折叠轮次之间的等待；不推断模型或工具阶段。

输入为已校验的历史轮次及持久化记录；输出只附加 Timing 区间，不参与执行状态归约。
raw 的中断／重新启动是独立边界，API 的整轮摘要不能跨过这些边界补成长区间。
"""
from dataclasses import dataclass

from .evidence import milliseconds, source_ref, timestamp


_STARTS = {'task_started', 'turn_started'}
_ENDS = {'task_complete', 'turn_complete', 'turn_aborted'}


def _seconds(value):
    """来源的轮次 epoch 字段以秒表示；转换后仍须满足 JSON 安全整数毫秒边界。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 9007199254740991 / 1000:
        return None
    return milliseconds(value * 1000)


@dataclass
class _Episode:
    start: int | None
    end: int | None = None
    closed: bool = False
    kind: str | None = None
    source: dict | None = None
    unit: str = 'seconds'
    floor: int | None = None


def _boundary(payload, field, record):
    value = _seconds(payload.get(field))
    if value is not None:
        return value, 'seconds'
    return milliseconds(timestamp(record.get('timestamp'))), 'milliseconds'


def execution_intervals(thread_id, records, api_turns=()):
    """按明确 thread/turn 身份生成可定位的执行区间，缺少起点时不反推。

    只接受自身、未排除的 raw 生命周期；保留零值并拒绝非法时间。独立冷读取的
    API 状态不进入此函数：startedAt/completedAt 是保存的时间事实，不证明仍在运行。
    未结束区间的 endMs 保持 null；调用方不得用采集时间替它延长。
    """
    api = {}
    for wrapper in api_turns:
        if not isinstance(wrapper, dict) or wrapper.get('threadId') not in (None, thread_id) or wrapper.get('projectionExcluded') or wrapper.get('inherited'):
            continue
        turn = wrapper.get('turn', wrapper)
        if not isinstance(turn, dict) or turn.get('threadId') not in (None, thread_id) or turn.get('projectionExcluded') or turn.get('inherited'):
            continue
        turn_id = turn.get('id')
        if isinstance(turn_id, str) and turn_id:
            start, end = _seconds(turn.get('startedAt')), _seconds(turn.get('completedAt'))
            if turn.get('startedAt') is not None and start is None or turn.get('completedAt') is not None and end is None:
                continue
            api[turn_id] = _Episode(start, end)

    raw, active = {}, {}
    for row in sorted(records, key=lambda value: value['ordinal']):
        if row.get('projectionExcluded') or row.get('inherited') or not isinstance(row.get('source'), dict):
            continue
        if row['source'].get('threadId') not in (None, thread_id):
            continue
        record = row.get('record', {})
        payload = record.get('payload', {})
        if record.get('type') != 'event_msg' or not isinstance(payload, dict):
            continue
        if payload.get('thread_id') not in (None, thread_id):
            continue
        turn_id, kind = payload.get('turn_id'), payload.get('type')
        if not isinstance(turn_id, str) or not turn_id or kind not in _STARTS | _ENDS:
            continue
        episodes = raw.setdefault(turn_id, [])
        if kind in _STARTS:
            start, unit = _boundary(payload, 'started_at', record)
            prior = episodes[-1] if episodes else None
            floor = prior.end if prior is not None and prior.closed else None
            if prior is not None and prior.closed and floor is None and prior.start is not None:
                floor = prior.start + 1
            if start is not None and floor is not None and start < floor:
                # 恢复记录若重复了原轮次起点，改用这次启动的保存时间；两者都无效则留空，
                # 不能让一个新的开放区间重新跨过已经确认的停止边界。
                observed = milliseconds(timestamp(record.get('timestamp')))
                start = observed if observed is not None and observed >= floor else None
                unit = 'milliseconds'
            previous = active.get(turn_id)
            if previous is not None:
                # 新启动不能充当前一次启动的结束；旧的无结束片段也不能延至后续轮次。
                previous.closed, previous.kind = True, 'superseded'
            episode = _Episode(start, source=source_ref(row), unit=unit, floor=floor)
            episodes.append(episode)
            active[turn_id] = episode
            continue
        episode = active.pop(turn_id, None)
        if episode is None:
            # 完成事件可自带整轮起点；若已经见过停止边界，该起点可能属于旧一轮，
            # 不能重新拿它覆盖停止后未记录的等待。
            start = _seconds(payload.get('started_at')) if not episodes else None
            episode = _Episode(start, source=source_ref(row))
            episodes.append(episode)
        elif episode.start is None:
            candidate = _seconds(payload.get('started_at'))
            if candidate is not None and (episode.floor is None or candidate >= episode.floor):
                episode.start = candidate
        episode.end, unit = _boundary(payload, 'completed_at', record)
        episode.closed, episode.kind, episode.source = True, kind, source_ref(row)
        if unit == 'milliseconds':
            episode.unit = unit

    result = {}
    for turn_id in dict.fromkeys([*api, *raw]):
        fallback = api.get(turn_id)
        episodes = raw.get(turn_id)
        if episodes is None:
            episodes = [fallback] if fallback is not None else []
        elif fallback is not None:
            for index, episode in enumerate(episodes):
                # API 只补最外侧缺失边界，不能抹掉 raw 中的停止／恢复分段。
                if index == 0 and episode.start is None:
                    if fallback.start is not None and (episode.end is None or fallback.start <= episode.end):
                        episode.start = fallback.start
                if index == len(episodes) - 1 and episode.end is None and episode.kind not in ('turn_aborted', 'superseded'):
                    if fallback.end is not None and episode.start is not None and fallback.end >= episode.start:
                        episode.end = fallback.end
        intervals = []
        for episode in episodes:
            if episode.start is None or episode.end is not None and episode.end < episode.start:
                continue
            # 已知结束但结束时间缺失的片段不能被误当成仍开放，进而跨过人类等待。
            if episode.closed and episode.end is None:
                continue
            exact = episode.end is not None
            intervals.append({
                'startMs': episode.start, 'endMs': episode.end,
                'durationMs': episode.end - episode.start if exact else None,
                'precision': 'exact' if exact else 'startOnly', 'originalUnit': episode.unit,
                'basis': '持久化轮次启动／结束边界；不表示模型或工具阶段' if turn_id in raw
                    else 'thread/turns/list.startedAt/completedAt（Unix 秒）；不依据冷读取执行状态',
                # SourceRef 只有 API item 身份；API 轮次不能冒用 itemId，字段名写在 basis。
                'source': episode.source,
            })
        if intervals:
            result[turn_id] = intervals
    return result
