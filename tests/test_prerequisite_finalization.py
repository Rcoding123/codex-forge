"""All workers are local fakes; no paid model or production state is used."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_blocker_continuations import BlockedWorker
from test_final_evidence import completed_result, git, passing_validation
from test_prerequisite_handoffs import fixture, invoke
from test_project_workflows import install
from test_review_infrastructure import run_result

from codex_forge.finalization import execute, preflight
from codex_forge.evidence import evidence_list
from codex_forge.gitops import CandidateStateUnavailable
from codex_forge.models import Project, Task, TaskState
from codex_forge.orchestrator import Orchestrator
from codex_forge.task_submission import task_fingerprint


class NeedsSlider(BlockedWorker):
    def run(self, *, phase, artifact_dir, cwd, prompt, **kwargs):
        if phase == 'review':
            self.phases.append(phase)
            final = {'verdict': 'changes_required', 'summary': 'Generic slider is required',
                     'acceptance_criteria': [], 'binding_requirements': [],
                     'findings': [{'severity': 'medium', 'file': 'client/reveal.txt', 'line': 1,
                                   'issue': 'Required slider absent', 'required_fix': 'Replace generic prerequisite'}]}
        elif phase == 'review-repair-1':
            self.phases.append(phase)
            if getattr(self, 'worker_inputs', None):
                (Path(cwd) / 'candidate.txt').unlink(missing_ok=True)
                for name, data in self.worker_inputs.items():
                    (Path(cwd) / name).parent.mkdir(parents=True, exist_ok=True)
                    (Path(cwd) / name).write_bytes(data)
            else:
                (Path(cwd) / 'candidate.txt').write_bytes(b'latest prospective worker bytes\n')
            final = {**completed_result(['candidate.txt'], 'blocked on locked generic slider'),
                     'status': 'blocked', 'blocking_reason': 'Locked prerequisite must supply slider'}
        else:
            return super().run(phase=phase, artifact_dir=artifact_dir, cwd=cwd, prompt=prompt, **kwargs)
        return run_result(Path(artifact_dir), phase, final, [])


class SliderSource(BlockedWorker):
    def run(self, *, phase, artifact_dir, cwd, prompt, **kwargs):
        if phase != 'implementation':
            return super().run(phase=phase, artifact_dir=artifact_dir, cwd=cwd, prompt=prompt, **kwargs)
        self.phases.append(phase)
        root = Path(cwd)
        if (root / 'client/slider.txt').exists():
            files = {'docs/evidence/slider-ready/ready.md': b'exact source readiness\n'}
        else:
            files = {'client/reveal.txt': b'approved prerequisite with slider\n',
                     'contracts/reveal.json': b'approved prerequisite\n', 'client/slider.txt': b'bounded slider\n'}
        for name, data in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_bytes(data)
        return run_result(Path(artifact_dir), phase, completed_result(list(files), 'new independently scoped prerequisite'), [])


def successor_fixture(tmp_path, monkeypatch, *, worker_inputs=None):
    scope = '\n'.join(['candidate.txt', *(worker_inputs or {})])
    f = fixture(tmp_path, monkeypatch, target_scope=scope)
    o, project, task, *_ = f
    o.runner = NeedsSlider()
    o.runner.worker_inputs = worker_inputs
    assert invoke(f)['task_state'] == 'FAILED'
    task = o.db.get_task(task.id)
    parent = o.db.blocker_continuation(task.id, 3)
    assert parent['state'] == 'TERMINAL'
    base = o.db.execution_checkpoint(task.id)['base_commit']
    repo = tmp_path / 'slider-source'
    git(tmp_path, 'clone', str(project.repo_path), str(repo))
    git(repo, 'config', 'user.name', 'Finalization fixture')
    git(repo, 'config', 'user.email', 'fixture@example.invalid')
    command = "python -c \"print('exact slider validation')\""
    source = Project('slider-source', 'slider-source', repo, validation_commands=[command], auto_fetch=False)
    o.db.add_project(source)
    source = o.db.get_project(source.id)
    source_task = Task('slider-task', source.id, 'Generic slider', 'Implement separately approved generic slider', [], [],
                       TaskState.READY, scope='client/reveal.txt\ncontracts/reveal.json\nclient/slider.txt', admission_base_commit=base)
    o.db.add_task(source_task)
    o.runner = SliderSource()
    monkeypatch.setattr(o, '_validate', Orchestrator._validate.__get__(o))
    claimed = o.db.claim_ready_tasks(1)
    assert [t.id for t in claimed] == [source_task.id]
    o._execute_task_body(claimed[0])
    source_task = o.db.get_task(source_task.id)
    assert source_task.state == TaskState.COMPLETED
    gate = Task('slider-gate', source.id, 'Slider readiness', 'Retain source readiness', [], [], TaskState.BACKLOG,
                scope='docs/evidence/slider-ready/**', dependencies=[source_task.id])
    o.db.add_task(gate)
    plan = install(o, source, {'branch': 'forge-project/slider-source', 'base_commit': source_task.result['commit'],
                              'phases': [{'name': 'Ready', 'tasks': [gate.id], 'acceptance_commands': [command]}]})
    for _ in range(4):
        o.run_once()
    plan = o.workflows.get(source.id)
    assert plan['state'] == 'COMPLETED' and plan['gates'][-1]['status'] == 'PASSED'
    pins = SimpleNamespace(parent_continuation=json.loads(parent['document_json'])['id'], source_task=source_task.id,
                           source_registration=source.repository_registration_id, source_candidate=source_task.result['commit'],
                           workflow_id=plan['id'], gate_id=plan['gates'][-1]['id'], gate_candidate=plan['head_commit'])
    o.runner = BlockedWorker()
    monkeypatch.setattr(o, '_validate', lambda *_, **__: passing_validation(tmp_path, True))
    reason, attestation = 'Authorize new generic slider prerequisite', 'Current prospective worker bytes; not a historical sealed candidate.'
    response = preflight(o.config, task.id, reason, attestation, pins)
    receipt = tmp_path / 'prospective-receipt.json'
    receipt.write_bytes(response['receipt_json'].encode('utf-8'))
    return o, project, task, pins, parent, reason, receipt, response['receipt_sha256']


def test_finalization_zero_repairs_one_review_preserves_parent_and_original_worker(tmp_path, monkeypatch):
    o, project, task, pins, parent, reason, receipt, digest = successor_fixture(tmp_path, monkeypatch)
    worker = (task.worktree_path / 'candidate.txt').read_bytes()
    admission = task_fingerprint(task)
    plan = o.workflows.get(project.id)
    result = execute(o, task.id, reason, receipt, digest)
    assert result['state'] == 'COMPLETED' and result['task_state'] == 'COMPLETED'
    assert o.runner.phases == ['review']
    current = o.db.get_task(task.id)
    assert current.attempts == current.max_attempts == 3 and task_fingerprint(current) == admission
    assert (task.worktree_path / 'candidate.txt').read_bytes() == worker
    assert o.db.blocker_continuation(task.id, 3) == parent
    assert current.result['base_commit'] == o.db.execution_checkpoint(task.id)['base_commit']
    assert current.result['prerequisite_finalization']['source']['source_candidate'] == pins.source_candidate
    assert o.workflows.get(project.id)['specifications'] == plan['specifications']
    assert o.workflows.get(project.id)['gates'][-1]['status'] == 'PASSED'
    assert execute(o, task.id, reason, receipt, digest) == result
    assert o.runner.phases == ['review']


@pytest.mark.parametrize('drift', ['worker', 'receipt', 'hidden'])
def test_stale_prospective_input_rejected_without_review(tmp_path, monkeypatch, drift):
    o, _, task, _, _, reason, receipt, digest = successor_fixture(tmp_path, monkeypatch)
    if drift == 'worker':
        (task.worktree_path / 'candidate.txt').write_bytes(b'external mutation\n')
    elif drift == 'receipt':
        digest = '0' * 64
    else:
        git(task.worktree_path, 'update-index', '--assume-unchanged', 'base.txt')
    with pytest.raises(Exception):
        execute(o, task.id, reason, receipt, digest)
    assert o.runner.phases == []
    assert o.db.get_task(task.id).attempts == 3


def test_validation_failure_never_runs_review_or_repairs(tmp_path, monkeypatch):
    o, _, task, _, parent, reason, receipt, digest = successor_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(o, '_validate', lambda *_, **__: passing_validation(tmp_path, False))
    result = execute(o, task.id, reason, receipt, digest)
    assert result['state'] == 'FAILED' and o.runner.phases == []
    assert o.db.blocker_continuation(task.id, 3) == parent
    assert execute(o, task.id, reason, receipt, digest) == result


def test_changed_review_ends_without_repair(tmp_path, monkeypatch):
    o, _, task, _, _, reason, receipt, digest = successor_fixture(tmp_path, monkeypatch)
    o.runner = NeedsSlider()
    result = execute(o, task.id, reason, receipt, digest)
    assert result['state'] == 'FAILED' and o.runner.phases == ['review']
    assert o.db.get_task(task.id).attempts == 3


@pytest.mark.parametrize('boundary', ['partial_import', 'trusted_review', 'staging', 'commit', 'integration'])
def test_restart_at_durable_boundaries_never_replays_review(tmp_path, monkeypatch, boundary):
    import codex_forge.finalization as module
    import codex_forge.orchestrator as controller

    o, project, task, _, parent, reason, receipt, digest = successor_fixture(tmp_path, monkeypatch)
    worker_bytes = (task.worktree_path / 'candidate.txt').read_bytes()
    with monkeypatch.context() as fault:
        if boundary == 'partial_import':
            original = module._import_file
            count = 0

            def import_file(path, data):
                nonlocal count
                original(path, data)
                count += 1
                if count == 1:
                    raise KeyboardInterrupt('isolated partial replacement')
            fault.setattr(module, '_import_file', import_file)
        elif boundary == 'trusted_review':
            original = module._save

            def save(db, current_task, state, checkpoint):
                if state == 'REVIEW_PASSED':
                    raise KeyboardInterrupt('trusted immutable review exists, DB transition pending')
                return original(db, current_task, state, checkpoint)
            fault.setattr(module, '_save', save)
        elif boundary == 'staging':
            original = module._git

            def staged(repo, *args, **kwargs):
                result = original(repo, *args, **kwargs)
                if args == ('add', '-A'):
                    raise KeyboardInterrupt('isolated stage completed before tree-intent seal')
                return result
            fault.setattr(module, '_git', staged)
        elif boundary == 'commit':
            original = controller.commit_task

            def committed(*args):
                original(*args)
                raise KeyboardInterrupt('exact journaled commit exists before completion checkpoint')
            fault.setattr(controller, 'commit_task', committed)
        else:
            original = o.workflows.integrate

            def integrated(*args):
                original(*args)
                raise KeyboardInterrupt('integration persisted before completed task update')
            fault.setattr(o.workflows, 'integrate', integrated)
        with pytest.raises(KeyboardInterrupt):
            execute(o, task.id, reason, receipt, digest)
    assert o.db.get_task(task.id).attempts == 3
    assert o.db.claim_ready_tasks(8) == []  # Also models an old daemon after the singleton lock releases.
    assert o.db.blocker_continuation(task.id, 3) == parent
    fresh = Orchestrator(o.config, Path(__file__).parents[1])
    fresh.runner = o.runner
    monkeypatch.setattr(fresh, '_validate', lambda *_, **__: passing_validation(tmp_path, True))
    outcome = execute(fresh, task.id, reason, receipt, digest)
    assert outcome['state'] == 'COMPLETED' and fresh.runner.phases == ['review']
    assert (task.worktree_path / 'candidate.txt').read_bytes() == worker_bytes
    assert fresh.workflows.get(project.id)['gates'][-1]['status'] == 'PASSED'
    listed = evidence_list(o.config, task.id, schema_version='1.4')
    assert any(a['artifact_kind'] == 'final_result' and '-f-' in a['display_name'] for a in listed['artifacts'])
    assert not any(a['display_name'].endswith(('finalization.json', 'operator-receipt.raw', 'trusted-review.json')) for a in listed['artifacts'])


def test_ambiguous_started_review_is_consumed_and_never_relaunched(tmp_path, monkeypatch):
    o, _, task, _, parent, reason, receipt, digest = successor_fixture(tmp_path, monkeypatch)
    calls = []

    def interrupted(**kwargs):
        calls.append(kwargs['phase'])
        raise KeyboardInterrupt('worker launch outcome unknown')
    monkeypatch.setattr(o.runner, 'run', interrupted)
    with pytest.raises(KeyboardInterrupt):
        execute(o, task.id, reason, receipt, digest)
    result = execute(o, task.id, reason, receipt, digest)
    assert result['state'] == 'FAILED' and result['review_started'] is True
    assert calls == ['review'] and o.db.blocker_continuation(task.id, 3) == parent
    assert execute(o, task.id, reason, receipt, digest) == result


def test_review_cannot_change_worker_or_replacement_bytes(tmp_path, monkeypatch):
    o, _, task, _, _, reason, receipt, digest = successor_fixture(tmp_path, monkeypatch)
    original = o.runner.run

    def changed(**kwargs):
        result = original(**kwargs)
        (task.worktree_path / 'client/slider.txt').write_bytes(b'unreviewed replacement mutation\n')
        return result
    monkeypatch.setattr(o.runner, 'run', changed)
    with pytest.raises(CandidateStateUnavailable):
        execute(o, task.id, reason, receipt, digest)
    assert o.runner.phases == ['review']
    assert o.db.get_task(task.id).attempts == 3


def test_public_preflight_terminal_projection_and_no_initialization_on_replay(tmp_path, monkeypatch, capsys):
    from codex_forge.cli import main
    from codex_forge.db import ForgeDB

    o, _, task, pins, _, reason, receipt, digest = successor_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr('codex_forge.cli.load_config', lambda *_: o.config)
    argv = ['task', 'finalize-prerequisite', task.id, '--reason', reason, '--json']
    proof = json.loads(receipt.read_bytes())
    dry = ['--dry-run', '--attestation', proof['prospective_attestation']['text'],
           '--parent-continuation', pins.parent_continuation, '--source-task', pins.source_task,
           '--source-registration', pins.source_registration, '--source-candidate', pins.source_candidate,
           '--workflow-id', pins.workflow_id, '--gate-id', pins.gate_id, '--gate-candidate', pins.gate_candidate]

    def forbidden(*_, **__):
        raise AssertionError('Read-only public operation attempted controller/schema initialization')
    with monkeypatch.context() as read_only:
        read_only.setattr(ForgeDB, 'initialize', forbidden)
        read_only.setattr('codex_forge.cli.Orchestrator', forbidden)
        assert main(argv + dry) == 0
    response = json.loads(capsys.readouterr().out)
    assert response['schema_version'] == '1.0' and response['data']['receipt_json'].encode() == receipt.read_bytes()
    assert response['data']['receipt_sha256'] == digest and o.db.prerequisite_finalization(task.id) is None
    assert main(argv + ['--receipt', str(receipt), '--receipt-sha256', '0' * 64]) == 2
    assert json.loads(capsys.readouterr().out)['error']['code'] == 'INVALID_ARGUMENT'
    result = execute(o, task.id, reason, receipt, digest)
    with monkeypatch.context() as read_only:
        read_only.setattr(ForgeDB, 'initialize', forbidden)
        read_only.setattr('codex_forge.cli.Orchestrator', forbidden)
        assert main(argv + ['--receipt', str(receipt), '--receipt-sha256', digest]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown['data'] == result and result['attribution']['parent_continuation_id'] == pins.parent_continuation
    assert result['prospective_attestation']['kind'] == 'current_prospective_input_not_historical_seal'
    assert o.runner.phases == ['review']
    before = o.db.prerequisite_finalization(task.id)
    assert before is not None
    status, _ = o.db.delete_task(task.id)
    assert status == 'deleted' and o.db.prerequisite_finalization(task.id) is None
    listed = evidence_list(o.config, task.id, schema_version='1.4')
    assert any(a['artifact_kind'] == 'final_result' and '-f-' in a['display_name'] for a in listed['artifacts'])
    with o.db.connect() as connection:
        assert connection.execute('SELECT 1 FROM prerequisite_receipts WHERE receipt_sha256=?', (digest,)).fetchone()


def test_missing_database_preflight_cannot_create_parent(tmp_path, monkeypatch, capsys):
    from codex_forge.cli import main
    from codex_forge.config import ForgeConfig

    root = tmp_path / 'missing-controller'
    config = ForgeConfig(config_path=tmp_path / 'forge.toml', data_root=root, database_path=root / 'nested/forge.db',
                         workspace_root=root / 'worktrees', repository_root=root / 'repositories',
                         evidence_root=root / 'evidence', inbox_root=root / 'inbox', knowledge_root=root / 'knowledge')
    monkeypatch.setattr('codex_forge.cli.load_config', lambda *_: config)
    assert main(['task', 'finalize-prerequisite', 'missing', '--reason', 'Inspect only', '--dry-run', '--json']) == 2
    assert json.loads(capsys.readouterr().out)['error']['code'] == 'INVALID_ARGUMENT'
    assert not root.exists()


def test_review_usage_stays_on_original_task_and_finite_budget_never_resets(tmp_path, monkeypatch):
    from codex_forge.orchestrator import TaskUsageBudgetExceeded
    from codex_forge.usage import budget_used_tokens

    o, _, task, pins, parent, reason, receipt, _ = successor_fixture(tmp_path, monkeypatch)
    usage = o.db.task_token_usage(task.id)
    used = budget_used_tokens(usage)
    o.config = replace(o.config, max_task_tokens=used + 69)
    prior_receipt = json.loads(receipt.read_bytes())
    response = preflight(o.config, task.id, reason, prior_receipt['prospective_attestation']['text'], pins)
    receipt.write_bytes(response['receipt_json'].encode())
    with pytest.raises(TaskUsageBudgetExceeded):
        execute(o, task.id, reason, receipt, response['receipt_sha256'])
    assert o.runner.phases == ['review']
    assert budget_used_tokens(o.db.task_token_usage(task.id)) == used + 70
    row = o.db.prerequisite_finalization(task.id)
    assert row['state'] == 'FAILED' and json.loads(row['checkpoint_json'])['review_started'] is True
    assert o.db.get_task(task.id).attempts == 3 and o.db.blocker_continuation(task.id, 3) == parent
    assert o.config.max_repair_cycles == 2 and o.config.max_task_tokens == used + 69


def reconciliation_fixture(tmp_path, monkeypatch):
    from codex_forge.blockers import _hash
    from codex_forge.prerequisites import _blob, _tree
    from test_metadata_selections import BEFORE, AFTER, SPAN

    inputs = {'engine/src/demo.py': BEFORE, 'content/demo.md': b'Lesson\nObsolete pending status.\nMove the control.\n',
              'engine/tests/test_demo.py': b'assert 6 * 7 == 42\n',
              'engine/tests/demo_evidence.md': b'Dated BLOCKED evidence retained exactly.\n'}
    f = successor_fixture(tmp_path, monkeypatch, worker_inputs=inputs)
    o, project, task, capability_pins, parent, reason, _, _ = f
    base = o.db.execution_checkpoint(task.id)['base_commit']
    capability_task = o.db.get_task(capability_pins.source_task)
    capability = o.db.get_project(capability_task.project_id)
    manifest_path = 'docs/evidence/transition/selection.json'
    md_after = inputs['content/demo.md'].replace(b'Obsolete pending status.\n', b'')
    policy = {'version': '1', 'capability_candidate': capability_pins.source_candidate,
              'prospective_worker_inputs': {name: _hash(data) for name, data in inputs.items()},
              'selections': [
                  {'kind': 'python_literal_list_removal', 'path': 'engine/src/demo.py',
                   'before_sha256': _hash(BEFORE), 'after_sha256': _hash(AFTER), 'call': 'Metadata',
                   'keyword': 'warnings', 'removed_literal': 'obsolete shell pending', 'old_span': SPAN, 'new_span': ''},
                  {'kind': 'utf8_text_replacements', 'path': 'content/demo.md',
                   'before_sha256': _hash(inputs['content/demo.md']), 'after_sha256': _hash(md_after),
                   'replacements': [{'old': 'Obsolete pending status.\n', 'new': ''}]}]}
    files = {name: _blob(capability.repo_path, entry['oid']) for name, entry in
             _tree(capability.repo_path, capability_pins.source_candidate).items()
             if entry != _tree(capability.repo_path, base).get(name)}
    files.update(inputs)
    files.update({'engine/src/demo.py': AFTER, 'content/demo.md': md_after,
                  manifest_path: json.dumps(policy, indent=2).encode()})
    repo = tmp_path / 'reconciliation'
    git(tmp_path, 'clone', str(project.repo_path), str(repo))
    git(repo, 'config', 'user.name', 'Reconciliation fixture')
    git(repo, 'config', 'user.email', 'fixture@example.invalid')
    command = "python -c \"print('exact reconciliation validation')\""
    source = Project('reconciliation', 'reconciliation', repo, validation_commands=[command], auto_fetch=False)
    o.db.add_project(source)
    source = o.db.get_project(source.id)
    source_task = Task('reconciliation-task', source.id, 'Capability transition metadata', 'Only typed exact metadata deletions',
                       [], [], TaskState.READY, scope='\n'.join(files), admission_base_commit=base)
    o.db.add_task(source_task)

    class Reconciler(BlockedWorker):
        def run(self, *, phase, artifact_dir, cwd, **kwargs):
            if phase != 'implementation':
                return super().run(phase=phase, artifact_dir=artifact_dir, cwd=cwd, **kwargs)
            self.phases.append(phase)
            payloads = ({'docs/evidence/transition-ready/ready.md': b'Reconciliation ready\n'}
                        if (Path(cwd) / manifest_path).exists() else files)
            for name, data in payloads.items():
                path = Path(cwd) / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            return run_result(Path(artifact_dir), phase, completed_result(list(payloads), 'separately budgeted transition'), [])

    o.runner = Reconciler()
    monkeypatch.setattr(o, '_validate', Orchestrator._validate.__get__(o))
    o._execute_task_body(o.db.claim_ready_tasks(1)[0])
    source_task = o.db.get_task(source_task.id)
    assert source_task.state == TaskState.COMPLETED
    gate = Task('reconciliation-gate', source.id, 'Transition gate', 'Verify exact reconciliation candidate', [], [],
                TaskState.BACKLOG, scope='docs/evidence/transition-ready/**', dependencies=[source_task.id])
    o.db.add_task(gate)
    install(o, source, {'branch': 'forge-project/reconciliation', 'base_commit': source_task.result['commit'],
                       'phases': [{'name': 'Ready', 'tasks': [gate.id], 'acceptance_commands': [command]}]})
    for _ in range(4):
        o.run_once()
    plan = o.workflows.get(source.id)
    assert plan['state'] == 'COMPLETED' and plan['gates'][-1]['status'] == 'PASSED'
    pins = SimpleNamespace(parent_continuation=capability_pins.parent_continuation,
                           source_task=source_task.id, source_registration=source.repository_registration_id,
                           source_candidate=source_task.result['commit'], workflow_id=plan['id'],
                           gate_id=plan['gates'][-1]['id'], gate_candidate=plan['head_commit'], selection_manifest=manifest_path,
                           **{'capability_' + name: getattr(capability_pins, name) for name in
                              ('source_task', 'source_registration', 'source_candidate', 'workflow_id', 'gate_id', 'gate_candidate')})
    response = preflight(o.config, task.id, reason, 'Current prospective worker inputs, selected exact separately gated metadata.', pins)
    receipt = tmp_path / 'selection-receipt.json'
    receipt.write_bytes(response['receipt_json'].encode())
    o.runner = BlockedWorker()
    monkeypatch.setattr(o, '_validate', lambda *_, **__: passing_validation(tmp_path, True))
    return o, project, task, pins, parent, reason, receipt, response['receipt_sha256'], inputs, policy


def test_gated_typed_reconciliation_uses_one_review_and_retains_nonselected_history(tmp_path, monkeypatch):
    o, project, task, pins, parent, reason, receipt, digest, inputs, policy = reconciliation_fixture(tmp_path, monkeypatch)
    result = execute(o, task.id, reason, receipt, digest)
    assert result['state'] == 'COMPLETED' and o.runner.phases == ['review']
    current = o.db.get_task(task.id)
    assert current.attempts == current.max_attempts == 3 and o.db.blocker_continuation(task.id, 3) == parent
    assert (task.worktree_path / 'engine/tests/test_demo.py').read_bytes() == inputs['engine/tests/test_demo.py']
    assert (task.worktree_path / 'engine/tests/demo_evidence.md').read_bytes() == inputs['engine/tests/demo_evidence.md']
    assert result['selection']['policy'] == policy
    attribution = result['attribution']
    assert attribution['capability']['source_candidate'] == pins.capability_source_candidate
    assert attribution['source']['source_candidate'] == pins.source_candidate
    assert sorted(attribution['worker_changed_files']) == sorted(inputs)
    root = Path(current.result['patch']).parent
    assert b'Obsolete pending status.' in (root / 'reconciliation.patch').read_bytes()
    assert b'engine/src/demo.py' not in (root / 'prerequisite.patch').read_bytes()
    assert b'engine/src/demo.py' in (root / 'worker-changes.patch').read_bytes()
    assert o.workflows.get(project.id)['gates'][-1]['status'] == 'PASSED'
    assert execute(o, task.id, reason, receipt, digest) == result


def test_completion_before_successor_seal_recovers_after_normal_cursor_advances(tmp_path, monkeypatch):
    import codex_forge.finalization as module

    o, project, task, _, parent, reason, receipt, digest = successor_fixture(tmp_path, monkeypatch)
    save = module._save
    with monkeypatch.context() as fault:
        def interrupted(db, current, state, checkpoint):
            if state == 'COMPLETED':
                raise KeyboardInterrupt('task completion durable before successor journal seal')
            return save(db, current, state, checkpoint)
        fault.setattr(module, '_save', interrupted)
        with pytest.raises(KeyboardInterrupt):
            execute(o, task.id, reason, receipt, digest)
    assert o.db.get_task(task.id).state == TaskState.COMPLETED
    o.workflows.advance_all()
    o.workflows.advance_all()
    assert o.workflows.get(project.id)['phase_index'] == 1
    fresh = Orchestrator(o.config, Path(__file__).parents[1])
    fresh.runner = o.runner
    result = execute(fresh, task.id, reason, receipt, digest)
    assert result['state'] == 'COMPLETED' and o.runner.phases == ['review']
    assert o.db.blocker_continuation(task.id, 3) == parent
    assert execute(fresh, task.id, reason, receipt, digest) == result


def test_typed_authority_drift_refuses_before_reservation_and_review(tmp_path, monkeypatch):
    from codex_forge.prerequisites import _blob, _tree

    o, _, task, pins, parent, reason, receipt, digest, inputs, _ = reconciliation_fixture(tmp_path, monkeypatch)
    source_task = o.db.get_task(pins.source_task)
    source = o.db.get_project(source_task.project_id)
    name = 'engine/src/demo.py'
    source_bytes = _blob(source.repo_path, _tree(source.repo_path, pins.source_candidate)[name]['oid'])
    for path, before in ((source_task.worktree_path / name, source_bytes),
                         (task.worktree_path / 'engine/tests/demo_evidence.md', inputs['engine/tests/demo_evidence.md'])):
        path.write_bytes(before + b'external mutation\n')
        with pytest.raises((ValueError, CandidateStateUnavailable)):
            execute(o, task.id, reason, receipt, digest)
        assert o.db.prerequisite_finalization(task.id) is None and o.runner.phases == []
        path.write_bytes(before)
    with pytest.raises(ValueError):
        execute(o, task.id, reason, receipt, '0' * 64)
    assert o.db.prerequisite_finalization(task.id) is None and o.db.blocker_continuation(task.id, 3) == parent


def test_active_import_and_validation_cannot_be_deleted_concurrently(tmp_path, monkeypatch):
    import codex_forge.finalization as module

    o, _, task, _, parent, reason, receipt, digest = successor_fixture(tmp_path, monkeypatch)
    observed = []
    imported = module._import_file

    def guarded_import(path, data):
        status, current = o.db.delete_task(task.id)
        observed.append(('import', status, current.state))
        assert status == 'active' and current.state == TaskState.VALIDATING
        imported(path, data)

    def guarded_validation(*args, **kwargs):
        status, current = o.db.delete_task(task.id)
        observed.append(('validation', status, current.state))
        assert status == 'active' and current.state in (TaskState.VALIDATING, TaskState.REVIEWING)
        return passing_validation(tmp_path, True)

    monkeypatch.setattr(module, '_import_file', guarded_import)
    monkeypatch.setattr(o, '_validate', guarded_validation)
    result = execute(o, task.id, reason, receipt, digest)
    assert result['state'] == 'COMPLETED' and o.runner.phases == ['review']
    assert {kind for kind, *_ in observed} == {'import', 'validation'}
    assert o.db.blocker_continuation(task.id, 3) == parent
    assert o.db.delete_task(task.id)[0] == 'deleted'
    assert o.db.prerequisite_finalization(task.id) is None
    assert any(a['artifact_kind'] == 'final_result' for a in evidence_list(o.config, task.id)['artifacts'])


def test_untracked_ignore_and_clean_filter_refuse_before_single_review(tmp_path, monkeypatch):
    from test_finalization_git_policy import install_filter

    o, _, task, _, parent, reason, receipt, digest = successor_fixture(tmp_path, monkeypatch)
    ignore = task.worktree_path / '.gitignore'
    hidden = task.worktree_path / 'hidden.py'
    ignore.write_bytes(b'.gitignore\nhidden.py\n')
    hidden.write_bytes(b'unreviewed = True\n')
    with pytest.raises(CandidateStateUnavailable):
        execute(o, task.id, reason, receipt, digest)
    assert o.db.prerequisite_finalization(task.id) is None and o.runner.phases == []
    ignore.unlink()
    hidden.unlink()
    install_filter(task.worktree_path)
    with pytest.raises(CandidateStateUnavailable):
        execute(o, task.id, reason, receipt, digest)
    assert o.db.prerequisite_finalization(task.id) is None and o.runner.phases == []
    assert o.db.blocker_continuation(task.id, 3) == parent


def test_git_conversion_introduced_during_review_never_completes_or_replays(tmp_path, monkeypatch):
    from test_finalization_git_policy import install_filter

    o, _, task, _, parent, reason, receipt, digest = successor_fixture(tmp_path, monkeypatch)
    run = o.runner.run

    def converted(**kwargs):
        result = run(**kwargs)
        install_filter(task.worktree_path)
        return result
    monkeypatch.setattr(o.runner, 'run', converted)
    with pytest.raises(CandidateStateUnavailable):
        execute(o, task.id, reason, receipt, digest)
    assert o.db.get_task(task.id).state != TaskState.COMPLETED and o.runner.phases == ['review']
    info = Path(git(task.worktree_path, 'rev-parse', '--git-path', 'info/attributes').strip())
    if not info.is_absolute():
        info = task.worktree_path / info
    info.unlink()
    result = execute(o, task.id, reason, receipt, digest)
    assert result['state'] == 'FAILED' and result['review_started'] is True
    assert o.runner.phases == ['review'] and o.db.blocker_continuation(task.id, 3) == parent
