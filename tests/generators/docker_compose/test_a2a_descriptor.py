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


# ---------------------------------------------------------------------------
# item 37 — governed voting owner fans out to the PRODUCER ∪ VOTER star, with
# producers (BPMN flows into the gateway) and voters (policy participants) decoupled.
# ---------------------------------------------------------------------------

def _voting_gov(participants, producers=None, policy_type="VotingPolicy"):
    return {"policy_type": policy_type, "ratio": 0.5, "requires_human": False,
            "participants": participants, "producers": producers or [],
            "instruction": "...", "summary": "...", "raw": "..."}


def _p(name, confidence=None, kind="agent"):
    return {"name": name, "kind": kind, "confidence": confidence, "roles": []}


def test_producers_and_voters_are_decoupled():
    # Coder produces but does NOT vote; Reviewer votes but does NOT produce; the owner
    # (Supervisor) votes (it is a participant) but does not produce. The star is the
    # union of producers and non-owner voters: {coder} ∪ {reviewer}.
    sup = _A('Supervisor', ['coordinate_work', 'to_coder'])
    sup._governance = [_voting_gov(
        participants=[_p("Supervisor", 0.9), _p("Reviewer", 0.6)],
        producers=["Coder"])]
    d = _a2a_descriptor(sup, {"supervisor", "coder", "reviewer"}, self_service="supervisor")
    gov = d["governance"]
    assert sorted(d["to_peers"]) == ["coder", "reviewer"]      # union, not just topology
    assert gov["producer_services"] == ["coder"]              # round-1 targets
    assert gov["weights"] == {"supervisor": 0.9, "reviewer": 0.6}  # voters only
    assert "coder" not in gov["weights"]                       # a producer is not a voter
    assert gov["owner_produces"] is False                      # Supervisor not a producer
    assert gov["owner_votes"] is True                          # Supervisor is a participant
    assert gov["is_voting"] is True
    assert gov["engine_src"]                                   # baked source present
    assert gov["self_service"] == "supervisor"


def test_owner_produces_when_it_is_a_producer():
    # Owner sits on an incoming branch → it is a producer; it self-produces in-process
    # (no peer) and still votes because it is also a participant.
    sup = _A('Supervisor', [])
    sup._governance = [_voting_gov(
        participants=[_p("Supervisor", 0.9), _p("Coder", 0.8)],
        producers=["Supervisor", "Coder"])]
    d = _a2a_descriptor(sup, {"supervisor", "coder"}, self_service="supervisor")
    gov = d["governance"]
    assert gov["owner_produces"] is True
    assert gov["owner_votes"] is True
    assert gov["producer_services"] == ["coder"]              # owner produces in-proc, not a peer
    assert sorted(d["to_peers"]) == ["coder"]


def test_owner_does_not_vote_when_absent_from_participants():
    # Owner is the judge but is NOT listed in the policy → it must not cast a ballot.
    sup = _A('Supervisor', [])
    sup._governance = [_voting_gov(
        participants=[_p("Coder", 0.8), _p("Reviewer", 0.6)],
        producers=["Coder", "Reviewer"])]
    d = _a2a_descriptor(sup, {"supervisor", "coder", "reviewer"}, self_service="supervisor")
    gov = d["governance"]
    assert gov["owner_votes"] is False
    assert "supervisor" not in gov["weights"]
    assert sorted(d["to_peers"]) == ["coder", "reviewer"]


def test_unresolved_producer_and_voter_are_recorded():
    # An unresolved producer AND an unresolved voter both surface as visible abstains;
    # Coder resolves so the star still runs.
    sup = _A('Supervisor', [])
    sup._governance = [_voting_gov(
        participants=[_p("Coder", 0.8), _p("GhostVoter", 0.5)],
        producers=["Coder", "GhostProducer"])]
    d = _a2a_descriptor(sup, {"supervisor", "coder"}, self_service="supervisor")
    gov = d["governance"]
    assert gov["unresolved"] == ["GhostProducer", "GhostVoter"]
    assert gov["producer_services"] == ["coder"]
    assert [p["service"] for p in d["peers"]] == ["coder"]


def test_no_candidates_degrades_to_topology():
    # Voting policy but no producer resolves and the owner is not a producer →
    # _governance_star returns None → the item-35 single-round topology is kept.
    sup = _A('Supervisor', ['coordinate_work', 'to_coder'])
    sup._governance = [_voting_gov(
        participants=[_p("Coder", 0.8)], producers=["Ghost"])]
    d = _a2a_descriptor(sup, {"supervisor", "coder"}, self_service="supervisor")
    assert d["to_peers"] == ["coder"]                          # topology, not a star
    assert "is_voting" not in d["governance"]                  # raw summary, unaugmented


def test_non_voting_policy_keeps_topology_peers():
    # LeaderDrivenPolicy → _governance_star returns None → topology unchanged.
    sup = _A('Supervisor', ['coordinate_work', 'to_coder'])
    sup._governance = [_voting_gov([_p("Coder", 0.8)], producers=["Coder"],
                                   policy_type="LeaderDrivenPolicy")]
    d = _a2a_descriptor(sup, {"supervisor", "coder", "reviewer"}, self_service="supervisor")
    assert d["to_peers"] == ["coder"]                          # topology, not the star
    assert "weights" not in d["governance"]                    # raw summary, unaugmented
    assert "is_voting" not in d["governance"]


def test_no_governance_descriptor_unchanged():
    sup = _A('Supervisor', ['coordinate_work', 'to_coder'])
    d = _a2a_descriptor(sup, {"supervisor", "coder"}, self_service="supervisor")
    assert d["to_peers"] == ["coder"]
    assert d["governance"] is None


def test_synthesized_default_policy_forms_a_star_with_unit_weights():
    # Phase 1 — a default policy synthesized for an unparseable .gov carries participants
    # with confidence None (no author weights). The star must still form, each voter
    # weighted 1.0 (the _governance_star confidence fallback).
    from besser.utilities.web_modeling_editor.backend.services.governance.govdsl_runtime import (
        build_default_summary,
    )
    sup = _A('Supervisor', [])
    summary = build_default_summary("MajorityPolicy", ["Coder", "Supervisor"], raw_text="raw")
    summary["producers"] = ["Coder", "Supervisor"]
    sup._governance = [summary]
    d = _a2a_descriptor(sup, {"supervisor", "coder"}, self_service="supervisor")
    gov = d["governance"]
    assert gov["is_voting"] is True
    assert gov["weights"] == {"coder": 1.0, "supervisor": 1.0}   # confidence None → 1.0
    assert gov["owner_votes"] is True
    assert gov["producer_services"] == ["coder"]


def test_governed_voting_star_via_tags_path():
    # the preferred (tags) builder must apply the same star override.
    sup = _tagged('Supervisor', outbound=[
        {"peer": "Coder", "ref": "u", "order": 1, "kind": "delegates", "state": "s"},
    ])
    sup._governance = [_voting_gov([_p("Coder", 0.8), _p("Reviewer", 0.6)],
                                   producers=["Coder"])]
    d = _a2a_descriptor_from_tags(sup, {"supervisor", "coder", "reviewer"},
                                  self_service="supervisor")
    assert sorted(d["to_peers"]) == ["coder", "reviewer"]
    assert d["governance"]["is_voting"] is True
    assert d["governance"]["weights"]["reviewer"] == 0.6
