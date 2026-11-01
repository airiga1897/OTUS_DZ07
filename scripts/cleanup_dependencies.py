"""Dependencies of retained Selectel stages, checked before deleting parents."""


def children(stages: dict, nodes: dict) -> dict[str, set[str]]:
    result = {name: set() for name in stages}
    networks = {n for n in stages if n.startswith('network-')}
    connections = {n for n in stages if n.startswith('connection-')}
    for name, record in stages.items():
        if name in nodes:
            pool = record.get('candidate', {}).get('region')
            # Unknown placement must preserve every potential parent.
            parents = {p for p in networks | connections if not pool or p.split('-', 1)[1] == pool}
        elif name in connections:
            parents = {'global', 'network-' + name.removeprefix('connection-')}
        elif name in networks or name == 'global':
            parents = set()
        else:
            raise ValueError('Unknown cleanup stage; preserve recovery data')
        for parent in parents & result.keys():
            result[parent].add(name)
    return result
