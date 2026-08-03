from pathlib import Path

from codex_forge.knowledge import KnowledgeStore


def test_relevant_patterns_are_project_scoped(tmp_path: Path):
    store = KnowledgeStore(tmp_path)
    store.add_patterns(
        "TASK-1",
        "project-a",
        [{"trigger": "dotnet compile failure", "lesson": "Restore packages first", "evidence": "Build passed"}],
    )
    store.add_patterns(
        "TASK-2",
        "project-b",
        [{"trigger": "python failure", "lesson": "Use venv", "evidence": "Tests passed"}],
    )
    records = store.relevant("project-a", "dotnet build compile")
    assert len(records) == 1
    assert records[0]["lesson"] == "Restore packages first"
