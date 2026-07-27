"""Parse a WME-authored governance .gov snippet at generation time and`r`nproduce a runtime instruction for the synthesizing agent.

Parsing runs HERE (BESSER backend), not in the agent container: the container only
receives the resulting instruction string. The govdsl metamodel is BESSER-BUML based,
so the parser imports cleanly wherever besser.BUML is importable. Any failure (bad
user edit, missing parser package) degrades to a raw-text instruction (Option B).
"""
import logging
import re

logger = logging.getLogger(__name__)

# Voting policy types resolved by the deterministic in-container tally. Mirrors
# docker_compose_generator._VOTING_POLICIES. Non-voting policies (leader/consensus/lazy)
# have NO vote, so their merge instruction must not ask the LLM to narrate one.
_VOTING_POLICY_TYPES = frozenset(("VotingPolicy", "MajorityPolicy", "AbsoluteMajorityPolicy"))

# Policy-type keywords as they appear verbatim in a .gov snippet (the govdsl grammar's
# `policyType` alternatives). Ordered longest-first so a substring keyword cannot shadow a
# longer one (e.g. AbsoluteMajorityPolicy must win over MajorityPolicy, LazyConsensusPolicy
# over ConsensusPolicy). Used to recover the author's intended type when the FULL ANTLR
# parse fails — the keyword survives even when the body does not (the most common failure
# is the missing //-comment lexer rule, which leaves the type keyword intact).
_POLICY_TYPE_KEYWORDS = (
    "AbsoluteMajorityPolicy",
    "LazyConsensusPolicy",
    "LeaderDrivenPolicy",
    "ConsensusPolicy",
    "MajorityPolicy",
    "VotingPolicy",
)

# Documented default ratio for the voting family — mirrors the MajorityPolicy rule text
# below and the in-container tally()'s own fallback threshold. Non-voting policies carry
# no ratio.
_DEFAULT_RATIO = 0.5

# Per-policy-type decision rule, in plain language for the LLM (the "concepts").
_POLICY_RULES = {
    "VotingPolicy": ("Treat each collaborator's reply as a weighted vote; select the "
                     "answer whose combined weight reaches the ratio threshold."),
    "MajorityPolicy": ("Select the answer supported by more than half of the "
                       "collaborators (ratio defaults to 0.5)."),
    "AbsoluteMajorityPolicy": ("Select the answer supported by more than half of ALL "
                               "eligible collaborators; abstentions count against it."),
    "LeaderDrivenPolicy": ("Defer to the lead collaborator's reply; only fall back to "
                           "the other replies if the leader gives none."),
    "ConsensusPolicy": ("Synthesise a single answer that ALL collaborators could agree "
                        "on; if they fundamentally conflict, say so explicitly."),
    "LazyConsensusPolicy": ("Accept the proposed answer unless a collaborator objects — "
                            "silence means assent."),
}


def _detect_policy_type(text: str):
    """Scan raw .gov text for a policy-type keyword; return the class name or None.

    Word-boundary matched and longest-first (see ``_POLICY_TYPE_KEYWORDS``) so the
    detected type is the author's intended one even when the surrounding policy fails
    to parse.
    """
    if not text:
        return None
    for kw in _POLICY_TYPE_KEYWORDS:
        if re.search(r"\b" + kw + r"\b", text):
            return kw
    return None


def build_default_summary(policy_type, participant_names, raw_text=""):
    """Synthesize a policy summary of ``policy_type`` over ``participant_names``, shaped
    EXACTLY like a parsed-policy summary so it flows through the identical star
    fan-out + deterministic tally path.

    Used when a .gov snippet fails to parse: rather than degrade to a raw-text directive
    (which produces no real vote and silently forces requires_human=False), the caller —
    which knows the BPMN collaboration participants — builds a genuine, type-preserving
    default policy here. ``policy_type`` is the type recovered from the raw text by
    :func:`_detect_policy_type` (None → MajorityPolicy). Participants are the collaboration
    agents, so the policy needs no human and gets the voting family's documented default
    ratio (None for non-voting types).
    """
    ptype = policy_type or "MajorityPolicy"
    ratio = _DEFAULT_RATIO if ptype in _VOTING_POLICY_TYPES else None
    participants = [{"name": n, "kind": "agent", "confidence": None, "roles": []}
                    for n in participant_names]
    summary = {
        "policy_type": ptype,
        "ratio": ratio,
        "decision_type": None,
        "participants": participants,
        "requires_human": False,
    }
    human_summary, instruction = _build_instruction(summary)
    return {
        "instruction": instruction,
        "summary": human_summary,
        "requires_human": False,
        "policy_type": ptype,
        "participants": participants,
        "ratio": ratio,
        "raw": raw_text,
        # Marks this as a generation-time synthesized default (not author-authored), so
        # downstream logs/tests can tell the two apart.
        "synthesized_default": True,
    }


