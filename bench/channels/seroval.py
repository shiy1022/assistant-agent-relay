"""Reads and writes seroval's JSON form, which wajo.ai's server functions speak.

wajo.ai is a TanStack Start app: the page calls "server functions" at
/_serverFn/<id>, sending its arguments and getting its answers as seroval
nodes ({"t": type, ...}). Only the node types those answers use are handled:
numbers, strings, constants, dates, arrays, objects, references, sets and
maps. Anything else comes back as {"$seroval": type}.

Strings are escaped twice in this form (seroval escapes them, then JSON
does), so `decode` undoes seroval's escapes.
"""

import json
import re

NUMBER, STRING, CONSTANT, BIGINT, REF, DATE, REGEXP, SET, MAP, ARRAY, OBJECT, NULL_OBJECT = range(12)
CONSTANTS = {0: None, 1: None, 2: True, 3: False, 4: -0.0, 5: float("inf"), 6: float("-inf"), 7: float("nan")}
ESCAPES = {'\\\\': '\\', '\\"': '"', '\\n': '\n', '\\r': '\r', '\\b': '\b', '\\t': '\t', '\\f': '\f',
           '\\u2028': '\u2028', '\\u2029': '\u2029', '\\x3C': '<'}
ESCAPED = re.compile(r'\\\\|\\"|\\n|\\r|\\b|\\t|\\f|\\u2028|\\u2029|\\x3C')


def unescape(s: str) -> str:
    return ESCAPED.sub(lambda m: ESCAPES[m.group()], s)


def escape(s: str) -> str:
    out = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
    out = out.replace("\b", "\\b").replace("\t", "\\t").replace("\f", "\\f").replace("<", "\\x3C")
    return out.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def decode(node, refs: dict | None = None):
    """A seroval node as plain Python: dicts, lists, strings, numbers, booleans and None."""
    refs = {} if refs is None else refs
    if not isinstance(node, dict) or "t" not in node:
        return node
    t = node["t"]
    if t == NUMBER:
        value = node.get("s")
        return float(value) if isinstance(value, str) else value
    if t == STRING:
        return unescape(node.get("s") or "")
    if t == CONSTANT:
        return CONSTANTS.get(node.get("s"))
    if t == BIGINT:
        return int(node.get("s"))
    if t == REF:
        return refs.get(node.get("i"))
    if t == DATE:
        return node.get("s")
    if t == ARRAY:
        out: list = []
        refs[node.get("i")] = out
        out.extend(decode(x, refs) for x in node.get("a") or [])
        return out
    if t in (OBJECT, NULL_OBJECT):
        obj: dict = {}
        refs[node.get("i")] = obj
        p = node.get("p") or {}
        for k, v in zip(p.get("k") or [], p.get("v") or []):
            obj[unescape(k) if isinstance(k, str) else k] = decode(v, refs)
        return obj
    if t == SET:
        out = []
        refs[node.get("i")] = out
        out.extend(decode(x, refs) for x in (node.get("a") or []))
        return out
    if t == MAP:
        obj = {}
        refs[node.get("i")] = obj
        e = node.get("e") or {}
        for k, v in zip(e.get("k") or [], e.get("v") or []):
            obj[str(decode(k, refs))] = decode(v, refs)
        return obj
    return {"$seroval": t}


def encode(value) -> dict:
    """Plain Python as a seroval node, the way the page sends arguments. A dict under the
    key "context" is sent as a null-prototype object, as wajo.ai's own page sends it."""
    counter = iter(range(1_000_000))

    def node(v, null_proto=False):
        if v is None:
            return {"t": CONSTANT, "s": 1}  # undefined: the page leaves unset arguments out this way
        if v is True:
            return {"t": CONSTANT, "s": 2}
        if v is False:
            return {"t": CONSTANT, "s": 3}
        if isinstance(v, (int, float)):
            return {"t": NUMBER, "s": v}
        if isinstance(v, str):
            return {"t": STRING, "s": escape(v)}
        if isinstance(v, (list, tuple)):
            i = next(counter)
            return {"t": ARRAY, "i": i, "a": [node(x) for x in v], "o": 0}
        if isinstance(v, dict):
            i = next(counter)
            keys = list(v)
            return {"t": NULL_OBJECT if null_proto else OBJECT, "i": i,
                    "p": {"k": keys, "v": [node(v[k], null_proto=(k == "context")) for k in keys]}, "o": 0}
        raise TypeError(f"can't send {type(v).__name__} to wajo.ai")

    return node(value)


def payload(data: dict | None, context: dict) -> str:
    """A server function's arguments as the page sends them: {"t": …, "f": 127, "m": []}."""
    args = ({"data": data} if data is not None else {}) | {"context": context}
    return json.dumps({"t": encode(args), "f": 127, "m": []}, ensure_ascii=False, separators=(",", ":"))
