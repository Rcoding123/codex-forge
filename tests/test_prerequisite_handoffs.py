from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_blocker_continuations import BlockedWorker
from test_final_evidence import completed_result, git, passing_validation
from test_project_workflows import install, setup
from test_review_infrastructure import run_result

from codex_forge.blockers import queue_resolution
from codex_forge.cli import main
from codex_forge.gitops import CandidateStateUnavailable, GitError
from codex_forge.models import Project, Task, TaskState
from codex_forge.orchestrator import Orchestrator
from codex_forge.prerequisites import preflight_handoff
from codex_forge.task_submission import task_fingerprint
from codex_forge.utils import controller_lock


class PrerequisiteWorker(BlockedWorker):
    def run(self, *, phase, artifact_dir, cwd, prompt, **kwargs):
        if phase != 'implementation':
            return super().run(phase=phase, artifact_dir=artifact_dir, cwd=cwd, prompt=prompt, **kwargs)
        self.phases.append(phase)
        self.prompts.append(prompt)
        root = Path(cwd)
        if (root / 'client/reveal.txt').exists():
            files = ['docs/evidence/reveal-prerequisite/controller-readiness.md']
        else:
            files = ['client/reveal.txt', 'contracts/reveal.json']
        for name in files:
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_bytes(b'approved prerequisite\n')
        return run_result(Path(artifact_dir), phase, completed_result(files, 'separate prerequisite'), [])


def fixture(tmp_path, monkeypatch):
    o, project, first, _second, definition = setup(tmp_path, monkeypatch)
    o.config = replace(o.config, max_repair_cycles=2, max_identical_failures=3)
    target_worker = BlockedWorker()
    o.runner = target_worker
    install(o, project, definition)
    for _ in range(4):
        o.run_once()
    task = o.db.get_task(first.id)
    assert task.state == TaskState.FAILED and task.attempts == 3
    base = o.db.execution_checkpoint(task.id)['base_commit']
    source_repo = tmp_path / 'prerequisite-repo'
    git(tmp_path, 'clone', str(project.repo_path), str(source_repo))
    git(source_repo, 'config', 'user.name', 'Prerequisite Test')
    git(source_repo, 'config', 'user.email', 'prerequisite@example.invalid')
    command = "python -c \"print('gate passed')\""
    source = Project('prerequisite', 'prerequisite', source_repo, validation_commands=[command], auto_fetch=False)
    o.db.add_project(source)
    source = o.db.get_project(source.id)
    source_task = Task('source-task', source.id, 'shared reveal prerequisite', 'implement generic reveal', [], [], TaskState.READY,
                       scope='client/reveal.txt\ncontracts/reveal.json', admission_base_commit=base)
    o.db.add_task(source_task)
    source_worker = PrerequisiteWorker()
    o.runner = source_worker
    monkeypatch.setattr(o, '_validate', Orchestrator._validate.__get__(o))
    claimed = o.db.claim_ready_tasks(1)
    assert [t.id for t in claimed] == [source_task.id]
    o._execute_task_body(claimed[0])
    source_task = o.db.get_task(source_task.id)
    assert source_task.state == TaskState.COMPLETED
    p = source_task.result['commit']
    gate_task = Task('gate-task', source.id, 'gate prerequisite', 'retain readiness', [], [], TaskState.BACKLOG,
                     dependencies=[source_task.id], scope='docs/evidence/reveal-prerequisite/**')
    o.db.add_task(gate_task)
    plan = install(o, source, {'branch': 'forge-project/prerequisite', 'base_commit': p,
                             'phases': [{'name': 'Ready', 'tasks': [gate_task.id], 'acceptance_commands': [command]}]})
    for _ in range(4):
        o.run_once()
    plan = o.workflows.get(source.id)
    assert plan['state'] == 'COMPLETED' and plan['gates'][-1]['status'] == 'PASSED'
    o.runner = target_worker
    monkeypatch.setattr(o, '_validate', lambda *_, **__: passing_validation(tmp_path, True))
    pins = SimpleNamespace(source_task=source_task.id, source_registration=source.repository_registration_id,
                           source_candidate=p, workflow_id=plan['id'], gate_id=plan['gates'][-1]['id'], gate_candidate=plan['head_commit'])
    reason = 'Import independently completed and gated generic reveal prerequisite.'
    preflight = preflight_handoff(o.config, task.id, reason, pins)
    receipt = tmp_path / 'handoff-receipt.json'
    receipt.write_bytes(json.dumps(preflight['receipt'], indent=2).encode('utf-8'))
    digest = hashlib.sha256(receipt.read_bytes()).hexdigest()
    assert digest == preflight['receipt_sha256']
    return o, project, task, source, source_task, pins, reason, receipt, digest


