# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from typing import Any

from json_repair import loads as repair_json_loads

from .models import HookDecision, HookEventName, HookHandlerResult, HookOutput

PROMPT_JUDGMENT_MAX_REASON_LENGTH = 2000
_PROMPT_JUDGMENT_KEYS = {"decision", "reason"}
_PROMPT_JUDGMENT_DECISIONS = {
    "allow": HookDecision.ALLOW,
    "deny": HookDecision.DENY,
    "block": HookDecision.BLOCK,
    "stop": HookDecision.STOP,
}
_STOP_PROMPT_JUDGMENT_DECISIONS = {
    "allow": HookDecision.ALLOW,
    "block": HookDecision.BLOCK,
}
_STOP_UNSUPPORTED_TOP_LEVEL_EFFECT_FIELDS = (
    ("continue_", "continue"),
    ("stop_reason", "stopReason"),
    ("suppress_output", "suppressOutput"),
    ("system_message", "systemMessage"),
)
_STOP_TRANSFORM_SPECIFIC_KEYS = {"replacementText"}


def _event_name_value(event_name: HookEventName | str | None) -> str:
    return str(getattr(event_name, "value", event_name or ""))


def _prompt_judgment_decisions(
    event_name: HookEventName | str | None,
) -> dict[str, HookDecision]:
    if _event_name_value(event_name) == HookEventName.STOP.value:
        return _STOP_PROMPT_JUDGMENT_DECISIONS
    return _PROMPT_JUDGMENT_DECISIONS


def _validate_stop_hook_output(output: HookOutput) -> None:
    if output.decision not in {"allow", "block"}:
        raise ValueError("Stop hook output has unsupported decision")

    unsupported_effect_fields = [
        field_name
        for attr_name, field_name in (
            _STOP_UNSUPPORTED_TOP_LEVEL_EFFECT_FIELDS
        )
        if getattr(output, attr_name) is not None
    ]
    if unsupported_effect_fields:
        raise ValueError(
            "Stop hook output has unsupported output fields",
        )

    specific = output.hook_specific_output or {}
    if specific:
        raise ValueError(
            "Stop hook output has unsupported hookSpecificOutput",
        )


def _validate_stop_transform_output(output: HookOutput) -> str | None:
    if output.model_extra:
        raise ValueError("Stop outputTransform has unsupported output fields")
    if output.decision != "allow":
        raise ValueError("Stop outputTransform has unsupported decision")

    unsupported_effect_fields = [
        field_name
        for attr_name, field_name in (
            _STOP_UNSUPPORTED_TOP_LEVEL_EFFECT_FIELDS
        )
        if getattr(output, attr_name) is not None
    ]
    if unsupported_effect_fields:
        raise ValueError("Stop outputTransform has unsupported output fields")

    specific = output.hook_specific_output or {}
    if set(specific) - _STOP_TRANSFORM_SPECIFIC_KEYS:
        raise ValueError(
            "Stop outputTransform has unsupported hookSpecificOutput",
        )
    replacement = specific.get("replacementText")
    if replacement is not None and (
        not isinstance(replacement, str) or not replacement.strip()
    ):
        raise ValueError(
            "Stop outputTransform replacementText must be a non-empty string",
        )
    return replacement


def _parse_prompt_judgment_json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as original_exc:
        try:
            return repair_json_loads(text, skip_json_loads=True)
        except ValueError as repair_exc:
            raise ValueError(
                f"Invalid prompt hook JSON output: {original_exc}",
            ) from repair_exc


def _validate_hook_output_contract(
    output: HookOutput,
    *,
    is_stop: bool,
    output_transform: bool,
) -> str | None:
    if output_transform and not is_stop:
        raise ValueError("outputTransform is supported on Stop only")
    if not is_stop:
        return None
    if output_transform:
        return _validate_stop_transform_output(output)
    _validate_stop_hook_output(output)
    return None


