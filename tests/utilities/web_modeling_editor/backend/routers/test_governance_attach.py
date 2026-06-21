"""item 37 — the candidate PRODUCERS of a merging gateway are derived from the BPMN
sequence flows that flow INTO it (source task → owning lane → agent), decoupled from
the policy's voter list. Covers ``_producer_agent_names``."""
# Prime the backend `services` package before the router so its __init__ resolves the
# generation_router ↔ github_deploy_api cycle in the order the app boots uses (importing
# the router first would catch it partially initialized).
import besser.utilities.web_modeling_editor.backend.services  # noqa: F401

from besser.utilities.web_modeling_editor.backend.routers.generation_router import (  # noqa: E402
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
