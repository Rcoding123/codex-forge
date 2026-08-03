from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .utils import ensure_dir, utc_now


class KnowledgeStore:
    def __init__(self, root: Path):
        self.root = ensure_dir(root)
        self.patterns_path = self.root / "patterns.jsonl"

    def add_patterns(self, task_id: str, project_id: str, patterns: list[dict[str, Any]]) -> None:
        if not patterns:
            return
        with self.patterns_path.open("a", encoding="utf-8") as handle:
            for pattern in patterns:
                record = {
                    "task_id": task_id,
                    "project_id": project_id,
                    "recorded_at": utc_now(),
                    "confidence": "candidate",
                    **pattern,
                }
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def relevant(self, project_id: str, query: str, limit: int = 8) -> list[dict[str, Any]]:
        if not self.patterns_path.exists():
            return []
        query_terms = self._terms(query)
        scored: list[tuple[int, dict[str, Any]]] = []
        with self.patterns_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("project_id") not in (project_id, "global"):
                    continue
                haystack = " ".join(str(v) for v in record.values())
                score = len(query_terms & self._terms(haystack))
                if score:
                    scored.append((score, record))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [record for _, record in scored[:limit]]

    @staticmethod
    def _terms(value: str) -> set[str]:
        return {term.lower() for term in re.findall(r"[A-Za-z0-9_+#.-]{3,}", value)}
