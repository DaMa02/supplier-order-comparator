#!/usr/bin/env python3
"""Merge explicit AI routing decisions into deterministic source profiles."""

from __future__ import annotations

import argparse
import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    profiles_doc = load(args.profiles)
    decisions_doc = load(args.decisions)
    decisions = decisions_doc.get("decisions") if isinstance(decisions_doc, dict) else decisions_doc
    if not isinstance(decisions, list):
        raise ValueError("Il file decisioni deve contenere una lista o {decisions: [...]}")

    by_id = {}
    by_name = {}
    for decision in decisions:
        if decision.get("profile_id"):
            if decision["profile_id"] in by_id:
                raise ValueError(f"Decisione duplicata per profile_id {decision['profile_id']}")
            by_id[decision["profile_id"]] = decision
        if decision.get("file_name"):
            key = str(decision["file_name"]).casefold()
            if key in by_name:
                raise ValueError(f"Decisione duplicata per file_name {decision['file_name']}")
            by_name[key] = decision

    files = []
    missing = []
    used = set()
    for profile in profiles_doc.get("profiles", []):
        decision = by_id.get(profile.get("profile_id")) or by_name.get(str(profile.get("file_name") or "").casefold())
        item = copy.deepcopy(profile)
        if decision is None:
            missing.append(profile.get("file_name"))
        else:
            used.add(id(decision))
            ai = {key: value for key, value in decision.items() if key not in {"profile_id", "file_name", "user_confirmation"}}
            item["ai_preflight"] = ai
            item["user_confirmation"] = decision.get("user_confirmation", {"required": False, "status": "NOT_REQUIRED"})
        files.append(item)

    unused = [decision.get("file_name") or decision.get("profile_id") for decision in decisions if id(decision) not in used]
    if missing or unused:
        raise ValueError(f"Decisioni non riconciliate; mancanti={missing}, inutilizzate={unused}")

    manifest = {
        "schema_version": 1,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "source_profiles": str(args.profiles.resolve()),
        "ai_preflight_status": "COMPLETE",
        "files": files,
        "postcheck": {"status": "PENDING", "decision": None, "rationale": None},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve()), "files": len(files), "status": "COMPLETE"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
