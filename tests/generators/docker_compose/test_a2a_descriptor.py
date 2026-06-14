from besser.generators.docker_compose.docker_compose_generator import _a2a_descriptor


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