def invoke(f):
    o, _, task, _, _, _, reason, receipt, digest = f
    return o.resolve_blocker(task.id, reason, receipt, digest, prerequisite=True)


def test_gated_prerequisite_handoff_preserves_frozen_task_and_runs_full_pipeline(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    o, project, task, source, source_task, pins, _, receipt, _ = f
    before = task_fingerprint(task)
    frozen = o.workflows.get(project.id)
    original = o.config.evidence_root / project.name / task.id / 'attempt-03'
    old = {str(p.relative_to(original)): p.read_bytes() for p in original.rglob('*') if p.is_file()}
    assert o.db.blocker_continuation(task.id, 3) is None  # preflight has no reservation or mutation.
    assert not (task.worktree_path / 'client/reveal.txt').exists()
    result = invoke(f)
    assert result['task_state'] == 'COMPLETED'
    task = o.db.get_task(task.id)
    plan = o.workflows.get(project.id)
    assert task.attempts == task.max_attempts == 3 and task_fingerprint(task) == before
    assert plan['specifications'] == frozen['specifications'] and plan['phases'] == frozen['phases']
    assert plan['gates'][-1]['status'] == 'PASSED' and plan['gates'][-1]['commit'] == task.result['commit']
    assert task.result['base_commit'] == frozen['head_commit']
    assert {'client/reveal.txt', 'contracts/reveal.json', 'candidate.txt'} <= set(task.result['changed_files'])
    assert 'docs/evidence/reveal-prerequisite/controller-readiness.md' not in task.result['changed_files']
    assert o.runner.phases[-2:] == ['validation-repair-1', 'review']
    assert all('CONTROLLER-OWNED PREREQUISITE HANDOFF' in p for p in o.runner.prompts[-2:])
    assert task.result['prerequisite_handoff']['worker_changed_files'] == ['candidate.txt']
    root = Path(task.result['patch']).parent
    assert b'client/reveal.txt' in (root / 'prerequisite.patch').read_bytes()
    assert b'client/reveal.txt' not in (root / 'worker-changes.patch').read_bytes()
    assert b'candidate.txt' in (root / 'worker-changes.patch').read_bytes()
    assert all(source_task.result['commit'] in p and task.scope in p for p in o.runner.prompts[-2:])
    assert old == {str(p.relative_to(original)): p.read_bytes() for p in original.rglob('*') if p.is_file()}
    phases = list(o.runner.phases)
    assert invoke(f)['state'] == 'TERMINAL' and o.runner.phases == phases
    assert git(project.repo_path, 'rev-parse', 'main').strip() == frozen['base_commit']
    assert git(source.repo_path, 'rev-parse', 'forge-project/prerequisite').strip() == pins.gate_candidate
    assert json.loads(receipt.read_bytes())['prerequisite']['allowed_paths'] == ['client/reveal.txt', 'contracts/reveal.json']


@pytest.mark.parametrize('invalid', ['candidate', 'receipt', 'source_dirty', 'source_generation', 'source_spec', 'source_final',
                                      'source_gate', 'gate_record', 'source_base', 'unapproved_path', 'no_seal', 'overlap', 'index', 'gate_checkout', 'gate_assume_unchanged', 'gate_skip_worktree'])
def test_handoff_refuses_changed_or_ambiguous_inputs_without_paid_repair(tmp_path, monkeypatch, invalid):
    f = fixture(tmp_path, monkeypatch)
    o, project, task, source, source_task, pins, reason, receipt, digest = f
    proof = json.loads(receipt.read_bytes())
    if invalid == 'candidate':
        (task.worktree_path / 'base.txt').write_text('external change')
    elif invalid == 'receipt':
        digest = '0' * 64
    elif invalid == 'source_dirty':
        (source_task.worktree_path / 'client/reveal.txt').write_text('external change')
    elif invalid == 'source_generation':
        with o.db.connect() as connection:
            connection.execute("UPDATE projects SET repository_registration_id='changed' WHERE id=?", (source.id,))
    elif invalid == 'source_spec':
        with o.db.connect() as connection:
            connection.execute("UPDATE tasks SET objective='changed' WHERE id=?", (source_task.id,))
    elif invalid == 'source_final':
        with o.db.connect() as connection:
            checkpoint = o.db.execution_checkpoint(source_task.id)
            final = json.loads(checkpoint['completion_json'])
            final['review']['verdict'] = 'changes_required'
            connection.execute('UPDATE execution_checkpoints SET completion_json=? WHERE task_id=?', (json.dumps(final), source_task.id))
    elif invalid == 'source_gate':
        with o.db.connect() as connection:
            plan = o.workflows.get(source.id)
            plan['gates'][-1]['commit'] = task.result.get('base_commit', 'wrong')
            connection.execute('UPDATE project_workflows SET document_json=? WHERE project_id=?', (json.dumps(plan), source.id))
    elif invalid == 'gate_record':
        path = o.config.data_root / 'project-evidence' / pins.workflow_id / f'gate-{pins.gate_id}' / 'result.json'
        path.write_text('{}')
    elif invalid == 'gate_checkout':
        (o.config.data_root / 'gate-worktrees' / f'gate-{pins.gate_id}' / 'base.txt').write_text('dirty')
    elif invalid in ('gate_assume_unchanged', 'gate_skip_worktree'):
        gate_checkout = o.config.data_root / 'gate-worktrees' / f'gate-{pins.gate_id}'
        flag = '--assume-unchanged' if invalid == 'gate_assume_unchanged' else '--skip-worktree'
        git(gate_checkout, 'update-index', flag, 'client/reveal.txt')
        (gate_checkout / 'client/reveal.txt').write_bytes(b'UNREVIEWED GATE INPUT')
        assert git(gate_checkout, 'status', '--porcelain') == ''
    elif invalid == 'source_base':
        with o.db.connect() as connection:
            connection.execute("UPDATE execution_checkpoints SET base_commit=? WHERE task_id=?", (pins.source_candidate, source_task.id))
    elif invalid == 'unapproved_path':
        proof['prerequisite']['allowed_paths'] = ['client/reveal.txt']
    elif invalid == 'no_seal':
        (o.config.evidence_root / project.name / task.id / 'attempt-03/implementation/blocked-candidate.json').unlink()
    elif invalid == 'overlap':
        (task.worktree_path / 'client').mkdir()
        (task.worktree_path / 'client/reveal.txt').write_bytes(b'own L02 change')
    elif invalid == 'index':
        git(task.worktree_path, 'add', 'base.txt')
        git(task.worktree_path, 'update-index', '--assume-unchanged', 'base.txt')
    if invalid in ('overlap',):
        # Re-seal this deliberate isolated fixture to reach the disjointness check rather than failing the outer seal.
        from codex_forge.blockers import candidate_snapshot
        seal = o.config.evidence_root / project.name / task.id / 'attempt-03/implementation/blocked-candidate.json'
        doc = json.loads(seal.read_bytes())
        doc['candidate_snapshot_sha256'] = candidate_snapshot(o.config, project, task, proof['candidate_base'])
        seal.write_text(json.dumps(doc))
        proof['candidate_snapshot_sha256'] = doc['candidate_snapshot_sha256']
    if invalid in ('unapproved_path', 'overlap'):
        receipt.write_text(json.dumps(proof))
        digest = hashlib.sha256(receipt.read_bytes()).hexdigest()
    with pytest.raises((ValueError, GitError)):
        o.resolve_blocker(task.id, reason, receipt, digest, prerequisite=True)
    assert o.runner.phases == ['implementation'] * 3
    assert o.db.get_task(task.id).attempts == 3
    assert o.db.blocker_continuation(task.id, 3) is None


def test_partial_import_restart_reconciles_exact_bytes_and_uses_only_one_slot(tmp_path, monkeypatch):
    import codex_forge.prerequisites as module
    f = fixture(tmp_path, monkeypatch)
    o, _, task, _, _, _, reason, receipt, digest = f
    original = module._import_file
    count = 0
    def interrupted(path, payload):
        nonlocal count
        original(path, payload)
        count += 1
        if count == 1:
            raise KeyboardInterrupt
    monkeypatch.setattr(module, '_import_file', interrupted)
    with pytest.raises(KeyboardInterrupt):
        invoke(f)
    assert o.db.get_task(task.id).state == TaskState.BLOCKED
    assert o.db.claim_ready_tasks(1) == []
    assert o.db.blocker_continuation(task.id, 3)['state'] == 'QUEUED'
    monkeypatch.setattr(module, '_import_file', original)
    restarted = Orchestrator(o.config, Path(__file__).parents[1])
    restarted.runner = o.runner
    monkeypatch.setattr(restarted, '_validate', lambda *_, **__: passing_validation(tmp_path, True))
    assert restarted.resolve_blocker(task.id, reason, receipt, digest, prerequisite=True)['task_state'] == 'COMPLETED'
    assert o.runner.phases[-2:] == ['validation-repair-1', 'review']
    with o.db.connect() as connection:
        assert connection.execute('SELECT count(*) FROM prerequisite_events').fetchone()[0] == 1
        assert connection.execute('SELECT count(*) FROM prerequisite_receipts').fetchone()[0] == 1


@pytest.mark.parametrize('modified', ['locked_file', 'receipt', 'failed_seal'])
def test_worker_cannot_modify_locked_prerequisite_even_with_passing_tests(tmp_path, monkeypatch, modified):
    f = fixture(tmp_path, monkeypatch)
    o, project, task, *_ = f
    original = o.runner.run
    def mutate(**kwargs):
        result = original(**kwargs)
        if kwargs['phase'].startswith('validation-repair-'):
            if modified == 'locked_file':
                (Path(kwargs['cwd']) / 'client/reveal.txt').write_text('worker changed prerequisite')
            elif modified == 'receipt':
                f[-2].write_text('receipt changed after model launch')
            else:
                (o.config.evidence_root / project.name / task.id / 'attempt-03/implementation/blocked-candidate.json').write_text('{}')
        return result
    monkeypatch.setattr(o.runner, 'run', mutate)
    assert invoke(f)['task_state'] == 'FAILED'
    assert o.runner.phases[-1] == 'validation-repair-1'
    assert o.db.execution_checkpoint(task.id)['completion_json'] is None
    assert o.workflows.get(project.id)['head_commit'] == o.db.execution_checkpoint(task.id)['base_commit']
    if modified == 'locked_file':
        assert invoke(f)['state'] == 'TERMINAL'
    else:
        assert o.db.blocker_continuation(task.id, 3)['state'] == 'TERMINAL'


def test_public_preflight_and_refusal_envelopes(tmp_path, monkeypatch, capsys):
    f = fixture(tmp_path, monkeypatch)
    o, _, task, _, _, pins, reason, receipt, digest = f
    monkeypatch.setattr('codex_forge.cli.load_config', lambda *_: o.config)
    argv = ['task', 'handoff-prerequisite', task.id, '--reason', reason, '--json']
    preflight_args = ['--dry-run', '--source-task', pins.source_task, '--source-registration', pins.source_registration,
                      '--source-candidate', pins.source_candidate, '--workflow-id', pins.workflow_id,
                      '--gate-id', pins.gate_id, '--gate-candidate', pins.gate_candidate]
    from codex_forge.db import ForgeDB
    def forbidden(*_, **__):
        raise AssertionError('Preflight must not initialize or construct an executing controller.')
    with monkeypatch.context() as read_only:
        read_only.setattr(ForgeDB, 'initialize', forbidden)
        read_only.setattr('codex_forge.cli.Orchestrator', forbidden)
        assert main(argv + preflight_args) == 0
    response = json.loads(capsys.readouterr().out)
    assert response['success'] and response['data']['dry_run']
    assert response['schema_version'] == '1.0'
    assert response['data']['receipt'] == json.loads(receipt.read_bytes())
    assert o.db.blocker_continuation(task.id, 3) is None
    assert main(argv + ['--receipt', str(receipt), '--receipt-sha256', '0' * 64]) == 2
    response = json.loads(capsys.readouterr().out)
    assert not response['success'] and response['error']['code'] == 'INVALID_ARGUMENT'
    assert str(task.worktree_path) not in json.dumps(response)
    monkeypatch.setattr('codex_forge.cli.Orchestrator', lambda *_: o)
    assert main(argv + ['--receipt', str(receipt), '--receipt-sha256', digest]) == 0
    response = json.loads(capsys.readouterr().out)
    assert response['success'] and response['data']['task_state'] == 'COMPLETED'
    provenance = response['data']['prerequisite_handoff']
    assert provenance['source_candidate'] == pins.source_candidate
    assert provenance['gate_candidate'] == pins.gate_candidate
    assert provenance['operator_receipt_sha256'] == digest
    assert provenance['worker_changed_files'] == ['candidate.txt']
    assert {f['path'] for f in provenance['locked_files']} == {'client/reveal.txt', 'contracts/reveal.json'}
    assert 'receipt_path' not in provenance and 'payload_base64' not in json.dumps(provenance)
    phases = list(o.runner.phases)
    assert main(argv + ['--receipt', str(receipt), '--receipt-sha256', digest]) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert repeated['data']['prerequisite_handoff'] == provenance
    assert o.runner.phases == phases


def test_reservation_rechecks_source_and_forbids_receipt_or_authority_reuse(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    o, _, task, source, source_task, _, reason, receipt, digest = f
    with controller_lock(o.config.data_root / 'controller.lock'):
        queue_resolution(o.config, task.id, reason, receipt, digest, prerequisite=True)
    from codex_forge.prerequisites import reserve_handoff
    row = o.db.blocker_continuation(task.id, 3)
    document = json.loads(row['document_json'])
    document['receipt_sha256'] = 'f' * 64  # Another receipt cannot reuse the same completed source authority.
    with o.db.connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        with pytest.raises(ValueError, match='already been used'):
            reserve_handoff(o.config, o.db, connection, o.db.get_project(task.project_id), task, document)
    (source_task.worktree_path / 'client/reveal.txt').write_text('changed after reservation')
    with pytest.raises((ValueError, CandidateStateUnavailable)):
        invoke(f)
    assert o.runner.phases == ['implementation'] * 3
    assert o.db.get_task(task.id).state == TaskState.BLOCKED


@pytest.mark.parametrize('flag', ['--skip-worktree', '--assume-unchanged'])
def test_worker_index_flags_cannot_hide_a_missing_prerequisite_from_commit(tmp_path, monkeypatch, flag):
    f = fixture(tmp_path, monkeypatch)
    o, _, task, *_ = f
    original = o.runner.run
    def hide(**kwargs):
        result = original(**kwargs)
        if kwargs['phase'].startswith('validation-repair-'):
            git(Path(kwargs['cwd']), 'add', 'client/reveal.txt')
            git(Path(kwargs['cwd']), 'update-index', flag, 'client/reveal.txt')
        return result
    monkeypatch.setattr(o.runner, 'run', hide)
    assert invoke(f)['task_state'] == 'FAILED'
    assert o.runner.phases[-1] == 'validation-repair-1'
    assert o.db.execution_checkpoint(task.id)['completion_json'] is None


def test_atomic_import_restarts_after_replace_interruption_and_partial_temporary_bytes(tmp_path, monkeypatch):
    import codex_forge.prerequisites as module
    path = tmp_path / 'file.txt'
    path.write_bytes(b'before')
    payload = b'canonical after\r\n'
    real = module.os.replace
    def stop(*_):
        raise KeyboardInterrupt
    monkeypatch.setattr(module.os, 'replace', stop)
    with pytest.raises(KeyboardInterrupt):
        module._import_file(path, payload)
    assert path.read_bytes() == b'before'
    temporary = path.with_name(path.name + '.forge-prerequisite.tmp')
    assert temporary.read_bytes() == payload
    temporary.write_bytes(payload[:5])  # A crash while flushing only the reserved temporary file.
    monkeypatch.setattr(module.os, 'replace', real)
    module._import_file(path, payload)
    assert path.read_bytes() == payload and not temporary.exists()
    temporary.write_bytes(b'unrelated bytes')
    with pytest.raises(CandidateStateUnavailable):
        module._import_file(path, payload)
    assert path.read_bytes() == payload


@pytest.mark.parametrize('later', ['unclosed_pass', 'missing_acceptance', 'valid_pass'])
def test_only_trusted_prior_reviews_clear_or_carry_findings(tmp_path, monkeypatch, later):
    from test_blocker_continuations import failed_blocker
    from codex_forge.prerequisites import _prior_review
    o, task, _, _ = failed_blocker(tmp_path, monkeypatch)
    project = o.db.get_project(task.project_id)
    if later == 'missing_acceptance':
        task = replace(task, acceptance=['Hide the fitted beta until reveal.'])
    finding = {'severity': 'high', 'file': 'client/reveal.txt', 'line': 1,
               'issue': 'Premature beta visibility.', 'required_fix': 'Hide until reveal and test actual client.'}
    old = {'verdict': 'changes_required', 'summary': 'Unresolved', 'acceptance_criteria': [],
           'binding_requirements': [], 'findings': [finding]}
    root = o.config.evidence_root / project.name / task.id
    for n, final in ((1, old), (2, {**old, 'verdict': 'pass', 'findings': []})):
        directory = root / f'attempt-0{n}' / 'review'
        events = []
        if n == 2 and later == 'unclosed_pass':
            events = [json.dumps({'type': 'item.started', 'item': {'id': 'open', 'type': 'command_execution', 'command': 'unfinished test'}})]
        run_result(directory, 'review', final, events)
        run = o.db.record_run_start(task.id, 'review', 'fake', 'high', directory)
        o.db.record_run_end(run, thread_id=None, exit_code=0, usage={})
    prior = _prior_review(o.config, o.db, project, task)
    if later == 'unclosed_pass':
        assert prior['findings'] == [finding]
    elif later == 'missing_acceptance':
        assert prior and any('Hide the fitted beta until reveal.' in json.dumps(f) for f in prior['findings'])
    else:
        assert prior is None


def test_final_git_tree_modes_are_proven_independently_of_clean_working_bytes(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    o, _, task, *_ = f
    from codex_forge.orchestrator import commit_task as actual_commit
    def corrupt(worktree, task):
        result = actual_commit(worktree, task)
        oid = git(worktree, 'rev-parse', 'HEAD:client/reveal.txt').strip()
        git(worktree, 'update-index', '--cacheinfo', f'100755,{oid},client/reveal.txt')
        git(worktree, 'commit', '--amend', '--no-edit')
        return result
    monkeypatch.setattr('codex_forge.orchestrator.commit_task', corrupt)
    assert invoke(f)['task_state'] == 'FAILED'
    assert o.db.execution_checkpoint(task.id)['completion_json'] is None


def test_prepaid_provenance_refusal_does_not_consume_reserved_slot(tmp_path, monkeypatch):
    import codex_forge.prerequisites as module
    f = fixture(tmp_path, monkeypatch)
    o, _, task, *_ = f
    original = module.prompt_context
    def changed(config, db, task):
        original(config, db, task)
        raise CandidateStateUnavailable('Local prelaunch prerequisite context unavailable.')
    monkeypatch.setattr(module, 'prompt_context', changed)
    with pytest.raises(CandidateStateUnavailable):
        invoke(f)
    assert o.db.blocker_continuation(task.id, 3)['state'] == 'QUEUED'
    assert o.db.get_task(task.id).state == TaskState.BLOCKED
    assert o.runner.phases == ['implementation'] * 3
    monkeypatch.setattr(module, 'prompt_context', original)
    assert invoke(f)['task_state'] == 'COMPLETED'


@pytest.mark.parametrize('name', ['client/AGENTS.md', 'engine/src/CLAUDE.md', 'docs/evidence/SPEC.md',
                                 'engine/src/.gitattributes', 'data/owner.parquet', 'config/settings.json',
                                 'client/../other.cs', 'client/C:alias.cs', 'client/CON'])
def test_unsafe_or_authority_import_paths_are_rejected(name):
    from codex_forge.prerequisites import _path
    with pytest.raises(ValueError):
        _path(name)


def test_started_prerequisite_repair_is_never_replayed_after_restart(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    o, _, task, *_ = f
    original = o.runner.run
    def stopped(**kwargs):
        if kwargs['phase'].startswith('validation-repair-'):
            raise KeyboardInterrupt
        return original(**kwargs)
    monkeypatch.setattr(o.runner, 'run', stopped)
    with pytest.raises(KeyboardInterrupt):
        invoke(f)
    assert o.db.get_task(task.id).state == TaskState.FAILED
    assert o.db.blocker_continuation(task.id, 3)['state'] == 'TERMINAL'
    monkeypatch.setattr(o.runner, 'run', original)
    restarted = Orchestrator(o.config, Path(__file__).parents[1])
    restarted.runner = o.runner
    _, _, _, _, _, _, reason, receipt, digest = f
    assert restarted.resolve_blocker(task.id, reason, receipt, digest, prerequisite=True)['task_state'] == 'FAILED'
    assert o.runner.phases == ['implementation'] * 3
    assert o.db.get_task(task.id).attempts == 3


def test_immutable_journal_publication_never_overwrites_a_competing_file(tmp_path, monkeypatch):
    import codex_forge.prerequisites as module
    path = tmp_path / 'journal.json'
    actual = module.os.link
    def collision(source, target):
        target.write_bytes(b'unrelated retained record')
        return actual(source, target)
    monkeypatch.setattr(module.os, 'link', collision)
    with pytest.raises(CandidateStateUnavailable):
        module._write_immutable(path, b'new journal')
    assert path.read_bytes() == b'unrelated retained record'
    monkeypatch.setattr(module.os, 'link', actual)
    with pytest.raises(CandidateStateUnavailable):
        module._write_immutable(path, b'new journal')


def test_public_preflight_missing_database_creates_neither_file_nor_parent(tmp_path, monkeypatch, capsys):
    from codex_forge.config import ForgeConfig
    root = tmp_path / 'must-stay-absent'
    config = ForgeConfig(config_path=tmp_path / 'config.toml', data_root=root, database_path=root / 'nested/forge.db',
                         workspace_root=root / 'worktrees', repository_root=root / 'repositories',
                         evidence_root=root / 'evidence', inbox_root=root / 'inbox', knowledge_root=root / 'knowledge')
    monkeypatch.setattr('codex_forge.cli.load_config', lambda *_: config)
    result = main(['task', 'handoff-prerequisite', 'missing-target', '--reason', 'Inspect only', '--dry-run', '--json'])
    assert result == 2
    response = json.loads(capsys.readouterr().out)
    assert not response['success'] and response['error']['code'] == 'INVALID_ARGUMENT'
    assert not root.exists()


def test_read_only_inspection_dao_refuses_writes_and_does_not_create_missing_parents(tmp_path):
    import sqlite3
    from codex_forge.db import ForgeDB
    missing = tmp_path / 'missing' / 'forge.db'
    with pytest.raises(ValueError):
        ForgeDB(missing, read_only=True)
    assert not missing.parent.exists()
    existing = ForgeDB(tmp_path / 'existing' / 'forge.db')
    existing.initialize()
    before = existing.path.read_bytes()
    inspection = ForgeDB(existing.path, read_only=True)
    with inspection.connect() as connection:
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            connection.execute('CREATE TABLE forbidden (value TEXT)')
    assert existing.path.read_bytes() == before
