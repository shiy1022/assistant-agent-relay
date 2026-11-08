import json

from bench.channels import seroval

# What wajo.ai's page sent on 2026-11-07 to read one chat's messages, as captured from Chrome.
CAPTURED = ('{"t":{"t":10,"i":0,"p":{"k":["data","context"],"v":[{"t":10,"i":1,"p":{"k":["sessionId","orgId","pageSize"],'
            '"v":[{"t":1,"s":"7d9830d2-120c-4965-8e2b-b978d39324b7"},{"t":1,"s":"666d727d-1c98-4103-9220-700a02c37d91"},'
            '{"t":0,"s":20}]},"o":0},{"t":11,"i":2,"p":{"k":["bootedSub"],"v":[{"t":1,"s":"5799144f-d3be-4ba5-8c20-d1e83a84ff62"}]},'
            '"o":0}]},"o":0},"f":127,"m":[]}')


def test_payload_is_what_the_page_sends():
    args = seroval.decode(json.loads(CAPTURED)["t"])
    assert args == {"data": {"sessionId": "7d9830d2-120c-4965-8e2b-b978d39324b7",
                             "orgId": "666d727d-1c98-4103-9220-700a02c37d91", "pageSize": 20},
                    "context": {"bootedSub": "5799144f-d3be-4ba5-8c20-d1e83a84ff62"}}
    assert json.loads(seroval.payload(args["data"], args["context"])) == json.loads(CAPTURED)


def test_unset_arguments_go_as_undefined_and_lists_as_arrays():
    sent = json.loads(seroval.payload({"workspaceId": "w", "agentIds": [], "pageToken": None, "excludePinned": True},
                                      {"bootedSub": "s"}))
    data = sent["t"]["p"]["v"][0]["p"]
    assert data["k"] == ["workspaceId", "agentIds", "pageToken", "excludePinned"]
    assert data["v"][1] == {"t": 9, "i": 2, "a": [], "o": 0}
    assert data["v"][2] == {"t": 2, "s": 1} and data["v"][3] == {"t": 2, "s": 2}
    assert sent["t"]["p"]["v"][1]["t"] == 11  # the context goes as a null-prototype object


def test_strings_are_unescaped_twice_escaped():
    # seroval escapes newlines, quotes and "<" itself, and JSON escapes them again
    raw = r'{"t":10,"i":0,"p":{"k":["content"],"v":[{"t":1,"s":"line one\\nsaid \\\"hi\\\" \\x3Cb>"}]},"o":0}'
    assert seroval.decode(json.loads(raw)) == {"content": 'line one\nsaid "hi" <b>'}
    assert seroval.decode(seroval.encode('a\n"b" <c> \\d')) == 'a\n"b" <c> \\d'


def test_constants_references_and_unknown_nodes():
    node = {"t": 10, "i": 0, "p": {"k": ["a", "b", "c", "d", "e"], "v": [
        {"t": 2, "s": 0}, {"t": 2, "s": 3}, {"t": 9, "i": 1, "a": [{"t": 0, "s": 1.5}], "o": 0},
        {"t": 4, "i": 1}, {"t": 25, "i": 2, "s": {}}]}, "o": 0}
    assert seroval.decode(node) == {"a": None, "b": False, "c": [1.5], "d": [1.5], "e": {"$seroval": 25}}
