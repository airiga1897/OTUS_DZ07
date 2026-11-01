"""Retained plan evidence: structure and comparisons, never arbitrary values."""
HINT_FIELDS = {'group', 'different_host', 'same_host', 'query', 'target_cell',
               'different_cell', 'build_near_host_ip', 'additional_properties'}


def hints(value: object, expected: object) -> dict:
    result = {'type': type(value).__name__}
    if not isinstance(value, list):
        return result
    result['count'] = len(value)
    result['items'] = []
    for row in value[:4]:
        if not isinstance(row, dict):
            result['items'].append({'type': type(row).__name__})
            continue
        result['items'].append({'group_matches_saved': bool(expected) and row.get('group') == expected,
            'unknown_fields': len(set(row) - HINT_FIELDS),
            'fields': {key: {'type': type(val).__name__, 'empty': val is None or val == '' or val == [] or val == {}}
                       for key, val in row.items() if key in HINT_FIELDS}})
    return result


def summarize(plan: dict, inputs: dict) -> dict:
    result = {}
    for section in ('resource_drift', 'resource_changes'):
        result[section] = []
        for item in plan.get(section, []):
            before, after = item['change'].get('before') or {}, item['change'].get('after') or {}
            changed = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
            row = {'address': item['address'], 'type': item.get('type'),
                   'actions': item['change']['actions'], 'changed_fields': changed}
            if item.get('type') == 'openstack_compute_instance_v2' and 'scheduler_hints' in changed:
                row['scheduler_hints'] = {'before': hints(before.get('scheduler_hints'), inputs.get('server_group_id')),
                    'after': hints(after.get('scheduler_hints'), inputs.get('server_group_id')),
                    'configured_distinct_hosts': inputs.get('require_distinct_hosts') is True}
            result[section].append(row)
    return result
