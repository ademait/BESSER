"""item 35 — parse a WME-authored governance .gov snippet at generation time and
produce a runtime instruction for the synthesizing agent (guide 10 §2, Option B+).

Parsing runs HERE (BESSER backend), not in the agent container: the container only
receives the resulting instruction string. The govdsl metamodel is BESSER-BUML based,
so the parser imports cleanly wherever besser.BUML is importable. Any failure (bad
user edit, missing parser package) degrades to a raw-text instruction (Option B).
"""
import logging

logger = logging.getLogger(__name__)

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


def _strip_comments(text: str) -> str:
    # The govdsl grammar has no LINE_COMMENT rule (guide 10 §4); WME emits `//` headers.
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
        # way the merge point must defer the decision to a human (guide 10 §2, point 5).
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


def _build_instruction(summary: dict) -> str:
    rule = _POLICY_RULES.get(summary["policy_type"],
                             "Apply the stated governance policy to merge the replies.")
    names = ", ".join(p["name"] for p in summary["participants"]) or "(unspecified)"
    lines = [
        "You are the merge point of an agent swarm. Apply this governance policy to "
        "combine the collaborators' replies into one result.",
        f"Policy type: {summary['policy_type']}.",
        f"Decision rule: {rule}",
        f"Participants (collaborators): {names}.",
    ]
    if summary["ratio"] is not None:
        lines.append(f"Ratio threshold: {summary['ratio']}.")
    if summary["decision_type"]:
        lines.append(f"Decision type: {summary['decision_type']}.")
    lines.append("State which reply(ies) you selected and why in one sentence, then give "
                 "the final answer.")
    return "\n".join(lines)


def summarize_governance(dsl_text):
    """Public entry. Returns a dict {instruction, requires_human, policy_type, raw}
    or None for empty input. Never raises."""
    if not dsl_text or not dsl_text.strip():
        return None
    cleaned = _strip_comments(dsl_text)
    try:
        policies = _parse_policies(cleaned)
        if not policies:
            raise ValueError("no policies parsed")
        summary = _summarize_policy(policies[0])  # one merge point per gateway (v1)
        return {
            "instruction": _build_instruction(summary),
            "requires_human": summary["requires_human"],
            "policy_type": summary["policy_type"],
            "raw": dsl_text,
        }
    except Exception as exc:  # bad user edit / parser unavailable -> Option B fallback
        logger.warning("[governance] parse failed (%s); falling back to raw-text prompt", exc)
        return {
            "instruction": ("You are the merge point of an agent swarm. Apply the following "
                            "governance policy (DSL) to combine the collaborators' replies "
                            "into one result, then give the final answer.\n\n" + dsl_text),
            "requires_human": False,
            "policy_type": None,
            "raw": dsl_text,
        }