def _strip_comments(text: str) -> str:
    # The govdsl grammar has no LINE_COMMENT rule; WME emits `//` headers.
    # Strip them so the parse succeeds regardless of the upstream grammar fix.
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("//"))


def _parse_policies(text: str):
    """Run the real govdsl parser. Returns a list[Policy] or raises."""
    # These imports require the governanceDSL package on sys.path (Step 4 / §packaging).
    from antlr4 import InputStream, CommonTokenStream, ParseTreeWalker
    from grammar.govdslLexer import govdslLexer
    from grammar.govdslParser import govdslParser
    from grammar.PolicyCreationListener import PolicyCreationListener

    lexer = govdslLexer(InputStream(text))
    parser = govdslParser(CommonTokenStream(lexer))
    tree = parser.governance()
    listener = PolicyCreationListener()
    ParseTreeWalker().walk(listener, tree)
    return listener.get_policies()


def _summarize_policy(policy) -> dict:
    from metamodel.governance import Agent, Human, Role  # local import (same dep as parser)

    ptype = type(policy).__name__
    participants, requires_human = [], False
    for p in (getattr(policy, "participants", None) or []):
        if isinstance(p, Agent):
            kind = "agent"
        elif isinstance(p, Human):
            kind = "human"
        elif isinstance(p, Role):
            kind = "role"
        else:
            kind = "other"
        # A human can enter the policy two ways: as a non-(Agent) Individual (parsed as
        # Human) or as a Role standing in for human actors (the WME generator only ever
        # emits agents as `(Agent)`, so any Role/Human participant is a person). Either
        # way the merge point must defer the decision to a human.
        if kind != "agent":
            requires_human = True
        participants.append({
            "name": p.name,
            "kind": kind,
            "confidence": getattr(p, "confidence", None),
            "roles": sorted(r.name for r in (getattr(p, "roles", None) or [])),
        })
    return {
        "policy_type": ptype,
        "ratio": getattr(policy, "ratio", None),
        "decision_type": type(policy.decision_type).__name__ if getattr(policy, "decision_type", None) else None,
        "participants": participants,
        "requires_human": requires_human,
    }


