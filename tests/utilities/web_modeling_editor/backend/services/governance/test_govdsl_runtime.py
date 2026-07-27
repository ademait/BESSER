"""Guard that the parser surfaces `participants` and `ratio`.

The star fan-out and tally depend on these two keys being present in the returned
dict. If the `governancedsl` (ANTLR `grammar.*` / `metamodel.governance`) package is
not importable in the env, the parser hits its raw-text fallback — the participant
list is then empty and the ratio None by design, so the voting-path tests are skipped.
"""
import pytest

from besser.utilities.web_modeling_editor.backend.services.governance.govdsl_runtime import (
    _detect_policy_type,
    build_default_summary,
    summarize_governance,
)

# A minimal voting-family policy mirroring what the WME generator emits: agentic lanes
# as `(Agent)` participants carrying a confidence, plus a `ratio` parameter.
_VOTING_DSL = """\
Scopes:
    Tasks :
        mergeTask
Participants:
    Individuals :
        (Agent) Coder {
            confidence : 0.8
        },
        (Agent) Reviewer {
            confidence : 0.6
        }
MajorityPolicy mergePolicy {
    Scope: mergeTask
    DecisionType as BooleanDecision
    Participant list : Coder, Reviewer
    Parameters:
        ratio : 0.5
}
"""


def _parser_available() -> bool:
    try:
        import grammar.govdslLexer  # noqa: F401
        import metamodel.governance  # noqa: F401
        return True
    except Exception:
        return False


def test_empty_input_returns_none():
    assert summarize_governance("") is None
    assert summarize_governance("   \n ") is None


def test_fallback_path_has_empty_participants_and_no_ratio():
    # Unparseable text → raw-text fallback. The keys must still be present (uniform
    # shape for the generator), but empty/None so no star can form here; the caller
    # turns the `unparseable` signal into a real default policy over the producers.
    r = summarize_governance("this is not a valid .gov policy {{{")
    assert r["policy_type"] is None
    assert r["participants"] == []
    assert r["ratio"] is None
    assert r["unparseable"] is True
    assert r["detected_policy_type"] is None   # no policy-type keyword in the text


# ---------------------------------------------------------------------------
# type-preserving default policy on an unparseable .gov.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kw", [
    "VotingPolicy", "MajorityPolicy", "AbsoluteMajorityPolicy",
    "LeaderDrivenPolicy", "ConsensusPolicy", "LazyConsensusPolicy",
])
def test_detect_policy_type_finds_each_keyword(kw):
    # The keyword survives a broken body (the common failure is the missing //-comment
    # lexer rule, which leaves the type keyword intact), so it is recoverable from raw text.
    assert _detect_policy_type(f"// header\n{kw} broken {{{{{{ not valid") == kw


def test_detect_policy_type_longest_match_wins():
    # 'AbsoluteMajorityPolicy' must not be misread as 'MajorityPolicy', nor
    # 'LazyConsensusPolicy' as 'ConsensusPolicy'.
    assert _detect_policy_type("AbsoluteMajorityPolicy p { ... }") == "AbsoluteMajorityPolicy"
    assert _detect_policy_type("LazyConsensusPolicy p { ... }") == "LazyConsensusPolicy"


def test_detect_policy_type_none_when_absent():
    assert _detect_policy_type("no policy keyword at all {{{") is None
    assert _detect_policy_type("") is None


def test_unparseable_signal_carries_detected_type():
    r = summarize_governance("// hdr\nAbsoluteMajorityPolicy totally broken {{{")
    assert r["unparseable"] is True
    assert r["detected_policy_type"] == "AbsoluteMajorityPolicy"


@pytest.mark.parametrize("ptype", ["VotingPolicy", "MajorityPolicy", "AbsoluteMajorityPolicy"])
def test_build_default_summary_voting_type_preserved_with_default_ratio(ptype):
    s = build_default_summary(ptype, ["AgentA", "AgentB"], raw_text="raw")
    assert s["policy_type"] == ptype
    assert s["ratio"] == 0.5                       # voting family → documented default
    assert s["requires_human"] is False
    assert s["synthesized_default"] is True
    assert [p["name"] for p in s["participants"]] == ["AgentA", "AgentB"]
    assert all(p["kind"] == "agent" and p["confidence"] is None for p in s["participants"])
    assert s["raw"] == "raw"


@pytest.mark.parametrize("ptype", ["LeaderDrivenPolicy", "ConsensusPolicy", "LazyConsensusPolicy"])
def test_build_default_summary_non_voting_type_has_no_ratio(ptype):
    s = build_default_summary(ptype, ["AgentA"], raw_text="raw")
    assert s["policy_type"] == ptype
    assert s["ratio"] is None                      # non-voting → no ratio
    # a non-voting merge must NOT ask the LLM to narrate a vote
    assert "Votes:" not in s["instruction"]


def test_build_default_summary_no_keyword_falls_back_to_majority():
    s = build_default_summary(None, ["AgentA", "AgentB"])
    assert s["policy_type"] == "MajorityPolicy"
    assert s["ratio"] == 0.5


@pytest.mark.skipif(not _parser_available(),
                    reason="governancedsl (grammar.* / metamodel.governance) not importable")
def test_voting_policy_surfaces_participants_and_ratio():
    r = summarize_governance(_VOTING_DSL)
    assert r["policy_type"] == "MajorityPolicy"
    assert r["ratio"] == 0.5
    names = {p["name"] for p in r["participants"]}
    assert names == {"Coder", "Reviewer"}
    confidences = {p["name"]: p["confidence"] for p in r["participants"]}
    assert confidences["Coder"] == 0.8
    assert confidences["Reviewer"] == 0.6


@pytest.mark.skipif(not _parser_available(),
                    reason="governancedsl (grammar.* / metamodel.governance) not importable")
def test_voting_policy_instruction_asks_for_vote_audit():
    # A voting policy's merge instruction keeps the auditable 'Votes:' directive (used on
    # the degraded fallback path when no producer resolves to a running service).
    r = summarize_governance(_VOTING_DSL)
    assert "Votes:" in r["instruction"]


@pytest.mark.skipif(not _parser_available(),
                    reason="governancedsl (grammar.* / metamodel.governance) not importable")
def test_non_voting_policy_instruction_forbids_fabricated_vote():
    # A LeaderDriven (non-voting) merge has NO vote. Its instruction must NOT ask for a
    # 'Votes:'/tally section, or the synthesis LLM fabricates one ("3 Approve, 0 ...").
    r = summarize_governance(_VOTING_DSL.replace("MajorityPolicy", "LeaderDrivenPolicy"))
    assert r["policy_type"] == "LeaderDrivenPolicy"
    assert "Votes:" not in r["instruction"]
    assert "Do NOT invent a vote" in r["instruction"]