def _resolve_hook_decision(
    output: HookOutput,
    *,
    is_stop: bool,
) -> tuple[HookDecision, str]:
    reason = output.reason or ""
    if is_stop and output.decision == "allow":
        return HookDecision.ALLOW, reason
    if output.continue_ is False:
        return HookDecision.STOP, output.stop_reason or reason or (
            "Hook requested stop"
        )
    if output.decision == "stop":
        return HookDecision.STOP, reason or "Hook requested stop"
    if output.decision == "block":
        return HookDecision.BLOCK, reason or "Hook blocked the event"
    return HookDecision.NONE, reason


def _override_permission_decision(
    output: HookOutput,
    *,
    decision: HookDecision,
    reason: str,
    output_transform: bool,
) -> tuple[HookDecision, str]:
    if output_transform:
        return decision, reason

    specific = output.hook_specific_output or {}
    permission_decision = specific.get("permissionDecision")
    permission_reason = specific.get("permissionDecisionReason")
    if permission_decision in {"allow", "deny", "ask"}:
        return HookDecision(permission_decision), str(
            permission_reason or reason or "",
        )
    if permission_decision == "defer":
        return (
            HookDecision.BLOCK,
            "Hook permissionDecision=defer is not supported",
        )
    return decision, reason


def normalize_hook_output(
    *,
    handler_id: str,
    order: int,
    raw_output: dict[str, Any],
    event_name: HookEventName | str | None = None,
    output_transform: bool = False,
) -> HookHandlerResult:
    output = HookOutput.model_validate(raw_output)
    is_stop = _event_name_value(event_name) == HookEventName.STOP.value
    replacement_text = _validate_hook_output_contract(
        output,
        is_stop=is_stop,
        output_transform=output_transform,
    )
    decision, reason = _resolve_hook_decision(output, is_stop=is_stop)
    decision, reason = _override_permission_decision(
        output,
        decision=decision,
        reason=reason,
        output_transform=output_transform,
    )

    return HookHandlerResult(
        handler_id=handler_id,
        order=order,
        output=output,
        decision=decision,
        reason=reason,
        replacement_text=replacement_text,
    )


def normalize_prompt_judgment_output(
    *,
    handler_id: str,
    order: int,
    text: str,
    event_name: HookEventName | str | None = None,
    output_transform: bool = False,
) -> HookHandlerResult:
    raw = _parse_prompt_judgment_json(text)
    if not isinstance(raw, dict):
        raise ValueError("Prompt hook output must be a JSON object")
    prompt_keys = _PROMPT_JUDGMENT_KEYS
    if output_transform:
        prompt_keys = _PROMPT_JUDGMENT_KEYS | {"hookSpecificOutput"}
    if not _PROMPT_JUDGMENT_KEYS.issubset(raw) or set(raw) - prompt_keys:
        raise ValueError(
            "Prompt hook output has unsupported keys",
        )

    allowed_decisions = _prompt_judgment_decisions(event_name)
    if output_transform:
        if _event_name_value(event_name) != HookEventName.STOP.value:
            raise ValueError("outputTransform is supported on Stop only")
        allowed_decisions = {"allow": HookDecision.ALLOW}
    decision_value = raw.get("decision")
    if decision_value not in allowed_decisions:
        raise ValueError("Prompt hook output has unsupported decision")

    reason = raw.get("reason")
    if not isinstance(reason, str):
        raise ValueError("Prompt hook output reason must be a string")
    reason = reason.strip()
    if not reason:
        raise ValueError("Prompt hook output reason must be non-empty")
    if len(reason) > PROMPT_JUDGMENT_MAX_REASON_LENGTH:
        raise ValueError("Prompt hook output reason is too long")

    output = HookOutput(
        decision=decision_value,
        reason=reason,
        hookSpecificOutput=raw.get("hookSpecificOutput") or {},
    )
    replacement_text = (
        _validate_stop_transform_output(output) if output_transform else None
    )
    return HookHandlerResult(
        handler_id=handler_id,
        order=order,
        output=output,
        decision=allowed_decisions[decision_value],
        reason=reason,
        replacement_text=replacement_text,
    )
