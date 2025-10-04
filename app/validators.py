# app/validators.py
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse


# -----------------------------
# Helpers
# -----------------------------

_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?$")


def _is_non_empty_str(val: Any) -> bool:
    return isinstance(val, str) and val.strip() != ""


def _is_list_of_str(val: Any) -> bool:
    return isinstance(val, list) and all(isinstance(x, str) for x in val)


def _as_mapping(val: Any) -> Mapping[str, Any] | None:
    return val if isinstance(val, Mapping) else None


def _as_sequence(val: Any) -> Sequence[Any] | None:
    return val if isinstance(val, Sequence) and not isinstance(val, (str, bytes)) else None


# -----------------------------
# Agent Card Validation
# -----------------------------

_REQUIRED_AGENT_CARD_FIELDS = frozenset(
    [
        "name",
        "description",
        "url",
        "version",
        "capabilities",
        "defaultInputModes",
        "defaultOutputModes",
        "skills",
    ]
)


def validate_agent_card(card_data: dict[str, Any]) -> list[str]:
    """
    Validate the structure and fields of an agent card.

    Contract (non-exhaustive, pragmatic checks):
      - Required top-level fields must exist.
      - url must be absolute (http/https) with a host.
      - version should be semver-like (e.g., 1.2.3 or 1.2.3-alpha).
      - capabilities must be an object/dict.
      - defaultInputModes/defaultOutputModes must be non-empty arrays of strings.
      - skills must be a non-empty array (objects or strings permitted); if objects, "name" should be string.

    Returns:
        A list of human-readable error strings. Empty list means "looks valid".
    """
    errors: list[str] = []
    data = card_data or {}

    # Presence of required fields
    for field in _REQUIRED_AGENT_CARD_FIELDS:
        if field not in data:
            errors.append(f"Required field is missing: '{field}'.")

    # Type/format checks (guard with `in` to avoid KeyErrors)
    # name
    if "name" in data and not _is_non_empty_str(data["name"]):
        errors.append("Field 'name' must be a non-empty string.")

    # description
    if "description" in data and not _is_non_empty_str(data["description"]):
        errors.append("Field 'description' must be a non-empty string.")

    # url
    if "url" in data:
        url_val = data["url"]
        if not _is_non_empty_str(url_val):
            errors.append("Field 'url' must be a non-empty string.")
        else:
            parsed = urlparse(url_val)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                errors.append(
                    "Field 'url' must be an absolute URL with http(s) scheme and host."
                )

    # version (soft semver check; adjust if your ecosystem allows non-semver)
    if "version" in data:
        ver = data["version"]
        if not _is_non_empty_str(ver):
            errors.append("Field 'version' must be a non-empty string.")
        elif not _SEMVER_RE.match(ver):
            errors.append(
                "Field 'version' should be semver-like (e.g., '1.2.3' or '1.2.3-alpha')."
            )

    # capabilities
    if "capabilities" in data:
        if not isinstance(data["capabilities"], dict):
            errors.append("Field 'capabilities' must be an object.")
        else:
            # Optional: sanity checks for common capability fields
            caps = data["capabilities"]
            if "streaming" in caps and not isinstance(caps["streaming"], bool):
                errors.append("Field 'capabilities.streaming' must be a boolean if present.")

    # defaultInputModes / defaultOutputModes
    for field in ("defaultInputModes", "defaultOutputModes"):
        if field in data:
            modes = data[field]
            if not _is_list_of_str(modes):
                errors.append(f"Field '{field}' must be an array of strings.")
            elif len(modes) == 0:
                errors.append(f"Field '{field}' must not be empty.")

    # skills
    if "skills" in data:
        skills = _as_sequence(data["skills"])
        if skills is None:
            errors.append("Field 'skills' must be an array.")
        elif len(skills) == 0:
            errors.append(
                "Field 'skills' must not be empty. Agent must have at least one skill if it performs actions."
            )
        else:
            # If entries are objects, check they have a name
            for i, s in enumerate(skills):
                if isinstance(s, Mapping):
                    if not _is_non_empty_str(s.get("name")):
                        errors.append(f"skills[{i}].name is required and must be a non-empty string.")
                elif not isinstance(s, str):
                    errors.append(
                        f"skills[{i}] must be either an object with 'name' or a string; found: {type(s).__name__}"
                    )

    return errors


# -----------------------------
# Agent Message/Event Validation
# -----------------------------

def _validate_task(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if "id" not in data:
        errors.append("Task object missing required field: 'id'.")
    status = _as_mapping(data.get("status"))
    if status is None or "state" not in status:
        errors.append("Task object missing required field: 'status.state'.")
    return errors


def _validate_status_update(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    status = _as_mapping(data.get("status"))
    if status is None or "state" not in status:
        errors.append("StatusUpdate object missing required field: 'status.state'.")
    return errors


def _validate_artifact_update(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    artifact = _as_mapping(data.get("artifact"))
    if artifact is None:
        errors.append("ArtifactUpdate object missing required field: 'artifact'.")
        return errors

    parts = artifact.get("parts")
    if not isinstance(parts, list) or len(parts) == 0:
        errors.append("Artifact object must have a non-empty 'parts' array.")
    return errors


def _validate_message(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    parts = data.get("parts")
    if not isinstance(parts, list) or len(parts) == 0:
        errors.append("Message object must have a non-empty 'parts' array.")
    role = data.get("role")
    if role != "agent":
        errors.append("Message from agent must have 'role' set to 'agent'.")
    # Optional: check text presence in at least one part if parts are objects
    # (Leave relaxed to avoid false negatives if parts are other media-types)
    return errors


_KIND_VALIDATORS: dict[str, callable[[dict[str, Any]], list[str]]] = {
    "task": _validate_task,
    "status-update": _validate_status_update,
    "artifact-update": _validate_artifact_update,
    "message": _validate_message,
}


def validate_message(data: dict[str, Any]) -> list[str]:
    """
    Validate an incoming event/message coming from the agent according to its 'kind'.

    Expected kinds: 'task', 'status-update', 'artifact-update', 'message'
    Returns:
        A list of human-readable error strings. Empty list means "looks valid".
    """
    if not isinstance(data, Mapping):
        return ["Response from agent must be an object."]
    if "kind" not in data:
        return ["Response from agent is missing required 'kind' field."]

    kind = str(data.get("kind"))
    validator = _KIND_VALIDATORS.get(kind)
    if validator:
        return validator(dict(data))

    return [f"Unknown message kind received: '{kind}'."]


__all__ = [
    "validate_agent_card",
    "validate_message",
]
