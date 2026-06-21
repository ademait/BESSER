"""item 37 — the candidate PRODUCERS of a merging gateway are derived from the BPMN
sequence flows that flow INTO it (source task → owning lane → agent), decoupled from
the policy's voter list. Covers ``_producer_agent_names``."""
# Prime the backend `services` package before the router so its __init__ resolves the
# generation_router ↔ github_deploy_api cycle in the order the app boots uses (importing
# the router first would catch it partially initialized).
import pytest  # noqa: E402

import besser.utilities.web_modeling_editor.backend.services  # noqa: F401

from besser.utilities.web_modeling_editor.backend.routers.generation_router import (  # noqa: E402
    _attach_governance_to_agents,
    _producer_agent_names,
)


class _Agent:
    def __init__(self, name):
        self.name = name


# A tiny BPMN: coder-lane task and reviewer-lane task both flow into the merging
# gateway (owned by the reviewer lane). Producers must be {AgentCoder, AgentReviewer}.
GW = "gw1"
ITEMS = {
    "laneC": {"id": "laneC", "type": "BPMNSwimlane", "agentDiagramRef": "refC"},
    "laneR": {"id": "laneR", "type": "BPMNSwimlane", "agentDiagramRef": "refR"},
    "taskC": {"id": "taskC", "type": "BPMNTask", "owner": "laneC"},
    "taskR": {"id": "taskR", "type": "BPMNTask", "owner": "laneR"},
    GW: {"id": GW, "type": "BPMNGateway", "owner": "laneR"},
}
LANE_REF = {"laneC": "refC", "laneR": "refR"}
AGENTS = {"refC": _Agent("AgentCoder"), "refR": _Agent("AgentReviewer")}


def _flow(src, tgt, flow_type="sequence"):
    return {"type": "BPMNFlow", "flowType": flow_type,
            "source": {"element": src}, "target": {"element": tgt}}


def test_producers_are_incoming_flow_sources():
    rels = [_flow("taskC", GW), _flow("taskR", GW)]
    names = _producer_agent_names(GW, rels, ITEMS, LANE_REF, AGENTS)
    assert sorted(names) == ["AgentCoder", "AgentReviewer"]


def test_outgoing_flow_is_not_a_producer():
    # A flow LEAVING the gateway must not make its target a producer.
    rels = [_flow("taskC", GW), _flow(GW, "taskR")]
    names = _producer_agent_names(GW, rels, ITEMS, LANE_REF, AGENTS)
    assert names == ["AgentCoder"]


def test_non_sequence_flow_into_gateway_is_ignored():
    rels = [_flow("taskC", GW), _flow("taskR", GW, flow_type="message")]
    names = _producer_agent_names(GW, rels, ITEMS, LANE_REF, AGENTS)
    assert names == ["AgentCoder"]


def test_missing_flow_type_defaults_to_sequence():
    rel = {"type": "BPMNFlow", "source": {"element": "taskC"}, "target": {"element": GW}}
    names = _producer_agent_names(GW, [rel], ITEMS, LANE_REF, AGENTS)
    assert names == ["AgentCoder"]


def test_duplicate_branches_from_same_lane_collapse_to_one_agent():
    rels = [_flow("taskC", GW), _flow("taskC", GW)]
    names = _producer_agent_names(GW, rels, ITEMS, LANE_REF, AGENTS)
    assert names == ["AgentCoder"]


def test_dangling_source_is_skipped():
    rels = [_flow("ghost", GW), _flow("taskC", GW)]
    names = _producer_agent_names(GW, rels, ITEMS, LANE_REF, AGENTS)
    assert names == ["AgentCoder"]


# ---------------------------------------------------------------------------
# Phase 1 — an unparseable .gov on a gateway is recovered into a real,
# type-preserving DEFAULT policy over the collaboration participants
# (gateway owner + the agents flowing into it). Covers _attach_governance_to_agents.
# ---------------------------------------------------------------------------

class _Input:
    """Minimal stand-in for ProjectInput: only `.diagrams` is read."""
    def __init__(self, diagrams):
        self.diagrams = diagrams


def _bpmn_input(gov_text):
    # coder-lane and reviewer-lane tasks both flow into a merging gateway owned by the
    # reviewer lane; the gateway carries `gov_text` as its governanceDsl.
    elements = {
        "laneC": {"id": "laneC", "type": "BPMNSwimlane", "agentDiagramRef": "refC"},
        "laneR": {"id": "laneR", "type": "BPMNSwimlane", "agentDiagramRef": "refR"},
        "taskC": {"id": "taskC", "type": "BPMNTask", "owner": "laneC"},
        "taskR": {"id": "taskR", "type": "BPMNTask", "owner": "laneR"},
        GW: {"id": GW, "type": "BPMNGateway", "owner": "laneR", "governanceDsl": gov_text},
    }
    relationships = {
        "f1": _flow("taskC", GW),
        "f2": _flow("taskR", GW),
    }
    return _Input({"BPMN": [{"model": {"elements": elements, "relationships": relationships}}]})


def _attach_and_get_owner_gov(gov_text):
    agents = {"refC": _Agent("AgentCoder"), "refR": _Agent("AgentReviewer")}
    _attach_governance_to_agents(_bpmn_input(gov_text), agents)
    owner = agents["refR"]                      # the gateway's owning lane → owner agent
    assert getattr(owner, "_governance", None)  # a policy was attached
    return owner._governance[0]


def test_unparseable_gov_builds_default_over_collaboration_participants():
    gov = _attach_and_get_owner_gov("// hdr\nMajorityPolicy m broken {{{ not valid")
    assert gov["synthesized_default"] is True
    assert gov["policy_type"] == "MajorityPolicy"      # type recovered from the raw text
    assert gov["ratio"] == 0.5
    # participants = owner (AgentReviewer) + producers (AgentCoder, AgentReviewer)
    assert {p["name"] for p in gov["participants"]} == {"AgentCoder", "AgentReviewer"}
    # producers are still the BPMN branches into the gateway
    assert sorted(gov["producers"]) == ["AgentCoder", "AgentReviewer"]


@pytest.mark.parametrize("kw", [
    "VotingPolicy", "MajorityPolicy", "AbsoluteMajorityPolicy",
    "LeaderDrivenPolicy", "ConsensusPolicy", "LazyConsensusPolicy",
])
def test_unparseable_gov_preserves_each_policy_type(kw):
    gov = _attach_and_get_owner_gov(f"// hdr\n{kw} p broken {{{{{{ nope")
    assert gov["policy_type"] == kw
    assert gov["synthesized_default"] is True


def test_unparseable_gov_without_keyword_defaults_to_majority():
    gov = _attach_and_get_owner_gov("totally unparseable, no keyword {{{")
    assert gov["policy_type"] == "MajorityPolicy"
    assert gov["synthesized_default"] is True
