"""Conversation flow editor — deterministic flow interpreter (roadmap 6.6).

A bot author can script a conversation as a small state machine stored in
``BotConfig.config['flow']`` (no new table, same idiom as the 6.3 A/B config and
the 5.5 QA rules). The interpreter does NOT replace the LLM — it *scaffolds* it:
each node carries a turn-prompt addition (extra guidance injected for that turn,
appended to — never replacing — the hardened system prompt / ``prompt_preamble``)
and branch rules keyed on the NLP-returned ``Intent``. All this module decides is
"which node are we on"; the LLM still writes the actual reply.

Fail-safe by construction: a malformed flow parses to ``None`` and the
orchestrator simply runs unscripted — a bad flow must never break a live call.
``advance`` never raises and always returns a valid node id.

Flow JSON shape (stored under ``BotConfig.config['flow']``)::

    {
      "start": "greeting",
      "nodes": {
        "greeting": {
          "prompt": "Greet warmly and ask if they have a minute.",
          "branches": {"interested": "pitch", "busy": "callback", "*": "greeting"}
        },
        "pitch": {
          "prompt": "Explain the offer in one sentence.",
          "branches": {"promise_to_pay": "close", "objection": "handle_objection"}
        }
      }
    }

Semantics:
  - ``start`` names the node the call opens on; it must exist in ``nodes``.
  - Each node's ``prompt`` is the per-turn guidance addition (may be empty).
  - ``branches`` maps an ``Intent`` value string → the next node id. A ``"*"``
    key is the wildcard fallback used when no explicit intent branch matches.
  - Branch targets that don't resolve to a real node are treated as "stay" at
    runtime (defensive — a typo'd target never crashes or teleports the call).

Design note: these functions take the current node id as a plain argument and
never read or write ``SessionState``, so a ``flow_node_id: str | None`` can be
threaded through the orchestrator later without coupling this module to session
storage.
"""

from __future__ import annotations

from dataclasses import dataclass

from koel.common.logging import get_logger

logger = get_logger(__name__)

# The catch-all branch key: taken when no explicit intent branch matches.
WILDCARD = "*"


@dataclass(frozen=True)
class FlowNode:
    """One scripted step: a prompt addition plus intent-keyed branch rules."""

    prompt: str
    branches: dict[str, str]


@dataclass(frozen=True)
class Flow:
    """A parsed, validated conversation flow. ``start`` is guaranteed to be a key
    of ``nodes`` (``parse_flow`` rejects a flow whose start node is missing)."""

    start: str
    nodes: dict[str, FlowNode]


def _parse_node(node_data: object) -> FlowNode | None:
    """Parse one node entry defensively. Returns None when it isn't a usable
    node (not a mapping); unknown keys are ignored, a non-string prompt becomes
    ``""``, and only string→string branch entries are kept."""
    if not isinstance(node_data, dict):
        return None
    raw_prompt = node_data.get("prompt")
    prompt = raw_prompt if isinstance(raw_prompt, str) else ""
    raw_branches = node_data.get("branches")
    branches: dict[str, str] = {}
    if isinstance(raw_branches, dict):
        branches = {
            k: v for k, v in raw_branches.items() if isinstance(k, str) and isinstance(v, str)
        }
    return FlowNode(prompt=prompt, branches=branches)


def parse_flow(config: dict | None) -> Flow | None:
    """Validate/parse ``BotConfig.config['flow']`` into a :class:`Flow`.

    Returns ``None`` when the flow is absent or malformed so the orchestrator can
    run unscripted — a bad flow must never break a call. A flow is rejected when:
    ``config`` (or its ``flow`` entry) isn't a mapping, ``start`` isn't a
    non-empty string, ``nodes`` isn't a non-empty mapping, or ``start`` doesn't
    resolve to a parsed node.
    """
    if not isinstance(config, dict):
        return None
    flow_raw = config.get("flow")
    if not isinstance(flow_raw, dict):
        return None

    start = flow_raw.get("start")
    if not isinstance(start, str) or not start.strip():
        logger.warning("flow_invalid reason=missing_or_bad_start")
        return None

    nodes_raw = flow_raw.get("nodes")
    if not isinstance(nodes_raw, dict) or not nodes_raw:
        logger.warning("flow_invalid reason=missing_or_bad_nodes")
        return None

    nodes: dict[str, FlowNode] = {}
    for node_id, node_data in nodes_raw.items():
        if not isinstance(node_id, str):
            continue
        node = _parse_node(node_data)
        if node is not None:
            nodes[node_id] = node

    if start not in nodes:
        logger.warning("flow_invalid reason=start_not_in_nodes start=%s", start)
        return None

    return Flow(start=start, nodes=nodes)


def current_prompt(flow: Flow, node_id: str | None) -> str | None:
    """The per-turn prompt addition for ``node_id``, or ``None`` when the node id
    is unknown (the orchestrator then injects no extra guidance this turn). The
    prompt itself may be an empty string — that is a valid "no addition" node."""
    if node_id is None or node_id not in flow.nodes:
        return None
    return flow.nodes[node_id].prompt


def advance(flow: Flow, node_id: str | None, intent: str) -> str:
    """Return the next node id given the NLP ``intent`` (an ``Intent.value``).

    Resolution order: an explicit ``intent`` branch, else the ``"*"`` wildcard
    branch, else stay on the current node. Never raises and always returns a
    valid node id — a branch target that doesn't resolve to a real node is
    treated as "stay", and an unknown/absent current node falls back to
    ``flow.start``.
    """
    if node_id is None or node_id not in flow.nodes:
        logger.warning("flow_advance_unknown_node node=%s falling_back_to_start", node_id)
        return flow.start

    branches = flow.nodes[node_id].branches
    target = branches.get(intent, branches.get(WILDCARD))
    if target is None:
        # No matching branch and no wildcard — hold position.
        return node_id
    if target not in flow.nodes:
        logger.warning(
            "flow_advance_bad_target node=%s intent=%s target=%s staying",
            node_id,
            intent,
            target,
        )
        return node_id
    return target
