"""item 37 — guard that the parser surfaces `participants` and `ratio`.

item 35 shipped `summarize_governance` without a unit test; this is the first. The
star fan-out + tally (item 37) depend on these two keys being present in the returned
dict. If the `governancedsl` (ANTLR `grammar.*` / `metamodel.governance`) package is
not importable in the env, the parser hits its raw-text fallback — the participant
list is then empty and the ratio None by design, so the voting-path tests are skipped.
"""
import pytest

from besser.utilities.web_modeling_editor.backend.services.governance.govdsl_runtime import (
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
    # shape for the generator), but empty/None so no star can form.
    r = summarize_governance("this is not a valid .gov policy {{{")
    assert r["policy_type"] is None
    assert r["participants"] == []
    assert r["ratio"] is None


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
