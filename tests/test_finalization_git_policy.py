"""Real Git adversarial policy checks, without controller state or model workers."""
from pathlib import Path

import pytest
from test_final_evidence import git, initialize_repository

from codex_forge.finalization import _visible
from codex_forge.gitops import CandidateStateUnavailable


@pytest.mark.parametrize('location', ['.gitignore', 'engine/src/.gitignore', 'engine/src/.GITIGNORE'])
def test_self_hiding_unknown_ignore_is_not_candidate_authority(tmp_path, location):
    repo, _ = initialize_repository(tmp_path)
    _visible(repo)
    path = repo / location
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'.gitignore\n.GITIGNORE\nhidden.py\n')
    (path.parent / 'hidden.py').write_bytes(b'unreviewed = True\n')
    with pytest.raises(CandidateStateUnavailable):
        _visible(repo)


def test_staged_ignore_cannot_become_new_prospective_authority(tmp_path):
    repo, _ = initialize_repository(tmp_path)
    (repo / '.gitignore').write_bytes(b'.gitignore\nhidden.py\n')
    git(repo, 'add', '-f', '.gitignore')
    (repo / 'hidden.py').write_bytes(b'unreviewed = True\n')
    with pytest.raises(CandidateStateUnavailable):
        _visible(repo)


def test_generated_ignores_are_allowed_only_below_versioned_ignored_ancestors(tmp_path):
    repo, _ = initialize_repository(tmp_path)
    (repo / '.gitignore').write_bytes(b'.venv/\ncache/.gitignore\n')
    git(repo, 'add', '.gitignore')
    git(repo, 'commit', '-m', 'Versioned generated directory policy')
    (repo / '.venv/nested').mkdir(parents=True)
    (repo / '.venv/nested/.gitignore').write_bytes(b'*\n')
    (repo / '.venv/nested/generated.py').write_bytes(b'ordinary generated cache\n')
    assert set(_visible(repo)) == {'base.txt', 'revert.txt', '.gitignore'}
    (repo / 'cache').mkdir()
    (repo / 'cache/.gitignore').write_bytes(b'hidden.py\n')
    (repo / 'cache/hidden.py').write_bytes(b'unreviewed = True\n')
    with pytest.raises(CandidateStateUnavailable):
        _visible(repo)


def test_changed_tracked_ignore_cannot_hide_new_inputs(tmp_path):
    repo, _ = initialize_repository(tmp_path)
    (repo / '.gitignore').write_bytes(b'.venv/\n')
    git(repo, 'add', '.gitignore')
    git(repo, 'commit', '-m', 'Versioned ignore')
    (repo / '.gitignore').write_bytes(b'.venv/\nhidden.py\n')
    (repo / 'hidden.py').write_bytes(b'unreviewed = True\n')
    with pytest.raises(CandidateStateUnavailable):
        _visible(repo)


def install_filter(repo):
    script = repo.parent / 'clean.py'
    script.write_bytes(b'import sys\nsys.stdin.buffer.read()\nsys.stdout.buffer.write(b"unreviewed committed bytes\\n")\n')
    info = Path(git(repo, 'rev-parse', '--git-path', 'info/attributes').strip())
    if not info.is_absolute():
        info = repo / info
    info.parent.mkdir(parents=True, exist_ok=True)
    info.write_bytes(b'base.txt filter=forge-probe\n')
    import sys
    git(repo, 'config', 'filter.forge-probe.clean', '"' + sys.executable.replace('\\', '/') + '" "' + str(script).replace('\\', '/') + '"')


def test_local_clean_conversion_cannot_substitute_reviewed_bytes(tmp_path):
    repo, _ = initialize_repository(tmp_path)
    _visible(repo)
    install_filter(repo)
    with pytest.raises(CandidateStateUnavailable, match='conversion'):
        _visible(repo)


def test_exact_staged_tree_cannot_substitute_worker_or_baseline_blobs(tmp_path):
    from codex_forge.finalization import _exact_raw_tree

    repo, base = initialize_repository(tmp_path)
    name = 'engine/src/demo.py'
    path = repo / name
    path.parent.mkdir(parents=True)
    path.write_bytes(b'reviewed = 42\n')
    index = git(repo, 'ls-files', '--stage', '-z')
    document = {'original_candidate': [base, 'main', index, '', [], []], 'files': [],
                'receipt': {'worker_paths': [name]}}
    git(repo, 'add', name)
    tree = git(repo, 'write-tree').strip()
    _exact_raw_tree(repo, tree, document)
    wrong = tmp_path / 'wrong.txt'
    wrong.write_bytes(b'unreviewed = 99\n')
    oid = git(repo, 'hash-object', '-w', str(wrong)).strip()
    git(repo, 'update-index', '--cacheinfo', '100644,' + oid + ',' + name)
    with pytest.raises(CandidateStateUnavailable, match='raw bytes'):
        _exact_raw_tree(repo, git(repo, 'write-tree').strip(), document)
    git(repo, 'add', name)
    git(repo, 'update-index', '--chmod=+x', 'base.txt')
    with pytest.raises(CandidateStateUnavailable, match='raw bytes or modes'):
        _exact_raw_tree(repo, git(repo, 'write-tree').strip(), document)


