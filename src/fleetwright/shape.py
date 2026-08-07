"""Check a result against the shape a kind said it would return.

`returns` was always written like this, because it is what a person naturally
writes when describing an answer:

    {"claims": <int>, "notes": "<string>"}

That was prose. Nothing read it, so a worker could hand back a bare string
against a declared object and be told nothing. This module makes the same text
mean something, without asking anyone to write JSON Schema instead.

**Not JSON Schema, deliberately.** Schema is the right answer for an API
contract negotiated between teams. Here the audience is an agent reading a
brief, and `{"claims": <int>}` is legible to it in a way that
`{"type":"object","properties":{"claims":{"type":"integer"}}}` is not. The
brief is the documentation; making the documentation also be the check is the
whole point.

**Deliberately permissive in one direction.** Extra keys are allowed. A worker
that returns more than it promised has not broken anything, and refusing that
would punish the useful habit of including context. Missing keys and wrong
types are refused, because those are what break the caller.

**Unparseable `returns` disables checking entirely.** Plenty of kinds describe
their result in a sentence, and a sentence is a legitimate thing to write. It
must not become an error.
"""

from __future__ import annotations

import json
import re

#: `<int>` and friends. The angle brackets are what makes a placeholder a
#: placeholder rather than a literal value, which is the convention the docs
#: and every example already used before this file existed.
_TYPES: dict[str, tuple[type, ...]] = {
    "int": (int,),
    "integer": (int,),
    "float": (float, int),
    "number": (float, int),
    "str": (str,),
    "string": (str,),
    "bool": (bool,),
    "boolean": (bool,),
    "list": (list,),
    "array": (list,),
    "object": (dict,),
    "dict": (dict,),
    "any": (object,),
}

_PLACEHOLDER = re.compile(r"^<\s*([a-zA-Z]+)\s*>$")


def parse(returns: str | None) -> object | None:
    """The declared shape, or `None` if this is prose rather than a shape.

    Returning `None` for anything unrecognised is the important behaviour: a
    kind whose `returns` is "a sentence saying what you found" is perfectly
    legitimate and must not start failing because this module exists.
    """
    if not returns or not returns.strip():
        return None
    text = returns.strip()
    if text[0] not in "{[":
        return None
    # Quote the BARE placeholders so the whole thing becomes JSON, then
    # recognise them again on the way out. The lookarounds matter: people write
    # both `<int>` and `"<string>"`, and quoting one that is already quoted
    # produces `""<string>""`, which fails to parse — so every shape silently
    # became "no shape" and nothing was ever checked.
    quoted = re.sub(r'(?<!")<\s*([a-zA-Z]+)\s*>(?!")', r'"<\1>"', text)
    try:
        return json.loads(quoted)
    except json.JSONDecodeError:
        return None


def check(template: object, value: object, path: str = "result") -> list[str]:
    """Everything wrong with `value`, all at once.

    All at once because a worker that fixes one problem and is then told about
    the next has to redo the work twice, and it is an agent: it will.
    """
    problems: list[str] = []

    if isinstance(template, str):
        m = _PLACEHOLDER.match(template.strip())
        if not m:
            # A literal string in the template means "a string goes here".
            if not isinstance(value, str):
                problems.append(f"{path}: expected a string, got {_name(value)}")
            return problems
        want = m.group(1).lower()
        types = _TYPES.get(want)
        if types is None:
            return problems                      # unknown placeholder: ignore
        if want == "any":
            return problems
        # bool is a subclass of int in Python and almost never what <int> meant.
        if types == (int,) and isinstance(value, bool):
            problems.append(f"{path}: expected an int, got a bool")
        elif not isinstance(value, types):
            problems.append(f"{path}: expected {want}, got {_name(value)}")
        return problems

    if isinstance(template, dict):
        if not isinstance(value, dict):
            return [f"{path}: expected an object, got {_name(value)}"]
        for key, sub in template.items():
            # `"notes?"` marks a key the worker may omit.
            optional = key.endswith("?")
            real = key[:-1] if optional else key
            if real not in value:
                if not optional:
                    problems.append(f"{path}: missing key {real!r}")
                continue
            problems += check(sub, value[real], f"{path}.{real}")
        return problems                          # extra keys are fine

    if isinstance(template, list):
        if not isinstance(value, list):
            return [f"{path}: expected an array, got {_name(value)}"]
        if not template:
            return problems
        for i, item in enumerate(value):
            problems += check(template[0], item, f"{path}[{i}]")
        return problems

    return problems


def describe(returns: str | None, value: object) -> list[str]:
    """Convenience: parse and check, or say nothing if there is no shape."""
    t = parse(returns)
    return check(t, value) if t is not None else []


def _name(value: object) -> str:
    return {dict: "an object", list: "an array", str: "a string",
            bool: "a bool", int: "an int", float: "a float",
            type(None): "null"}.get(type(value), type(value).__name__)
