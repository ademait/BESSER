from besser.generators.docker_compose.docker_compose_generator import (
    _a2a_descriptor,
    _a2a_descriptor_from_tags,
    _resolve_peer_service,
)


class _S:
    def __init__(self, name):
        self.name = name
        self.body = None


class _A:
    def __init__(self, name, state_names):
        self.name = name
        self.states = [_S(n) for n in state_names]


def test_self_referential_from_does_not_demote_entry():
    # Supervisor with a stray self-referential `from_supervisor` (item 22) + a real
    # outbound `to_coder`. It must stay an ENTRY, and the self-from must be ignored.
    sup = _A('Supervisor', ['coordinate_work', 'from_supervisor', 'to_coder'])
    services = {'supervisor', 'coder', 'reviewer'}
    d = _a2a_descriptor(sup, services, self_service='supervisor')
    assert d['role'] == 'entry'
    assert d['to_peers'] == ['coder']


def test_real_inbound_peer_makes_worker():
    coder = _A('Coder', ['write_code', 'from_supervisor', 'to_reviewer'])
    services = {'supervisor', 'coder', 'reviewer'}
    d = _a2a_descriptor(coder, services, self_service='coder')
    assert d['role'] == 'worker'
    assert d['to_peers'] == ['reviewer']


def test_non_service_from_human_is_ignored():
    # `from_Human` resolves to no service → entry stays entry (pre-existing behavior).
    sup = _A('Supervisor', ['coordinate_work', 'from_human', 'to_coder'])
    services = {'supervisor', 'coder'}
    d = _a2a_descriptor(sup, services, self_service='supervisor')
    assert d['role'] == 'entry'


# ---------------------------------------------------------------------------
# item 10 — `peers` shim on the legacy descriptor (template single-path)
# ---------------------------------------------------------------------------

def test_legacy_descriptor_emits_plain_channel_peers_shim():
    sup = _A('Supervisor', ['coordinate_work', 'to_coder', 'to_reviewer'])
    services = {'supervisor', 'coder', 'reviewer'}
    d = _a2a_descriptor(sup, services, self_service='supervisor')
    assert d['source'] == 'convention'
    # one peer entry per to_peer, all plain-channel (kind=None) → renders as today
    assert [p['service'] for p in d['peers']] == d['to_peers']
    assert all(p['kind'] is None for p in d['peers'])


# ---------------------------------------------------------------------------
# item 10 — tag-aware descriptor (_a2a_descriptor_from_tags)
# ---------------------------------------------------------------------------

def _tagged(name, outbound=None, inbound=None):
    a = _A(name, [])
    a._a2a = {"outbound": outbound or [], "inbound": inbound or []}
    return a


def test_tags_entry_with_delegate_peer():
    sup = _tagged('Supervisor', outbound=[
        {"peer": "AgentCoder", "ref": "u", "order": 1, "kind": "delegates", "state": "coordinate"},
    ])
    services = {'agent_supervisor', 'agent_coder'}
    d = _a2a_descriptor_from_tags(sup, services, self_service='agent_supervisor')
    assert d['source'] == 'tags'
    assert d['role'] == 'entry'
    assert d['to_peers'] == ['agent_coder']
    assert d['peers'][0]['kind'] == 'delegates'
    assert d['peers'][0]['service'] == 'agent_coder'


def test_tags_inbound_makes_worker():
    coder = _tagged(
        'Coder',
        outbound=[{"peer": "AgentReviewer", "ref": "u", "order": 1,
                   "kind": "supervises", "state": "write_code"}],
        inbound=[{"peer": "AgentSupervisor", "ref": "u", "order": 9999, "kind": "delegates"}],
    )
    services = {'agent_supervisor', 'agent_coder', 'agent_reviewer'}
    d = _a2a_descriptor_from_tags(coder, services, self_service='agent_coder')
    assert d['role'] == 'worker'
    assert d['to_peers'] == ['agent_reviewer']
    assert d['inbound'] == ['agent_supervisor']


def test_tags_dangling_peer_dropped():
    sup = _tagged('Supervisor', outbound=[
        {"peer": "GhostPeer", "ref": "", "order": 1, "kind": "delegates", "state": "s"},
    ])
    services = {'agent_supervisor'}
    d = _a2a_descriptor_from_tags(sup, services, self_service='agent_supervisor')
    assert d['to_peers'] == []          # GhostPeer not in services → dropped, no raise
    assert d['role'] == 'entry'


def test_tags_ordered_and_deduped():
    sup = _tagged('Supervisor', outbound=[
        {"peer": "AgentB", "ref": "u", "order": 2, "kind": "collaborates", "state": "s"},
        {"peer": "AgentA", "ref": "u", "order": 1, "kind": "delegates", "state": "s"},
        {"peer": "AgentA", "ref": "u", "order": 3, "kind": "delegates", "state": "s"},
    ])
    services = {'agent_supervisor', 'agent_a', 'agent_b'}
    d = _a2a_descriptor_from_tags(sup, services, self_service='agent_supervisor')
    # input order preserved (parser already order-sorted upstream); deduped on service
    assert d['to_peers'] == ['agent_b', 'agent_a']


def test_resolve_peer_service_by_name():
    assert _resolve_peer_service({"peer": "AgentCoder"}, {"agent_coder"}) == "agent_coder"
    assert _resolve_peer_service({"peer": "Ghost"}, {"agent_coder"}) is None