def test_baseline_conversion_still_requires_exact_original_committed_identity(tmp_path):
    from codex_forge.finalization import _raw_git_inputs

    repo, _ = initialize_repository(tmp_path)
    git(repo, 'config', 'core.autocrlf', 'true')
    (repo / 'base.txt').write_bytes(b'base\r\n')
    git(repo, 'add', 'base.txt')
    git(repo, 'commit', '--allow-empty', '-m', 'Pin builtin checkout normalization')
    approved = git(repo, 'rev-parse', 'HEAD:base.txt').strip()
    _raw_git_inputs(repo, ['base.txt'], allow_crlf=True, approved={'base.txt': approved})
    git(repo, 'config', 'core.autocrlf', 'false')
    with pytest.raises(CandidateStateUnavailable, match='conversion'):
        _raw_git_inputs(repo, ['base.txt'], allow_crlf=True, approved={'base.txt': approved})



def test_ignore_exemption_cannot_be_broadened_by_host_case_defaults(tmp_path):
    repo, _ = initialize_repository(tmp_path)
    (repo / '.gitignore').write_bytes(b'build/\n')
    git(repo, 'add', '.gitignore')
    git(repo, 'commit', '-m', 'Exact versioned build exclusion')
    git(repo, 'config', 'core.ignoreCase', 'false')
    folder = repo / 'BUILD'
    folder.mkdir()
    (folder / '.gitignore').write_bytes(b'.gitignore\nhidden.py\n')
    (folder / 'hidden.py').write_bytes(b'unreviewed = True\n')
    with pytest.raises(CandidateStateUnavailable, match='ignore policy'):
        _visible(repo)


@pytest.mark.parametrize('location', ['repository', 'source', 'gate'])
def test_source_gate_filter_guard_precedes_conversion_capable_source_proof(tmp_path, monkeypatch, location):
    from types import SimpleNamespace
    import codex_forge.finalization as module

    repo, base = initialize_repository(tmp_path)
    source = tmp_path / 'source-checkout'
    data = tmp_path / 'controller'
    gate = data / 'gate-worktrees' / ('gate-' + 'a' * 32)
    gate.parent.mkdir(parents=True)
    git(repo, 'worktree', 'add', '--detach', str(source), base)
    git(repo, 'worktree', 'add', '--detach', str(gate), base)
    selected = {'repository': repo, 'source': source, 'gate': gate}[location]
    (selected / '.gitattributes').write_bytes(b'base.txt filter=unsafe\n')
    db = SimpleNamespace(get_project=lambda _: SimpleNamespace(id='source-project', repo_path=repo),
                         get_task=lambda _: SimpleNamespace(project_id='source-project', worktree_path=source))
    proof = {'source_project_id': 'source-project', 'source_task_id': 'source-task', 'gate_id': 'a' * 32}
    reached = []
    monkeypatch.setattr(module, '_source', lambda *args: reached.append(True))
    with pytest.raises(CandidateStateUnavailable, match='filter conversion'):
        module._checked_source(SimpleNamespace(data_root=data), db, None, None, base, proof)
    assert reached == []


def test_parent_filter_guard_precedes_old_retained_source_proof(monkeypatch):
    import json
    from types import SimpleNamespace
    import codex_forge.finalization as module

    proof = {'source_project_id': 'old', 'source_task_id': 'old-task', 'gate_id': 'a' * 32}
    document = {'id': 'parent', 'prerequisite': {'source': {'proof': proof}}}
    db = SimpleNamespace(blocker_continuation=lambda *_: {'state': 'TERMINAL', 'document_json': json.dumps(document)})
    reached = []
    def guard(*args):
        assert args[-1] == proof
        raise CandidateStateUnavailable('old source filter refused')
    monkeypatch.setattr(module, '_guard_source', guard)
    monkeypatch.setattr(module, '_prove_source', lambda *args: reached.append(True))
    with pytest.raises(CandidateStateUnavailable, match='old source filter'):
        module._parent(None, db, SimpleNamespace(id='target', attempts=3), None, 'parent')
    assert reached == []