def _build_instruction(summary: dict):
    """Return (human_summary, llm_instruction).

    `human_summary` is the policy facts shown to a person at the approval step.
    `llm_instruction` adds the merge preamble and the auditable-vote directive on top
    of those facts; it is the agent's system message only, NOT for human display.
    """
    rule = _POLICY_RULES.get(summary["policy_type"],
                             "Apply the stated governance policy to merge the replies.")
    names = ", ".join(p["name"] for p in summary["participants"]) or "(unspecified)"
    facts = [
        f"Policy type: {summary['policy_type']}.",
        f"Decision rule: {rule}",
        f"Participants (collaborators): {names}.",
    ]
    if summary["ratio"] is not None:
        facts.append(f"Ratio threshold: {summary['ratio']}.")
    if summary["decision_type"]:
        facts.append(f"Decision type: {summary['decision_type']}.")
    human_summary = "\n".join(facts)
    if summary["policy_type"] in _VOTING_POLICY_TYPES:
        # Voting policies are normally resolved by the deterministic in-container tally
        #; this directive only steers the LLM on the degraded fallback path
        # (no producer resolved to a service), where an auditable vote narrative is the
        # best available audit trail.
        directive = (
            "First output a 'Votes:' section so the decision is auditable: list each "
            "collaborator using the reply label you were given (e.g. an agent name with "
            "its #replica index) and the position/option its reply supports; add your own "
            "position, and the human's choice if one was provided. Then state the tally "
            "against the policy (and the ratio threshold, if any) and the resulting "
            "outcome. Finally, give the merged final answer.")
    else:
        # Non-voting policies (leader-driven / consensus / lazy-consensus) are NOT decided
        # by a vote. Asking for a 'Votes:'/tally section makes the LLM fabricate one (e.g. a
        # LeaderDriven merge inventing "3 Approve, 0 Disapprove"). Forbid the tally AND the
        # softer "the collaborators approved/agreed" narrative it falls back to — neither
        # event happened; the collaborators only returned candidate replies. Just merge.
        directive = (
            "Apply the decision rule above to the collaborators' replies and produce a "
            "single merged final answer. Output ONLY that answer. Do NOT invent a vote, "
            "tally, approval count, or outcome line, and do NOT claim that the collaborators "
            "approved, agreed on, endorsed, confirmed, or reviewed the answer — no such step "
            "happened. This policy is not decided by voting.")
    llm_instruction = "\n".join(
        ["You are the merge point of an agent swarm. Apply this governance policy to "
         "combine the collaborators' replies into one result."]
        + facts + [directive])
    return human_summary, llm_instruction


def summarize_governance(dsl_text):
    """Public entry. Returns a dict {instruction, summary, requires_human, policy_type,
    raw} or None for empty input. Never raises.

    `instruction` is the agent's system message (full, with the audit directive);
    `summary` is the human-readable policy facts shown at the approval step.
    """
    if not dsl_text or not dsl_text.strip():
        return None
    cleaned = _strip_comments(dsl_text)
    try:
        policies = _parse_policies(cleaned)
        if not policies:
            raise ValueError("no policies parsed")
        if len(policies) > 1:
            # v1 models one merge point per gateway; a .gov declaring several top-level
            # policies has only its first wired. Surface the drop instead of hiding it.
            logger.warning("[governance] %d policies parsed from one gateway's DSL; v1 wires "
                           "only the first (%s)", len(policies), type(policies[0]).__name__)
        summary = _summarize_policy(policies[0])  # one merge point per gateway (v1)
        human_summary, instruction = _build_instruction(summary)
        return {
            "instruction": instruction,
            "summary": human_summary,
            "requires_human": summary["requires_human"],
            "policy_type": summary["policy_type"],
            # the star fan-out + tally need the participant list (names +
            # confidence weights) and the ratio. They were parsed but not surfaced.
            "participants": summary["participants"],
            "ratio": summary["ratio"],
            "raw": dsl_text,
        }
    except Exception as exc:  # bad user edit / parser unavailable
        detected = _detect_policy_type(dsl_text)
        # Signal "unparseable" so the caller — which knows the BPMN collaboration
        # participants — can synthesize a real, type-preserving DEFAULT policy
        # (build_default_summary) that runs through the same vote + tally path. The
        # raw-text instruction/summary below are kept only as a last-resort shape for
        # any caller that ignores the `unparseable` signal.
        logger.warning("[governance] parse failed (%s); will synthesize a default %s policy "
                       "over the collaboration participants", exc, detected or "MajorityPolicy")
        return {
            "instruction": ("You are the merge point of an agent swarm. Apply the following "
                            "governance policy (DSL) to combine the collaborators' replies "
                            "into one result, then give the final answer.\n\n" + dsl_text),
            # Fallback never sets requires_human, so the human-approval step (the only
            # consumer of `summary`) won't fire; provide the key for template safety.
            "summary": "Governance policy (could not be parsed; shown verbatim):\n" + dsl_text,
            "requires_human": False,
            "policy_type": None,
            # no structured participants on the fallback path; a star vote
            # is impossible without them, so the generator keeps the topology peers.
            "participants": [],
            "ratio": None,
            "raw": dsl_text,
            # The caller turns these into a default policy over the producers it traced.
            "unparseable": True,
            "detected_policy_type": detected,
        }
