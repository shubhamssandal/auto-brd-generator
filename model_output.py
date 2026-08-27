"""
Reading a language model's answer as data.

A model's response is untrusted input: it may be fenced, prefixed with a sentence,
half-typed, or shaped nothing like what was asked for. Every module in this app that
asks a model for JSON needs the same small set of coercions to get from that response
to something deterministic code can validate, so they live here once instead of being
re-implemented per generator.

Nothing here validates *meaning*. These functions only make a response readable; the
caller decides whether what it says is acceptable.
"""

import json
import re
from typing import Optional


class ModelResponseError(Exception):
    """
    A model's answer could not be read.

    Carries a message that is safe to show a user: what was wrong with the shape of
    the response, never the response itself. A raw payload can run to thousands of
    characters and is not something to paste into a UI note.
    """


def line(value) -> str:
    """One line of text: whitespace collapsed, ``None`` becoming ``""``."""
    return " ".join(str(value if value is not None else "").split())


def block(value) -> str:
    """Multi-line text with its own line breaks kept and trailing space removed."""
    text = str(value if value is not None else "").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(part.rstrip() for part in text.split("\n")).strip()


def excerpt(value, limit: int = 300) -> str:
    """One line, truncated with an ellipsis rather than cut mid-word-ish."""
    text = line(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def strings(value) -> list:
    """
    A list of non-empty single-line strings from whatever the model sent.

    Accepts a list, a bare string, or a list of small objects such as
    ``[{"id": "FR-1"}]``, because all three turn up in practice. Anything else
    contributes nothing rather than raising: a malformed member of one field is not a
    reason to discard an otherwise usable item.
    """
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        single = line(value)
        return [single] if single else []
    if isinstance(value, dict):
        value = list(value.values())
    if not isinstance(value, (list, tuple, set)):
        single = line(value)
        return [single] if single else []

    out: list = []
    for item in value:
        if isinstance(item, dict):
            cleaned = ""
            for key in ("id", "requirement_id", "action_item_id", "key", "value", "item"):
                if item.get(key):
                    cleaned = line(item.get(key))
                    break
        else:
            cleaned = line(item)
        if cleaned:
            out.append(cleaned)
    return out


def first(payload: dict, *names):
    """The first of ``names`` present in ``payload`` with a non-empty value."""
    for name in names:
        if name in payload:
            value = payload.get(name)
            if value not in (None, "", [], (), {}):
                return value
    return None


def normalise_id(value: str) -> str:
    """An id in comparison form: trimmed of stray punctuation, case-folded."""
    return line(value).strip(" .,;:()[]").lower()


def first_json_span(text: str) -> Optional[str]:
    """
    The first balanced ``{...}`` or ``[...]`` in ``text``, or ``None``.

    String literals are tracked so a brace inside a description cannot end the span
    early. This exists because a model told to return JSON only will still occasionally
    prefix a sentence, and re-prompting to fix that would be a second model call.
    """
    start = None
    for index, character in enumerate(text):
        if character in "{[":
            start = index
            break
    if start is None:
        return None

    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        character = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == opener:
            depth += 1
        elif character == closer:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def json_payload(raw, subject: str = "AI"):
    """
    The model's response as Python data.

    Raises ``ModelResponseError`` rather than returning something half-read, so the
    caller's one decision -- use this answer or fall back -- stays a single branch.
    ``subject`` names the generator in the error message.
    """
    if isinstance(raw, (dict, list)):
        return raw

    text = str(raw or "").strip()
    if not text:
        raise ModelResponseError("The {} returned an empty response.".format(subject))

    unfenced = re.sub(r"^```[A-Za-z]*\s*", "", text)
    unfenced = re.sub(r"\s*```\s*$", "", unfenced).strip()

    for candidate in (unfenced, text):
        try:
            return json.loads(candidate)
        except (TypeError, ValueError):
            pass

    span = first_json_span(unfenced)
    if span is not None:
        try:
            return json.loads(span)
        except ValueError:
            pass

    raise ModelResponseError(
        "The {}'s response could not be read as JSON.".format(subject)
    )
