"""Read-only classification of mihomo snapshots, with explicit unclassified bytes."""
import datetime as dt

DIRECTIONS = ('upload', 'download')
KINDS = ('proxy', 'direct', 'excluded')


def pair():
    return {key: 0 for key in DIRECTIONS}


def nonnegative(value):
    value = int(value)
    if value < 0:
        raise ValueError('Negative traffic counter')
    return value


def raw(snapshot):
    return {key: nonnegative(snapshot[key]) for key in DIRECTIONS}


def classify(chains):
    if not chains or not isinstance(chains[0], str):
        return None
    # Proxy names are case-sensitive; a user node named 'direct' is not DIRECT.
    leaf = chains[0]
    if leaf in ('DIRECT', 'DNS'):
        return 'direct'
    if leaf in ('REJECT', 'REJECT-DROP'):
        return 'excluded'
    if leaf in ('GLOBAL', 'COMPATIBLE', 'PASS'):
        return None
    return 'proxy'


def epoch(value):
    try:
        return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, AttributeError):
        return None


def watermark(flow, timestamp):
    return dict(upload=nonnegative(flow.get('upload', 0)),
                download=nonnegative(flow.get('download', 0)),
                kind=classify(flow.get('chains')), seen=timestamp)


def new_meter(snapshot, now, legacy_bytes=0):
    timestamp = now.timestamp()
    meter = dict(day=now.date().isoformat(), started_at=now.isoformat(),
                 day_started_at=now.isoformat(), created_epoch=timestamp,
                 previous=raw(snapshot), generation=snapshot.get('core_pid') or snapshot['generation'],
                 all=pair(), proxy=pair(), direct=pair(), excluded=pair(),
                 pending={kind: pair() for kind in KINDS}, flows={},
                 legacy_bytes=nonnegative(legacy_bytes), checked_at=now.isoformat(),
                 last_epoch=timestamp, reconnects=0, coverage_gaps=0,
                 last_proxy_delta=pair(), current_proxy_connections=0,
                 current_direct_connections=0)
    for flow in (snapshot.get('connections') or []):
        if flow.get('id'):
            meter['flows'][flow['id']] = watermark(flow, timestamp)
    return meter


def totals(meter):
    unknown = {key: max(0, meter['all'][key] -
                        sum(meter[kind][key] for kind in KINDS))
               for key in DIRECTIONS}
    confirmed = sum(meter['proxy'].values())
    unclassified = sum(unknown.values())
    return dict(proxy_bytes=confirmed, direct_bytes=sum(meter['direct'].values()),
                excluded_bytes=sum(meter['excluded'].values()),
                unknown_bytes=unclassified, historical_bytes=meter['legacy_bytes'],
                budget_bytes=confirmed + unclassified + meter['legacy_bytes'],
                unknown=unknown)


def update(meter, snapshot, now):
    timestamp = now.timestamp()
    day = now.date().isoformat()
    before_proxy = meter['proxy'].copy()
    crossed_offline_midnight = meter['day'] != day and timestamp - meter['last_epoch'] > 5
    if meter['day'] != day:
        meter.update(day=day, day_started_at=now.isoformat(), all=pair(),
                     proxy=pair(), direct=pair(), excluded=pair(),
                     pending={kind: pair() for kind in KINDS}, legacy_bytes=0,
                     coverage_gaps=0)
        before_proxy = pair()
    current = raw(snapshot)
    generation = snapshot.get('core_pid') or snapshot['generation']
    generation_changed = meter['generation'] != generation
    if generation_changed:
        meter['flows'] = {}
        # Old pending deltas are bounded by the old core's total; do not assign
        # them against an unrelated new core's traffic after a reset.
        meter['pending'] = {kind: pair() for kind in KINDS}
        meter['reconnects'] += 1
    if timestamp - meter['last_epoch'] > 5:
        meter['coverage_gaps'] += 1
    for key in DIRECTIONS:
        before = meter['previous'][key]
        delta = current[key] if generation_changed or current[key] < before else current[key] - before
        meter['all'][key] += delta
    old_flows = {} if crossed_offline_midnight else meter['flows']
    flows = {flow['id']: flow for flow in (snapshot.get('connections') or []) if flow.get('id')}
    meter['current_proxy_connections'] = meter['current_direct_connections'] = 0
    observations = []
    for identifier, flow in flows.items():
        mark = watermark(flow, timestamp)
        kind = mark['kind']
        if kind in ('proxy', 'direct'):
            meter['current_' + kind + '_connections'] += 1
        previous = old_flows.get(identifier)
        start = epoch(flow.get('start'))
        # Initial pre-existing traffic and missing start times cannot be
        # assigned to this observation window just from a cumulative counter.
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        count_full = start is not None and start >= max(meter['created_epoch'], midnight)
        deltas = pair()
        for key in DIRECTIONS:
            if previous is not None:
                deltas[key] = max(0, mark[key] - previous[key])
            elif count_full:
                deltas[key] = mark[key]
        if previous and previous['kind'] != kind:
            kind = None
        if kind:
            for key in DIRECTIONS:
                meter['pending'][kind][key] += deltas[key]
        if kind == 'proxy' and any(deltas.values()):
            metadata = flow.get('metadata') or {}
            host = metadata.get('host') or metadata.get('destinationIP') or '未知目标'
            observations.append(dict(host=str(host)[:160], flow_id=identifier,
                                     source_ip=str(metadata.get('sourceIP') or ''),
                                     source_port=str(metadata.get('sourcePort') or ''),
                                     process=str(metadata.get('process') or '')[:120],
                                     process_path=str(metadata.get('processPath') or '')[:512],
                                     **deltas))
        old_flows[identifier] = mark
    # Keep short-lived tombstones so an inconsistent concurrent snapshot cannot
    # cause a momentarily missing live UUID to be counted twice.
    meter['flows'] = {identifier: mark for identifier, mark in old_flows.items()
                      if timestamp - mark['seen'] < 10}
    for key in DIRECTIONS:
        available = max(0, meter['all'][key] - sum(meter[kind][key] for kind in KINDS))
        requests = sum(meter['pending'][kind][key] for kind in KINDS)
        if not requests or not available:
            continue
        allocations = {kind: meter['pending'][kind][key] for kind in KINDS}
        if requests > available:
            allocations = {kind: meter['pending'][kind][key] * available // requests
                           for kind in KINDS}
            remainder = available - sum(allocations.values())
            for kind in KINDS:
                take = min(remainder, meter['pending'][kind][key] - allocations[kind])
                allocations[kind] += take
                remainder -= take
        for kind, amount in allocations.items():
            meter[kind][key] += amount
            meter['pending'][kind][key] -= amount
    meter.update(previous=current, generation=generation,
                 checked_at=now.isoformat(), last_epoch=timestamp,
                 last_proxy_delta={key: meter['proxy'][key] - before_proxy[key] for key in DIRECTIONS})
    return observations
