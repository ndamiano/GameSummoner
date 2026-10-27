"""services — the LLM tool-call parsers the codegen fix shapes share.

The old Services gateway + AgentLoop that owned the synchronous fix loop are gone (a build is now a
chain of llm jobs driven by build_chain). What remains here is the loose-output normalization every
local-model turn depends on: `parse_args` (any argument shape → dict). Recovering a call the model
wrote as TEXT lives in maestro/tool_calls.py — see test_tool_calls.py.
"""


from maestro.services import parse_args, parse_args_checked

_ADD_SCHEMA = {"type": "function", "function": {
    "name": "add_item",
    "parameters": {"type": "object",
                   "properties": {"item_id": {}, "name": {}},
                   "required": ["item_id"]}}}
_OTHER_SCHEMA = {"type": "function", "function": {
    "name": "write_node",
    "parameters": {"type": "object",
                   "properties": {"node_id": {}, "content": {}},
                   "required": ["node_id", "content"]}}}


def test_parse_args_handles_dict_json_and_fenced():
    assert parse_args({"a": 1}) == {"a": 1}
    assert parse_args('{"a": 1}') == {"a": 1}
    assert parse_args('```json\n{"a": 1}\n```') == {"a": 1}
    # a dict whose single key IS the json blob (some templates) unwraps
    assert parse_args({'{"a": 1}': ""}) == {"a": 1}


def test_parse_args_unparseable_returns_empty():
    assert parse_args("not json at all") == {}
    assert parse_args("[1, 2, 3]") == {}   # a non-dict json value is not args


def test_no_arguments_and_unreadable_arguments_are_told_apart():
    """Both answer {}, and only one is a failure: a call cut off at the output cap has arguments
    that cannot be read, while list_files legitimately carries none."""
    assert parse_args_checked("{}") == ({}, True)
    assert parse_args_checked("") == ({}, True)
    assert parse_args_checked(None) == ({}, True)
    assert parse_args_checked({"a": 1}) == ({"a": 1}, True)
    assert parse_args_checked('```json\n{"a": 1}\n```') == ({"a": 1}, True)
    assert parse_args_checked('{"path":"story.js","content":"const S = {\\n  ') == ({}, False)
    assert parse_args_checked("[1, 2, 3]") == ({}, False)


def test_a_complete_object_with_trailing_text_is_the_call_the_model_meant():
    """DeepSeek Flash, 2026-09-10: arguments came back as a valid object followed by prose, and
    `json.loads` refusing the whole thing took a world build down with it."""
    args, ok = parse_args_checked('{"path": "a.js"} and then some words')
    assert ok and args == {"path": "a.js"}


def test_a_fragment_that_never_closes_is_still_unreadable():
    args, ok = parse_args_checked('{"path": "a.js"')
    assert not ok and args == {}
