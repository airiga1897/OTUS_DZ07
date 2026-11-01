"""Clone-local ordering and retry; provider details stay in the backend."""


class PlacementExhausted(RuntimeError):
    """All candidates failed with their failed allocations confirmed absent."""


def place(backend, name: str, candidates: list[dict]) -> None:
    if backend.completed(name):
        backend.reapply(name)
        return
    preserved = backend.identities(exclude_pending=True)
    backend.recover_attempt(name)
    backend.assert_preserved(preserved)
    pending = backend.pending(name)
    choices = ([pending] if pending else []) + [c for c in candidates if c != pending]
    for candidate in choices:
        if pending != candidate:
            backend.begin(name, candidate)
        try:
            backend.apply(name)
        except Exception as error:
            backend.capture_fault(name)
            if not backend.is_capacity_error(error, name):
                raise
            # The backend must prove empty stage state AND API absence before return.
            backend.cleanup_attempt(name)
            backend.assert_preserved(preserved)
            pending = None
            continue
        backend.assert_preserved(preserved)
        backend.finish(name)
        return
    raise PlacementExhausted('No compatible placement succeeded for ' + name)
