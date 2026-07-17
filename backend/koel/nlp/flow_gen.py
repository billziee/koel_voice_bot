"""Auto-generate a conversation flow from RAG knowledge-base excerpts (6.6 auto
node-builder).

Given a bot's KB chunks + use case, ask the LLM to draft a scripted flow that
conforms to the interpreter contract in ``koel.voicebot.pipeline.flow`` — a
``start`` node id + ``nodes[id] = {prompt, branches: {intent: next_id}}``. The
draft is validated/cleaned here (never trusted raw) and handed to the FlowEditor,
where the operator refines it. Fully degrade-safe: any LLM/parse failure returns
``None`` so the caller surfaces a clean "couldn't generate" rather than a 500.
"""

from __future__ import annotations

import json

from koel.common.http import make_async_client
from koel.common.logging import get_logger

logger = get_logger("koel.nlp.flow_gen")

# Branch keys the interpreter understands (mirrors nlp.schemas.Intent). Given to
# the model so branches key off real intents, plus "*" for the catch-all.
_BRANCH_INTENTS = (
    "interested", "high_interest", "objection", "not_interested", "busy",
    "promise_to_pay", "dispute", "request_human", "opt_out", "smalltalk",
)
_MAX_NODES = 8
_MIN_NODES = 2


def _system_prompt() -> str:
    return (
        "You design short phone-call conversation flows for an Indian bank's "
        "outbound voice bot. You output ONLY a single JSON object, no prose.\n\n"
        "Schema — a state machine:\n"
        '{\n  "start": "<node id>",\n  "nodes": {\n'
        '    "<node id>": {"prompt": "<one-sentence spoken-turn guidance>", '
        '"branches": {"<intent>": "<next node id>"}}\n  }\n}\n\n'
        f"Rules: 2-{_MAX_NODES} nodes. `start` MUST be a key of `nodes`. Node ids "
        "are short snake_case. Each `prompt` is ONE sentence of guidance for that "
        "turn (what the bot should say/do), grounded in the knowledge base — never "
        "invent rates/fees. Branch keys MUST be from this set: "
        f"{', '.join(_BRANCH_INTENTS)}, or \"*\" (catch-all). Branch targets MUST "
        "be node ids that exist. A natural arc: greeting -> pitch/inform -> handle "
        "objection -> escalate to human on high_interest/request_human -> close on "
        "not_interested/opt_out."
    )


def _user_prompt(chunks: list[dict], use_case: str, language: str) -> str:
    kb = "\n".join(
        f"- {c.get('title', '')}: {str(c.get('content', ''))[:400]}" for c in chunks[:20]
    ) or "(no knowledge base content available)"
    return (
        f"Use case: {use_case}\nLanguage: {language}\n\n"
        f"KNOWLEDGE BASE EXCERPTS:\n{kb}\n\n"
        "Produce the conversation flow JSON now."
    )


def _clean_flow(data: object) -> dict | None:
    """Coerce the model's output into a valid flow dict, or None if unsalvageable.
    Drops malformed nodes, non-intent/dangling branches, and repairs `start`."""
    if not isinstance(data, dict):
        return None
    raw_nodes = data.get("nodes")
    if not isinstance(raw_nodes, dict) or not raw_nodes:
        return None
    allowed = set(_BRANCH_INTENTS) | {"*"}
    nodes: dict[str, dict] = {}
    for node_id, nd in list(raw_nodes.items())[:_MAX_NODES]:
        if not isinstance(node_id, str) or not node_id or not isinstance(nd, dict):
            continue
        prompt = nd.get("prompt")
        branches_in = nd.get("branches")
        branches = {}
        if isinstance(branches_in, dict):
            for k, v in branches_in.items():
                if isinstance(k, str) and k in allowed and isinstance(v, str) and v:
                    branches[k] = v
        nodes[node_id] = {"prompt": str(prompt) if isinstance(prompt, str) else "", "branches": branches}
    if len(nodes) < _MIN_NODES:
        return None
    # Drop branch targets that don't resolve to a real node.
    valid = set(nodes)
    for nd in nodes.values():
        nd["branches"] = {k: v for k, v in nd["branches"].items() if v in valid}
    start = data.get("start")
    if not isinstance(start, str) or start not in nodes:
        start = next(iter(nodes))  # repair: first node
    return {"start": start, "nodes": nodes}


async def generate_flow(
    *, chunks: list[dict], use_case: str, language: str, settings
) -> dict | None:  # noqa: ANN001 — NlpSettings (avoid import cycle)
    """Draft a flow from KB chunks via the Groq chat API (JSON mode). Returns a
    validated flow dict or None (LLM disabled / call failed / unparseable)."""
    if not settings.groq_api_key:
        return None  # no LLM configured → caller surfaces "generation unavailable"
    client = make_async_client(timeout_s=settings.groq_timeout_s + 8.0)
    try:
        resp = await client.post(
            f"{settings.groq_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.groq_api_key}", "Content-Type": "application/json"},
            json={
                "model": settings.groq_model,
                "temperature": 0.4,
                "max_tokens": 1200,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": _system_prompt()},
                    {"role": "user", "content": _user_prompt(chunks, use_case, language)},
                ],
            },
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return _clean_flow(json.loads(content))
    except Exception:  # noqa: BLE001 — degrade to None; caller surfaces a clean error
        logger.warning("flow_generation_failed use_case=%s", use_case, exc_info=True)
        return None
    finally:
        await client.aclose()
