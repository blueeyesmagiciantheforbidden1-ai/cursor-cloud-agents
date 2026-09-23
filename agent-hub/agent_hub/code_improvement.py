"""Bounded JSON interface to the full development tool lab and shared core.

Commands read one JSON object on stdin and write one JSON result. They perform
planning/validation only: no providers, candidate processes, cloud calls or
production changes. Real execution belongs to the hub's trusted controllers.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import tool_lab_core as core


def analyze(telemetry):
    """Use the full port's actual multi-component planners on explicit telemetry.

    Names are translated telemetry names from PORT_PROVENANCE.json. This data
    must come from development workloads; hidden test contents are not accepted.
    The returned plans are bounded research proposals, never acceptance evidence.
    """
    telemetry = core.clone(telemetry)
    core.keys(telemetry, ("index", "recent", "evidence", "matched_lift", "patch_garden", "main_search_flow"))
    for key, value in telemetry.items():
        core.require(type(value) is (list if key == "patch_garden" else dict), "telemetry_invalid")
    core.require(len(telemetry["patch_garden"]) <= 48, "memory_limit")
    pending = [(telemetry, 0)]
    while pending:
        value, depth = pending.pop()
        core.require(depth <= 12, "telemetry_depth_limit")
        if isinstance(value, dict):
            core.require(len(value) <= 128 and all(type(k) is str and len(k) <= 128 for k in value), "telemetry_fields_limit")
            core.require(not any(any(word in k.casefold() for word in
                ("holdout", "hidden", "secret", "credential", "token", "password")) for k in value),
                "withheld_or_credential_data_forbidden")
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            core.require(len(value) <= 128, "telemetry_items_limit")
            pending.extend((item, depth + 1) for item in value)
        elif isinstance(value, str):
            core.text(value, 500)
        elif type(value) in (int, float):
            core.require(-1_000_000 <= value <= 1_000_000, "telemetry_number_limit")
        else:
            core.require(value is None or type(value) is bool, "telemetry_value_invalid")
    from . import development_tool_lab as port
    kwargs = dict(index=telemetry["index"], recent=telemetry["recent"], evidence=telemetry["evidence"],
        matched_lift=telemetry["matched_lift"], patch_garden=telemetry["patch_garden"],
        main_search_flow=telemetry["main_search_flow"])
    # Original main_ga_flow identifier translated to main_search_flow.
    atom = port.build_tool_atom_flow_ga_plan(**kwargs)
    components = port.build_component_atom_promotion_gas(**kwargs)
    shadow = port.build_continuous_section_shadow_lane_mixer(index=kwargs["index"], recent=kwargs["recent"],
        evidence=kwargs["evidence"], patch_garden=kwargs["patch_garden"], main_search_flow=kwargs["main_search_flow"])
    result = {"schema_version": 1, "source_sha256": core.SOURCE_SHA256, "domain": "software_development",
        "atom_plan": atom, "component_plan": components, "shadow_plan": shadow,
        "provider_calls": 0, "dispatch_authorized": False, "promotion_allowed": False}
    core.canonical(result)
    return result


def dispatch(action, request):
    if action == "plan":
        core.keys(request, ("ticket", "roster", "allocation", "now", "generation", "memory"))
        return core.make_plan(**request)
    if action == "analyze":
        return analyze(request)
    if action == "handoff":
        core.keys(request, ("plan", "agent"))
        return core.handoff(**request)
    if action == "assess":
        core.keys(request, ("plan", "lane_id", "stage", "evidence", "previous"))
        return core.assess_stage(**request)
    raise core.ToolLabError("action_invalid")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "analyze", "handoff", "assess"))
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.buffer.read(core.MAX_BYTES + 1)
        core.require(len(raw) <= core.MAX_BYTES, "request_size_limit")
        def pairs(items):
            result = {}
            for key, value in items:
                core.require(key not in result, "duplicate_json_key")
                result[key] = value
            return result
        request = json.loads(raw, object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(core.ToolLabError("finite_json_required")))
        result = dispatch(args.action, request)
        print(json.dumps({"ok": True, "result": result}, allow_nan=False))
        return 0
    except core.ToolLabError as error:
        code = str(error)
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError, UnicodeError):
        code = "request_invalid"
    print(json.dumps({"ok": False, "error": code}))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
