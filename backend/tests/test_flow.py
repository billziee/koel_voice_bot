"""Deterministic conversation flow interpreter (roadmap 6.6)."""

from __future__ import annotations

from koel.nlp.schemas import Intent
from koel.voicebot.pipeline.flow import (
    Flow,
    FlowNode,
    advance,
    current_prompt,
    parse_flow,
)

# A representative, well-formed flow (matches the module docstring shape).
_FLOW_CONFIG = {
    "flow": {
        "start": "greeting",
        "nodes": {
            "greeting": {
                "prompt": "Greet warmly and ask if they have a minute.",
                "branches": {
                    Intent.interested.value: "pitch",
                    Intent.busy.value: "callback",
                    "*": "greeting",
                },
            },
            "pitch": {
                "prompt": "Explain the offer in one sentence.",
                "branches": {
                    Intent.promise_to_pay.value: "close",
                    Intent.objection.value: "handle_objection",
                },
            },
            "callback": {"prompt": "Offer to call back at a better time."},
            "handle_objection": {"prompt": "Acknowledge the concern, then reassure."},
            "close": {"prompt": "Confirm the commitment and thank them."},
        },
    }
}


def _parsed_flow() -> Flow:
    flow = parse_flow(_FLOW_CONFIG)
    assert flow is not None
    return flow


def test_parse_flow_reads_start_and_nodes_from_config():
    # Arrange / Act
    flow = parse_flow(_FLOW_CONFIG)

    # Assert
    assert flow is not None
    assert flow.start == "greeting"
    assert set(flow.nodes) == {"greeting", "pitch", "callback", "handle_objection", "close"}
    assert flow.nodes["pitch"].prompt == "Explain the offer in one sentence."
    assert flow.nodes["pitch"].branches[Intent.objection.value] == "handle_objection"


def test_parse_flow_defaults_missing_prompt_and_branches():
    # Arrange
    config = {"flow": {"start": "a", "nodes": {"a": {}}}}

    # Act
    flow = parse_flow(config)

    # Assert
    assert flow is not None
    assert flow.nodes["a"] == FlowNode(prompt="", branches={})


def test_parse_flow_ignores_unknown_keys_and_non_string_branches():
    # Arrange
    config = {
        "flow": {
            "start": "a",
            "extra": "ignored",
            "nodes": {
                "a": {
                    "prompt": "hi",
                    "branches": {"interested": "a", 5: "a", "busy": 9},
                    "colour": "blue",
                }
            },
        }
    }

    # Act
    flow = parse_flow(config)

    # Assert
    assert flow is not None
    assert flow.nodes["a"].branches == {"interested": "a"}  # non-str key/value dropped


def test_parse_flow_returns_none_for_absent_or_empty_config():
    # Arrange / Act / Assert
    assert parse_flow(None) is None
    assert parse_flow({}) is None
    assert parse_flow({"flow": None}) is None


def test_parse_flow_returns_none_when_start_or_nodes_missing():
    # Arrange / Act / Assert
    assert parse_flow({"flow": {"nodes": {"a": {}}}}) is None  # no start
    assert parse_flow({"flow": {"start": "a"}}) is None  # no nodes
    assert parse_flow({"flow": {"start": "a", "nodes": {}}}) is None  # empty nodes


def test_parse_flow_returns_none_when_start_node_does_not_exist():
    # Arrange
    config = {"flow": {"start": "missing", "nodes": {"a": {"prompt": "hi"}}}}

    # Act / Assert
    assert parse_flow(config) is None


def test_parse_flow_returns_none_for_malformed_flow_types():
    # Arrange / Act / Assert
    assert parse_flow({"flow": "not-a-dict"}) is None
    assert parse_flow({"flow": {"start": 123, "nodes": {"a": {}}}}) is None
    assert parse_flow({"flow": {"start": "a", "nodes": "not-a-dict"}}) is None


def test_current_prompt_returns_the_node_prompt():
    # Arrange
    flow = _parsed_flow()

    # Act
    prompt = current_prompt(flow, "pitch")

    # Assert
    assert prompt == "Explain the offer in one sentence."


def test_current_prompt_returns_none_for_invalid_node():
    # Arrange
    flow = _parsed_flow()

    # Act / Assert
    assert current_prompt(flow, "nope") is None
    assert current_prompt(flow, None) is None


def test_advance_follows_explicit_intent_branch():
    # Arrange
    flow = _parsed_flow()

    # Act
    nxt = advance(flow, "greeting", Intent.interested.value)

    # Assert
    assert nxt == "pitch"


def test_advance_uses_wildcard_when_no_intent_branch_matches():
    # Arrange
    flow = _parsed_flow()

    # Act — greeting has no branch for smalltalk, but has "*" -> greeting
    nxt = advance(flow, "greeting", Intent.smalltalk.value)

    # Assert
    assert nxt == "greeting"


def test_advance_stays_when_no_branch_and_no_wildcard():
    # Arrange
    flow = _parsed_flow()

    # Act — pitch has neither a not_interested branch nor a wildcard
    nxt = advance(flow, "pitch", Intent.not_interested.value)

    # Assert
    assert nxt == "pitch"


def test_advance_stays_when_branch_target_does_not_exist():
    # Arrange — "objection" points at a node that isn't defined
    config = {
        "flow": {
            "start": "pitch",
            "nodes": {"pitch": {"prompt": "p", "branches": {"objection": "ghost"}}},
        }
    }
    flow = parse_flow(config)
    assert flow is not None

    # Act
    nxt = advance(flow, "pitch", Intent.objection.value)

    # Assert — a dangling target is treated as "stay"
    assert nxt == "pitch"


def test_advance_falls_back_to_start_for_unknown_current_node():
    # Arrange
    flow = _parsed_flow()

    # Act / Assert
    assert advance(flow, "does_not_exist", Intent.interested.value) == "greeting"
    assert advance(flow, None, Intent.interested.value) == "greeting"
