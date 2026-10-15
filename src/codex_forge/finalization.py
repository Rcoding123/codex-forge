"""One-shot prospective prerequisite finalization; never another implementation/repair."""
from __future__ import annotations

import base64
import difflib
import fnmatch
import hashlib
import json
import os
import re
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from .blockers import _hash, _plain_bytes, _prove_plan, candidate_state
from .db import ACTIVE_TASK_STATES, ForgeDB
from .gitops import CandidateStateUnavailable, _git, explicit_candidate_commit, require_admission_base_commit
from .models import TaskState
from .prerequisites import (
    _blob, _git_bytes, _import_file, _path, _prior_review, _prove_source, _safe_target, _source,
    _tree, _write_immutable,
)
from .processes import run_supervised
from .prompts import review_prompt
from .runner import detect_review_infrastructure_block
from .schema_resources import load_schema
from .task_submission import task_fingerprint
from .utils import controller_lock, flatten_unique, utc_now
from .workflows import _digest


def _raw(value):
    # DB accounting canonicalizes nested mappings; seals must not depend on insertion order.
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode('utf-8')


def _limits(config):
    return {key: getattr(config, key) for key in (
        'max_repair_cycles', 'max_task_tokens', 'max_identical_failures', 'task_timeout_minutes',
    )}


def _visible(repo):
    # Only committed ignore rules may exempt an input. An unknown .gitignore can hide
    # itself, so comparing two listings which both load working ignore files is insufficient.
    tree = _tree(repo, 'HEAD')
    tracked = {n: entry for n, entry in tree.items() if Path(n).name.casefold() == '.gitignore'}
    index_ignores = {n for n in _git(repo, 'ls-files', '--cached', '-z', '--',
        ':(icase).gitignore', ':(icase)**/.gitignore').stdout.split('\0') if n}
    if index_ignores != set(tracked) or any(Path(n).name != '.gitignore' for n in tracked):
        raise CandidateStateUnavailable('Ignore policy was added/deleted or has a portable case alias.')
    policies = {}
    for name, entry in tracked.items():
        if entry['mode'] != '100644' or entry['kind'] != 'blob':
            raise CandidateStateUnavailable('Versioned ignore policy is not a regular plain file.')
        raw = _plain_bytes(_safe_target(repo, name))
        committed = _blob(repo, entry['oid'])
        if raw.replace(b'\r\n', b'\n') != committed.replace(b'\r\n', b'\n'):
            raise CandidateStateUnavailable('Working ignore policy differs from its committed authority.')
        policies[name] = committed
    unknown = [n for n in _git(repo, 'ls-files', '--others', '-z', '--', ':(icase).gitignore', ':(icase)**/.gitignore').stdout.split('\0') if n]
    if unknown:
        parents = sorted({str(Path(n).parent).replace('\\', '/') + '/' for n in unknown})
        if './' in parents or len(parents) > 256 or sum(len(n.encode('utf-8')) for n in parents) > 24000:
            raise CandidateStateUnavailable('Untracked ignore policy can hide prospective inputs.')
        with tempfile.TemporaryDirectory(prefix='forge-ignore-authority-') as folder:
            isolated = Path(folder)
            environment = {k: v for k, v in os.environ.items() if not k.upper().startswith('GIT_')}
            environment.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
            initialized = run_supervised(['git', 'init', '--template=', '--quiet'], cwd=isolated,
                                         env=environment, timeout=300)
            if initialized.returncode:
                raise CandidateStateUnavailable('Isolated versioned ignore authority could not initialize.')
            for name, raw in policies.items():
                path = isolated / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
            for parent in parents:
                _safe_target(isolated, parent.rstrip('/')).mkdir(parents=True, exist_ok=True)
            # No source/global/info excludes enter this fresh context. Only entire
            # versioned-ignored ancestors may contain unknown generated ignore files.
            ignored = run_supervised(['git', '-c', 'core.excludesFile=' + os.devnull,
                '-c', 'core.worktree=' + str(isolated), '-c', 'core.ignoreCase=false',
                'check-ignore', '--no-index', '--stdin', '-z'],
                cwd=isolated, env=environment, timeout=300, input=''.join(name + '\0' for name in parents))
            if ignored.returncode not in (0, 1) or set(ignored.stdout.split('\0')) - {''} != set(parents):
                raise CandidateStateUnavailable('Untracked ignore policy can affect visible candidate inputs.')
    canonical = _git(repo, 'ls-files', '-z', '--cached', '--others', '--exclude-per-directory=.gitignore').stdout
    ordinary = _git(repo, 'ls-files', '-z', '--cached', '--others', '--exclude-standard').stdout
    if set(canonical.split('\0')) != set(ordinary.split('\0')):
        raise CandidateStateUnavailable('Local/global Git excludes hide candidate inputs.')
    return _raw_git_inputs(repo, sorted(set(ordinary.split('\0')) - {''}), allow_crlf=True)


def _raw_git_inputs(repo, names, *, allow_crlf=False, approved=None):
    # Git must commit exactly the bytes validation/review see. Batch conversion stays
    # kill-on-close supervised, including arbitrary descendants of local clean filters.
    raw_oids, normalized, live = {}, {}, []
    for name in names:
        path = _safe_target(repo, name)
        if path.exists():
            raw = _plain_bytes(path, maximum=256 * 1024 * 1024)
            raw_oids[name] = hashlib.sha1(b'blob ' + str(len(raw)).encode('ascii') + b'\0' + raw).hexdigest()
            lf = raw.replace(b'\r\n', b'\n')
            normalized[name] = hashlib.sha1(b'blob ' + str(len(lf)).encode('ascii') + b'\0' + lf).hexdigest()
            live.append(name)
    if live:
        attributes = run_supervised(['git', 'check-attr', '-z', '--stdin', 'filter'], cwd=repo, timeout=300,
                                    input=''.join(n + '\0' for n in live))
        values = attributes.stdout.split('\0')
        if (attributes.returncode or len(values) != 3 * len(live) + 1
                or any(values[i:i + 2] != [name, 'filter'] or values[i + 2] not in ('unspecified', 'unset')
                       for i, name in zip(range(0, 3 * len(live), 3), live))):
            raise CandidateStateUnavailable('External Git filter conversion is not approved candidate authority.')
        result = run_supervised(['git', 'hash-object', '--stdin-paths'], cwd=repo, timeout=300,
                               input=''.join(json.dumps(n, ensure_ascii=False) + '\n' for n in live))
        observed = result.stdout.splitlines()
        if (result.returncode or len(observed) != len(live)
                or (approved is not None and any(oid != approved.get(name) for name, oid in zip(live, observed)))
                or any(oid != raw_oids[name] and (not allow_crlf or oid != normalized[name])
                       for name, oid in zip(live, observed))):
            raise CandidateStateUnavailable('Git conversion differs from approved raw working input bytes.')
    return raw_oids


def _exact_raw_tree(repo, tree, document):
    raw = _visible(repo)
    entries = _tree(repo, tree)
    index = {v.split('\t', 1)[1]: v.split('\t', 1)[0].split()[0]
             for v in document['original_candidate'][2].split('\0') if v}
    modes = dict(document['original_candidate'][5])
    imports = {f['path']: f for f in document['files']}
    worker = set(document['receipt']['worker_paths'])
    baseline = _tree(repo, document['original_candidate'][0])
    if set(entries) != set(raw):
        raise CandidateStateUnavailable('Final tree paths differ from approved complete raw candidate.')
    for name, entry in entries.items():
        mode = (imports[name]['source_entry']['mode'] if name in imports
                else index.get(name, '100755' if modes.get(name) else '100644'))
        approved = ({'mode': mode, 'kind': 'blob', 'oid': raw[name]}
                    if name in imports or name in worker else baseline.get(name))
        if entry != approved:
            raise CandidateStateUnavailable('Final staged/committed tree differs from approved raw bytes or modes.')


def _state(config, project, task, base):
    _visible(task.worktree_path)
    state = candidate_state(config, project, task, base)
    _visible(task.worktree_path)
    modes = [(n, (os.stat(task.worktree_path / n).st_mode & 0o111) if os.name != 'nt' else 0)
             for n, value in state[4] if value is not None]
    return [*state, modes]


def _history(root, excluded=None):
    result = {}
    for path in sorted(root.rglob('*')):
        if excluded and path.is_relative_to(excluded):
            continue
        _safe_target(root, str(path.relative_to(root)).replace('\\', '/'))
        if path.is_file():
            result[path.relative_to(root).as_posix()] = _hash(_plain_bytes(path, maximum=256 * 1024 * 1024))
    return result


def _scope(task, name):
    return any(fnmatch.fnmatchcase(name, pattern.strip()) for pattern in task.scope.splitlines() if pattern.strip())


def _guard_source(config, db, proof):
    if not isinstance(proof, dict) or not re.fullmatch('[0-9a-f]{32}', str(proof.get('gate_id', ''))):
        raise ValueError('Exact portable source/gate proof is required.')
    source = db.get_project(proof.get('source_project_id'))
    task = db.get_task(proof.get('source_task_id'))
    if not source or not task or task.project_id != source.id or not task.worktree_path:
        raise ValueError('Source registration and retained task checkout are required.')
    gate = config.data_root / 'gate-worktrees' / ('gate-' + proof['gate_id'])
    for checkout in (source.repo_path, task.worktree_path, gate):
        _safe_target(checkout.parent, checkout.name)
        _visible(checkout)


def _checked_source(config, db, project, task, base, proof):
    # Guard BEFORE existing source proof executes status/diff/explicit-candidate checks.
    _guard_source(config, db, proof)
    result = _source(config, db, project, task, base, proof)
    _guard_source(config, db, proof)
    return result


def _parent(config, db, task, project, expected):
    row = db.blocker_continuation(task.id, task.attempts)
    if not row or row['state'] != 'TERMINAL':
        raise ValueError('A terminal prerequisite handoff is required.')
    document = json.loads(row['document_json'])
    if document['id'] != expected or not document.get('prerequisite'):
        raise ValueError('Exact terminal parent prerequisite identity is required.')
    _guard_source(config, db, document['prerequisite']['source']['proof'])
    _prove_source(config, db, project, task, document)
    return row, document


def _source_pins(config, db, task, checkpoint, pins):
    for key in ('source_task', 'source_registration', 'source_candidate', 'workflow_id', 'gate_id', 'gate_candidate'):
        if not isinstance(getattr(pins, key, None), str) or not getattr(pins, key) or len(getattr(pins, key)) > 2048:
            raise ValueError('All exact source and gate pins are required.')
    source_task = db.get_task(pins.source_task)
    source = db.get_project(source_task.project_id) if source_task else None
    source_checkpoint = db.execution_checkpoint(source_task.id) if source_task else None
    if not source or not source_checkpoint:
        raise ValueError('Explicit independently completed source identity is required.')
    require_admission_base_commit(source.repo_path, pins.source_candidate)
    require_admission_base_commit(source.repo_path, pins.gate_candidate)
    a, b = _tree(source.repo_path, checkpoint['base_commit']), _tree(source.repo_path, pins.source_candidate)
    proof = {'source_project_id': source.id, 'source_registration': pins.source_registration,
             'source_task_id': source_task.id, 'source_candidate': pins.source_candidate,
             'workflow_id': pins.workflow_id, 'gate_id': pins.gate_id, 'gate_candidate': pins.gate_candidate,
             'source_specification_digest': source_checkpoint['specification_digest'],
             'source_task_fingerprint': task_fingerprint(source_task),
             'allowed_paths': sorted(n for n in a.keys() | b.keys() if a.get(n) != b.get(n))}
    return proof


def _prepare(config, db, task_id, reason, attestation, parent_id, source_proof, selection_authority=None):
    if (not isinstance(reason, str) or not isinstance(attestation, str)
            or not reason.strip() or len(reason) > 4000 or not attestation.strip() or len(attestation) > 4000):
        raise ValueError('Bounded reason and explicit prospective attestation are required.')
    task = db.get_task(task_id)
    project = db.get_project(task.project_id) if task else None
    checkpoint = db.execution_checkpoint(task_id)
    if (not task or not project or task.state != TaskState.FAILED or task.attempts != task.max_attempts
            or not checkpoint or checkpoint['completion_json'] is not None
            or checkpoint['repository_registration_id'] != project.repository_registration_id or not project.auto_commit):
        raise ValueError('An exhausted failed owned nonterminal candidate with automatic commits is required.')
    if task.admission_base_commit and task.admission_base_commit != checkpoint['base_commit']:
        raise ValueError('Original admission base changed.')
    _prove_plan(config, db, project, task, checkpoint)
    _visible(task.worktree_path)
    parent_row, parent = _parent(config, db, task, project, parent_id)
    from .prerequisites import verify_locked

    verify_locked(config, db, task, document=parent)
    with db.connect() as connection:
        latest = connection.execute('SELECT * FROM runs WHERE task_id=? ORDER BY id DESC LIMIT 1', (task.id,)).fetchone()
        if connection.execute('SELECT 1 FROM tasks WHERE state IN (?,?,?,?)', tuple(s.value for s in ACTIVE_TASK_STATES)).fetchone():
            raise ValueError('Active controller work prevents finalization preflight.')
    if (not latest or not latest['phase'].startswith('review-repair-') or latest['exit_code'] != 0
            or not latest['completed_at'] or Path(latest['artifact_dir']).parent != Path(parent['artifact_root'])):
        raise ValueError('Latest run must be the closed blocked review repair of the terminal parent.')
    output = Path(latest['artifact_dir']) / f"{latest['phase']}.final.json"
    blocked_raw = _plain_bytes(output)
    blocked = json.loads(blocked_raw)
    from .runner import _instance_conforms

    if not _instance_conforms(blocked, load_schema('task-result.schema.json')) or blocked['status'] != 'blocked':
        raise ValueError('Latest terminal repair did not report a schema-valid blocker.')
    prior = _prior_review(config, db, project, task)
    if not prior:
        raise ValueError('An unresolved trusted independent review is required.')
    state = _state(config, project, task, checkpoint['base_commit'])
    source, source_task, identity = _checked_source(config, db, project, task, checkpoint['base_commit'], source_proof)
    old_source = db.get_project(parent['prerequisite']['source']['proof']['source_project_id'])
    old_tree = _tree(old_source.repo_path, parent['prerequisite']['source']['proof']['source_candidate'])
    base_tree = _tree(source.repo_path, checkpoint['base_commit'])
    new_tree = _tree(source.repo_path, source_proof['source_candidate'])
    if any(name in new_tree for name in identity['gate_extra_paths']):
        raise ValueError('Finalization gate modified/deleted retained source evidence.')
    if source_proof['allowed_paths'] != sorted(n for n in base_tree.keys() | new_tree.keys() if base_tree.get(n) != new_tree.get(n)):
        raise ValueError('Source allowed paths do not equal its exact original-base delta.')
    replacement = sorted(n for n in old_tree.keys() | new_tree.keys() if old_tree.get(n) != new_tree.get(n))
    if not replacement or len(replacement) > 256:
        raise ValueError('An exact finite changed prerequisite is required.')
    for name in replacement:
        _path(name)
        if not _scope(source_task, name) or (name.startswith('docs/evidence/') and name in old_tree):
            raise ValueError('Replacement is outside approved source scope or changes historical evidence.')
    old_paths = {f['path'] for f in parent['prerequisite']['files']}
    changed = set(_git(task.worktree_path, 'diff', '--name-only', '-z', checkpoint['base_commit'], '--').stdout.split('\0'))
    changed.update(_git(task.worktree_path, 'ls-files', '--others', '--exclude-standard', '-z').stdout.split('\0'))
    worker = sorted(changed - old_paths - {''})
    if not worker or len(worker) > 128 or any(not _scope(task, n) for n in worker):
        raise ValueError('Prospective worker delta lacks exact original scope authority.')
    _raw_git_inputs(task.worktree_path, worker)
    selection = None
    if selection_authority is not None:
        from .selections import prepare

        selection = prepare(config, db, project, task, checkpoint['base_commit'], source, new_tree,
                            state, worker, selection_authority)
    selected = set(selection['selected']) if selection else set()
    paths = sorted(((old_paths | set(source_proof['allowed_paths'])) - set(worker) | selected)
                   if selection else (old_paths | set(source_proof['allowed_paths'])))
    if len(paths) > 256 or not set(replacement) <= set(paths) | (set(worker) if selection else set()):
        raise ValueError('Replacement contains unrelated baseline changes.')
    names = set(base_tree) | set(new_tree) | {n for n, _ in state[4]}
    folded = {}
    for name in names:
        key = name.casefold()
        if key in folded and folded[key] != name:
            raise ValueError('Candidate namespace contains case aliases.')
        folded[key] = name
    files, total = [], 0
    observed = dict(state[4])
    for name in paths:
        _path(name)
        if any((name.casefold() == n.casefold() and name not in selected)
               or name.casefold().startswith(n.casefold() + '/')
               or n.casefold().startswith(name.casefold() + '/') for n in worker):
            raise ValueError('Replacement overlaps original worker paths.')
        if any(other != name and (other.casefold().startswith(name.casefold() + '/')
                or name.casefold().startswith(other.casefold() + '/')) for other in names):
            raise ValueError('Replacement has file/directory namespace collisions.')
        for tree in (base_tree, old_tree, new_tree):
            if name in tree and tree[name]['mode'] != '100644':
                raise ValueError('Only regular non-executable prerequisite blobs are supported.')
        # Prove old imports are still exactly the approved prior inputs before replacing any.
        prior_file = next((f for f in parent['prerequisite']['files'] if f['path'] == name), None)
        if prior_file and observed.get(name) != prior_file['after_sha256']:
            raise ValueError('Previous locked prerequisite bytes changed.')
        payload = _blob(source.repo_path, new_tree[name]['oid']) if name in new_tree else None
        total += len(payload or b'')
        temporary = name + '.forge-prerequisite.tmp'
        if total > 16 * 1024 * 1024 or temporary.casefold() in folded:
            raise ValueError('Import size or temporary namespace is unsafe.')
        index = _git(task.worktree_path, 'ls-files', '--stage', '-z', '--', name).stdout
        files.append({'path': name, 'before_sha256': observed.get(name), 'after_sha256': _hash(payload) if payload is not None else None,
                      'payload_base64': base64.b64encode(payload).decode('ascii') if payload is not None else None,
                      'base_entry': base_tree.get(name), 'source_entry': new_tree.get(name), 'before_index': index})
    root = config.evidence_root / project.name / task.id
    history = _history(root)
    receipt = {'task_id': task.id, 'attempt': task.attempts, 'candidate_base': checkpoint['base_commit'],
               'parent_continuation_id': parent_id, 'parent_sha256': _hash(_raw(parent_row)),
               'prospective_attestation': {'kind': 'current_prospective_input_not_historical_seal', 'text': attestation},
               'candidate_snapshot_sha256': _hash(json.dumps(state, ensure_ascii=False).encode('utf-8')),
               'candidate_head': state[0], 'candidate_branch': state[1], 'candidate_index_sha256': _hash(state[2].encode()),
               'latest_blocked_repair_sha256': _hash(blocked_raw), 'unresolved_review_sha256': prior['sha256'],
               'historical_evidence_sha256': _hash(_raw(history)), 'worker_paths': worker,
               'worker_file_sha256': {n: observed.get(n) for n in worker},
               'previous_source_candidate': parent['prerequisite']['source']['proof']['source_candidate'],
               'prerequisite': source_proof, 'replacement_paths': replacement, 'limits': _limits(config), 'reason': reason}
    authority = {'task_id': task.id, 'attempt': task.attempts, 'task_fingerprint': task_fingerprint(task),
                 'project_digest': _digest(project, task), 'repository_registration_id': project.repository_registration_id,
                 'base_commit': checkpoint['base_commit'], 'parent': parent_row, 'original_candidate': state,
                 'source': identity, 'files': files, 'prior_review': prior, 'history': history,
                 'blocked_output_path': str(output), 'blocked_result': blocked, 'receipt': receipt}
    if selection:
        receipt['selection'] = selection['authority']
        authority['selection'] = selection
    return receipt, authority


def preflight(config, task_id, reason, attestation, pins):
    db = ForgeDB(config.database_path, read_only=True)
    if db.prerequisite_finalization(task_id):
        raise ValueError('This parent already has its one permitted successor.')
    task = db.get_task(task_id)
    checkpoint = db.execution_checkpoint(task_id)
    if not task or not checkpoint:
        raise ValueError('Task and original checkpoint are required.')
    proof = _source_pins(config, db, task, checkpoint, pins)
    selection = None
    manifest = getattr(pins, 'selection_manifest', None)
    if manifest:
        capability_pins = SimpleNamespace(**{key: getattr(pins, 'capability_' + key, None)
            for key in ('source_task', 'source_registration', 'source_candidate', 'workflow_id', 'gate_id', 'gate_candidate')})
        capability = _source_pins(config, db, task, checkpoint, capability_pins)
        source_task = db.get_task(proof['source_task_id'])
        source = db.get_project(source_task.project_id)
        entry = _tree(source.repo_path, proof['source_candidate']).get(_path(manifest))
        if not entry or entry['mode'] != '100644' or entry['kind'] != 'blob':
            raise ValueError('Selection manifest must be a committed regular source blob.')
        raw_manifest = _blob(source.repo_path, entry['oid'])
        selection = {'manifest_path': manifest, 'manifest_sha256': _hash(raw_manifest),
                     'policy': json.loads(raw_manifest), 'capability': capability}
    elif any(getattr(pins, 'capability_' + key, None) for key in
             ('source_task', 'source_registration', 'source_candidate', 'workflow_id', 'gate_id', 'gate_candidate')):
        raise ValueError('Capability pins require an explicit typed selection manifest.')
    receipt, _ = _prepare(config, db, task_id, reason, attestation, pins.parent_continuation, proof, selection)
    raw = _raw(receipt)
    return {'dry_run': True, 'receipt': receipt, 'receipt_json': raw.decode('utf-8'), 'receipt_sha256': _hash(raw)}


def _row(db, task):
    row = db.prerequisite_finalization(task.id)
    return row, json.loads(row['document_json']), json.loads(row['checkpoint_json'])


def _save(db, task, state, checkpoint):
    with db.connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        connection.execute('UPDATE prerequisite_finalizations SET state=?,checkpoint_json=?,updated_at=? WHERE task_id=?',
                           (state, json.dumps(checkpoint), utc_now(), task.id))


def _proof(config, db, task, document):
    project = db.get_project(task.project_id)
    checkpoint = db.execution_checkpoint(task.id)
    if (task.attempts != document['attempt'] or task_fingerprint(task) != document['task_fingerprint']
            or _digest(project, task) != document['project_digest']
            or checkpoint['base_commit'] != document['base_commit']
            or project.repository_registration_id != document['repository_registration_id']
            or _limits(config) != document['receipt']['limits']):
        raise CandidateStateUnavailable('Finalization admission/base/generation/limits changed.')
    parent = db.blocker_continuation(task.id, task.attempts)
    if parent != document['parent']:
        raise CandidateStateUnavailable('Historical parent continuation changed.')
    _visible(task.worktree_path)
    _guard_source(config, db, json.loads(parent['document_json'])['prerequisite']['source']['proof'])
    _prove_source(config, db, project, task, json.loads(parent['document_json']))
    root = Path(document['artifact_root'])
    if (_hash(_plain_bytes(Path(document['receipt_path']))) != document['receipt_sha256']
            or _plain_bytes(root / 'operator-receipt.raw') != _raw(document['receipt'])
            or _plain_bytes(root / 'finalization.json', maximum=32 * 1024 * 1024) != _raw(document)
            or _history(config.evidence_root / project.name / task.id, root) != document['history']):
        raise CandidateStateUnavailable('Prospective receipt or retained historical evidence changed.')
    _, source_task, source = _checked_source(config, db, project, task, document['base_commit'], document['source']['proof'])
    if source != document['source']:
        raise CandidateStateUnavailable('Replacement source/gate provenance changed.')
    if document.get('selection'):
        from .selections import prepare

        source_project = db.get_project(source_task.project_id)
        selected = prepare(config, db, project, task, document['base_commit'], source_project,
                           _tree(source_project.repo_path, source['proof']['source_candidate']),
                           document['original_candidate'], document['receipt']['worker_paths'],
                           document['selection']['authority'], retained=document['selection']['selected'])
        if selected != document['selection']:
            raise CandidateStateUnavailable('Capability/reconciliation selection authority changed.')
    row = db.prerequisite_finalization(task.id)
    journal = json.loads(row['checkpoint_json'])
    if journal.get('review'):
        if (_history(root / 'review') != journal['review_evidence']
                or _plain_bytes(root / 'trusted-review.json') != _raw({'run_id': journal['review_run_id'],
                    'review': journal['review'], 'raw_sha256': journal['review_sha256'],
                    'evidence': journal['review_evidence'], 'sandbox': journal['review_sandbox'],
                    'requested_sandbox': journal['review_sandbox_requested']})):
            raise CandidateStateUnavailable('Cached trusted review evidence changed.')
    for key, suffix in (('validation', 'initial'), ('final_validation', 'final')):
        if key in journal:
            if (_plain_bytes(root / f'{suffix}-validation.json') != _raw(journal[key])
                    or _history(root / f'validation-{suffix}') != journal[key + '_evidence']):
                raise CandidateStateUnavailable('Cached controller validation evidence changed.')
    for key, name in (('stage_intent', 'stage-intent.json'), ('commit_intent', 'commit-intent.json')):
        if key in journal and _plain_bytes(root / name) != _raw(journal[key]):
            raise CandidateStateUnavailable('Cached finalization commit intent changed.')
    return project


def verify_candidate(config, db, task, *, committed=False):
    row, document, checkpoint = _row(db, task)
    _proof(config, db, task, document)
    state = _state(config, db.get_project(task.project_id), task, document['base_commit'])
    expected = dict(document['original_candidate'][4])
    for item in document['files']:
        if item['after_sha256'] is None:
            expected.pop(item['path'], None)
        else:
            expected[item['path']] = item['after_sha256']
    actual = {n: h for n, h in state[4] if h is not None}
    if actual != {n: h for n, h in expected.items() if h is not None}:
        raise CandidateStateUnavailable('Prospective worker/imported/baseline bytes changed.')
    _raw_git_inputs(task.worktree_path, sorted(set(document['receipt']['worker_paths'])
                    | {f['path'] for f in document['files']}))
    baseline = set(actual) - set(document['receipt']['worker_paths']) - {f['path'] for f in document['files']}
    original_tree = _tree(task.worktree_path, document['original_candidate'][0])
    _raw_git_inputs(task.worktree_path, sorted(baseline), allow_crlf=True,
                    approved={n: original_tree.get(n, {}).get('oid') for n in baseline})
    modes = dict(document['original_candidate'][5])
    for item in document['files']:
        if item['after_sha256'] is None:
            modes.pop(item['path'], None)
        else:
            modes[item['path']] = 0
    if dict(state[5]) != modes:
        raise CandidateStateUnavailable('Prospective source or replacement executable modes changed.')
    intent = checkpoint.get('commit_intent')
    if intent:
        tree = _git(task.worktree_path, 'write-tree').stdout.strip()
        if tree != intent['tree']:
            raise CandidateStateUnavailable('Finalization staged commit tree changed.')
        if state[0] != intent['parent']:
            if (_git(task.worktree_path, 'show', '-s', '--format=%P', 'HEAD').stdout.strip() != intent['parent']
                    or _git(task.worktree_path, 'rev-parse', 'HEAD^{tree}').stdout.strip() != intent['tree']):
                raise CandidateStateUnavailable('Finalization commit is not the journaled exact child/tree.')
    elif checkpoint.get('stage_intent'):
        if state[:2] != document['original_candidate'][:2]:
            raise CandidateStateUnavailable('Staging intent does not permit another HEAD/branch.')
        # A crash may leave any subset staged, but every index entry must still be old or
        # the exact approved current working blob. The controller then finishes its own add.
        original_index = {v.split('\t', 1)[1]: v for v in document['original_candidate'][2].split('\0') if v}
        for entry in state[2].split('\0'):
            if not entry:
                continue
            metadata, name = entry.split('\t', 1)
            if entry == original_index.get(name):
                continue
            mode, oid, stage = metadata.split()
            path = _safe_target(task.worktree_path, name)
            if (stage != '0' or mode != '100644' or not path.is_file()
                    or _git(task.worktree_path, 'hash-object', '--no-filters', str(path)).stdout.strip() != oid):
                raise CandidateStateUnavailable('Partial staging contains an unapproved index entry.')
    elif state[:3] != document['original_candidate'][:3]:
        raise CandidateStateUnavailable('Prospective HEAD/branch/index changed before commit intent.')
    if intent:
        _exact_raw_tree(task.worktree_path, intent['tree'], document)
    if committed:
        _exact_raw_tree(task.worktree_path, 'HEAD', document)
        tree = _tree(task.worktree_path, 'HEAD')
        for item in document['files']:
            if tree.get(item['path']) != item['source_entry']:
                raise CandidateStateUnavailable('Committed replacement blob/mode differs from approved source.')
    if row['state'] not in ('IMPORTED', 'REVIEW_READY', 'REVIEW_STARTED', 'REVIEW_PASSED', 'FINAL_VALIDATED', 'COMMIT_STAGING', 'COMMIT_READY', 'COMPLETED'):
        raise CandidateStateUnavailable('Finalization candidate is not in a verified execution stage.')


def _import(config, db, task, document):
    project = _proof(config, db, task, document)
    current = _state(config, project, task, document['base_commit'])
    original = document['original_candidate']
    if current[:3] != original[:3]:
        raise CandidateStateUnavailable('Prospective HEAD/branch/index changed during import.')
    paths = {f['path'] for f in document['files']}
    temporary = {name + '.forge-prerequisite.tmp' for name in paths}
    if {n: h for n, h in current[4] if n not in paths | temporary} != {n: h for n, h in original[4] if n not in paths}:
        raise CandidateStateUnavailable('Prospective worker/baseline changed during replacement.')
    if {n: mode for n, mode in current[5] if n not in paths | temporary} != {n: mode for n, mode in original[5] if n not in paths}:
        raise CandidateStateUnavailable('Prospective executable modes changed during replacement.')
    for item in document['files']:
        path = _safe_target(task.worktree_path, item['path'])
        observed = _hash(_plain_bytes(path, maximum=4 * 1024 * 1024)) if path.exists() else None
        if observed not in (item['before_sha256'], item['after_sha256']):
            raise CandidateStateUnavailable('Replacement is neither its journaled old nor new bytes.')
        payload = base64.b64decode(item['payload_base64'], validate=True) if item['payload_base64'] is not None else None
        _import_file(path, payload)
    imported = _state(config, project, task, document['base_commit'])
    checkpoint = {'imported_candidate_snapshot_sha256': _hash(json.dumps(imported, ensure_ascii=False).encode('utf-8'))}
    _write_immutable(Path(document['artifact_root']) / 'imported.json', _raw(checkpoint))
    _save(db, task, 'IMPORTED', checkpoint)
    verify_candidate(config, db, task)


def before_commit(config, db, task):
    row = getattr(db, 'prerequisite_finalization', lambda *_: None)(task.id)
    if not row:
        return
    verify_candidate(config, db, task)
    _, document, checkpoint = _row(db, task)
    if not checkpoint.get('commit_intent'):
        if not checkpoint.get('stage_intent'):
            checkpoint['stage_intent'] = {'parent': document['original_candidate'][0],
                                         'prospective_receipt_sha256': document['receipt_sha256']}
            _write_immutable(Path(document['artifact_root']) / 'stage-intent.json', _raw(checkpoint['stage_intent']))
            _save(db, task, 'COMMIT_STAGING', checkpoint)
        _git(task.worktree_path, 'add', '-A')
        tree = _git(task.worktree_path, 'write-tree').stdout.strip()
        _exact_raw_tree(task.worktree_path, tree, document)
        checkpoint['commit_intent'] = {'parent': document['original_candidate'][0], 'tree': tree}
        _write_immutable(Path(document['artifact_root']) / 'commit-intent.json', _raw(checkpoint['commit_intent']))
        _save(db, task, 'COMMIT_READY', checkpoint)
    verify_candidate(config, db, task)


def review_context(config, db, task):
    _, document, _ = _row(db, task)
    return ('\nCONTROLLER-OWNED PREREQUISITE FINALIZATION: no implementation or repair is authorized.\n'
            + json.dumps({'prospective_attestation': document['receipt']['prospective_attestation'],
                          'parent_continuation_id': document['receipt']['parent_continuation_id'],
                          'source': document['source']['proof'], 'replacement_paths': document['receipt']['replacement_paths'],
                          'prospective_worker_paths': document['receipt']['worker_paths'],
                          'separately_budgeted_selection': document.get('selection'),
                          'unresolved_prior_review': document['prior_review'], 'original_base': document['base_commit']}, ensure_ascii=False)
            + '\nReview the full original-base delta against the unchanged task contract. The replacement delta is separately approved '
              'controller-owned input. Explicitly verify every carried finding, including generic UI interactions; do not assume tests '
              'override SPEC or that a prior blocked output was a completed/validated candidate. Do not modify candidate files.\n')


def _public(db, task, row):
    document = json.loads(row['document_json'])
    checkpoint = json.loads(row['checkpoint_json'])
    result = {'task_id': task.id, 'attempt': task.attempts, 'finalization_id': document['id'], 'state': row['state'],
              'task_state': task.state.value, 'implementation_calls': 0, 'repair_calls': 0,
              'review_started': bool(checkpoint.get('review_started') or row['state'] == 'REVIEW_STARTED'),
              'parent_continuation_id': document['receipt']['parent_continuation_id'],
              'operator_receipt_sha256': document['receipt_sha256'], 'prospective_attestation': document['receipt']['prospective_attestation'],
              'candidate_base': document['base_commit'], 'prospective_candidate_snapshot_sha256': document['receipt']['candidate_snapshot_sha256'],
              'previous_source_candidate': document['receipt']['previous_source_candidate'],
              'prerequisite': document['source']['proof'], 'worker_paths': document['receipt']['worker_paths'],
              'worker_file_sha256': document['receipt']['worker_file_sha256'], 'replacement_paths': document['receipt']['replacement_paths'],
              'latest_blocked_repair_sha256': document['receipt']['latest_blocked_repair_sha256'],
              'unresolved_review_sha256': document['receipt']['unresolved_review_sha256'],
              'failure': checkpoint.get('failure'), 'final_candidate': task.result.get('commit') if task.state == TaskState.COMPLETED else None}
    if document.get('selection'):
        result['selection'] = document['selection']['authority']
        result['capability'] = document['selection']['capability_identity']['proof']
    result['attribution'] = task.result.get('prerequisite_finalization')
    return result


def terminal_evidence(config, db, task, artifact_root, base, candidate, changed_files):
    verify_candidate(config, db, task, committed=True)
    _, document, checkpoint = _row(db, task)
    proof = document['source']['proof']
    locked = [f['path'] for f in document['files']]
    selection = document.get('selection')
    selected_names = list(selection['selected']) if selection else []
    capability_proof = selection['capability_identity']['proof'] if selection else proof
    capability = db.get_project(capability_proof['source_project_id'])
    source_patch = _git_bytes(capability.repo_path, 'diff', '--no-ext-diff', '--no-textconv', '--binary',
                              base, capability_proof['source_candidate'], '--')
    worker_patch = _git_bytes(task.worktree_path, 'diff', '--no-ext-diff', '--no-textconv', '--binary', base, candidate,
                              '--', '.', *[':(exclude,literal)' + name for name in locked if name not in selected_names])
    for name, data in (('prerequisite.patch', source_patch), ('worker-changes.patch', worker_patch)):
        _write_immutable(artifact_root / name, data)
    attribution = {'finalization_id': document['id'], 'parent_continuation_id': document['receipt']['parent_continuation_id'],
            'operator_receipt_sha256': document['receipt_sha256'], 'prospective_attestation': document['receipt']['prospective_attestation'],
            'prospective_candidate_snapshot_sha256': document['receipt']['candidate_snapshot_sha256'],
            'imported_candidate_snapshot_sha256': checkpoint['imported_candidate_snapshot_sha256'],
            'previous_source_candidate': document['receipt']['previous_source_candidate'], 'source': proof,
            'original_base_commit': base, 'final_candidate_commit': candidate,
            'carried_review_run_id': document['prior_review']['source_run_id'], 'fresh_review_run_id': checkpoint['review_run_id'],
            'worker_changed_files': [n for n in changed_files if n not in locked or n in selected_names],
            'locked_files': [{k: f[k] for k in ('path', 'after_sha256', 'base_entry', 'source_entry')} for f in document['files']],
            'prerequisite_patch_sha256': _hash(source_patch), 'worker_patch_sha256': _hash(worker_patch)}
    if selection:
        # The prospective before bytes are retained input, never claimed to be historical output.
        reconciliation = b''
        for name, value in selection['selected'].items():
            before = base64.b64decode(value['before_base64'], validate=True)
            after = _blob(db.get_project(proof['source_project_id']).repo_path,
                          _tree(db.get_project(proof['source_project_id']).repo_path, proof['source_candidate'])[name]['oid'])
            reconciliation += b''.join(difflib.diff_bytes(difflib.unified_diff, before.splitlines(keepends=True),
                after.splitlines(keepends=True), ('a/' + name).encode(), ('b/' + name).encode()))
        _write_immutable(artifact_root / 'reconciliation.patch', reconciliation)
        attribution.update(selection=selection['authority'], capability=capability_proof,
                           reconciliation_sha256=_hash(reconciliation))
    return attribution


def execute(orchestrator, task_id, reason, receipt_path, digest):
    o, db, config = orchestrator, orchestrator.db, orchestrator.config
    with controller_lock(config.data_root / 'controller.lock'):
        raw = _plain_bytes(receipt_path)
        if _hash(raw) != digest:
            raise ValueError('Prospective receipt SHA256 mismatch.')
        receipt = json.loads(raw)
        if not isinstance(receipt, dict) or raw != _raw(receipt):
            raise ValueError('The exact canonical preflight receipt bytes are required.')
        task = db.get_task(task_id)
        row = db.prerequisite_finalization(task_id)
        if row:
            document = json.loads(row['document_json'])
            if (document['receipt_sha256'] != digest or document['receipt_path'] != str(receipt_path.absolute())
                    or receipt != document['receipt'] or reason != receipt['reason']):
                raise ValueError('A different successor/receipt already owns this task.')
            if row['state'] in ('FAILED', 'COMPLETED'):
                return _public(db, task, row)  # No provenance or mutable checkout reads on terminal replay.
        else:
            if reason != receipt.get('reason'):
                raise ValueError('Receipt reason changed.')
            try:
                prospective = receipt['prospective_attestation']
                attestation = prospective['text']
                parent_id, proof = receipt['parent_continuation_id'], receipt['prerequisite']
            except (KeyError, TypeError) as error:
                raise ValueError('A complete prospective preflight receipt is required.') from error
            expected, document = _prepare(config, db, task_id, reason, attestation, parent_id, proof, receipt.get('selection'))
            if expected != receipt or _raw(expected) != raw:
                raise ValueError('Prospective candidate/receipt pins changed before reservation.')
            identity = uuid.uuid4().hex
            root = config.evidence_root / db.get_project(task.project_id).name / task.id / f'attempt-{task.attempts:02d}-f-{identity}'
            document.update(id=identity, artifact_root=str(root), receipt_path=str(receipt_path.absolute()), receipt_sha256=digest)
            with db.connect() as connection:
                connection.execute('BEGIN IMMEDIATE')
                current = db._task_from_row(connection.execute('SELECT * FROM tasks WHERE id=?', (task.id,)).fetchone())
                if current.state != TaskState.FAILED or task_fingerprint(current) != document['task_fingerprint']:
                    raise ValueError('Target changed before successor reservation.')
                if connection.execute('SELECT 1 FROM tasks WHERE state IN (?,?,?,?)', tuple(s.value for s in ACTIVE_TASK_STATES)).fetchone():
                    raise ValueError('Active worker prevents successor reservation.')
                source = receipt['prerequisite']
                if connection.execute('SELECT 1 FROM prerequisite_receipts WHERE receipt_sha256=? OR (source_registration=? AND source_task=? AND source_candidate=?)',
                                      (digest, source['source_registration'], source['source_task_id'], source['source_candidate'])).fetchone():
                    raise ValueError('Replacement authority was already used.')
                # Re-prove under the reservation transaction; no stale preflight may authorize writes.
                current_receipt, _ = _prepare(config, db, task_id, reason, prospective['text'],
                                                receipt['parent_continuation_id'], receipt['prerequisite'], receipt.get('selection'))
                if current_receipt != receipt or _raw(current_receipt) != raw or _plain_bytes(receipt_path) != raw:
                    raise ValueError('Prospective inputs changed during reservation.')
                root.mkdir(parents=True, exist_ok=False)
                _write_immutable(root / 'operator-receipt.raw', raw)
                _write_immutable(root / 'finalization.json', _raw(document))
                connection.execute('INSERT INTO prerequisite_finalizations(task_id,parent_id,state,document_json,updated_at) VALUES(?,?,?,?,?)',
                                   (task.id, receipt['parent_continuation_id'], 'IMPORTING', json.dumps(document), utc_now()))
                connection.execute('INSERT INTO prerequisite_receipts VALUES(?,?,?,?,?,?)',
                                   (digest, source['source_registration'], source['source_task_id'], source['source_candidate'], task.id, task.attempts))
                connection.execute("UPDATE tasks SET state='VALIDATING',updated_at=? WHERE id=?", (utc_now(), task.id))
        # Active operation ownership must also exclude public deletion, which uses a DB claim
        # rather than the controller lock. A paused successor becomes BLOCKED only on exit.
        with db.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            current = connection.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if current is None or task_fingerprint(db._task_from_row(current)) != document['task_fingerprint']:
                raise ValueError('Target was deleted or changed before active successor ownership.')
            if current['state'] != TaskState.COMPLETED.value:
                connection.execute("UPDATE tasks SET state='VALIDATING',updated_at=? WHERE id=?", (utc_now(), task_id))
        try:
            _execute_stages(o, db.get_task(task_id))
        except BaseException as error:
            row = db.prerequisite_finalization(task_id)
            checkpoint = json.loads(row['checkpoint_json'])
            from .orchestrator import TaskUsageBudgetExceeded

            if db.get_task(task_id).state == TaskState.COMPLETED:
                # Completion is already durable; an old controller may advance the normal cursor.
                pass
            elif isinstance(error, TaskUsageBudgetExceeded):
                checkpoint['failure'] = 'FINALIZATION_TOKEN_BUDGET_EXCEEDED'
                _save(db, task, 'FAILED', checkpoint)
                o.db.update_task(task_id, state=TaskState.FAILED, last_error=checkpoint['failure'])
            elif row['state'] == 'REVIEW_STARTED':
                # A started run may be recovered only from its closed exact evidence, never launched again.
                o.db.update_task(task_id, state=TaskState.FAILED, last_error='FINALIZATION_REVIEW_OUTCOME_UNCERTAIN')
            elif row['state'] in ('IMPORTING', 'IMPORTED', 'REVIEW_READY', 'REVIEW_PASSED', 'FINAL_VALIDATED', 'COMMIT_STAGING', 'COMMIT_READY'):
                o.db.update_task(task_id, state=TaskState.BLOCKED, last_error='FINALIZATION_REQUIRES_EXPLICIT_RESUME')
            else:
                checkpoint['failure'] = type(error).__name__
                _save(db, task, 'FAILED', checkpoint)
                o.db.update_task(task_id, state=TaskState.FAILED, last_error='FINALIZATION_FAILED')
            raise
        return _public(db, db.get_task(task_id), db.prerequisite_finalization(task_id))


def _fail(o, task, checkpoint, reason):
    checkpoint['failure'] = reason
    _save(o.db, task, 'FAILED', checkpoint)
    o.db.update_task(task.id, state=TaskState.FAILED, last_error=reason)


def _execute_stages(o, task):
    db, config = o.db, o.config
    row, document, checkpoint = _row(db, task)
    project = _proof(config, db, task, document)
    completion = db.execution_checkpoint(task.id)
    if task.state != TaskState.COMPLETED:
        _prove_plan(config, db, project, task, completion)
    root = Path(document['artifact_root'])
    commands = flatten_unique([*project.validation_commands, *task.validation_commands])
    if completion['completion_json'] is not None:
        verify_candidate(config, db, task, committed=True)
        final = json.loads(completion['completion_json'])
        if (explicit_candidate_commit(project.repo_path, task.worktree_path, document['base_commit']) != final['commit']
                or completion['specification_digest'] != _digest(project, task)):
            raise CandidateStateUnavailable('Cached finalization completion changed.')
        if task.state == TaskState.COMPLETED:
            plan = o.workflows.get(project.id)
            expected = {'task_id': task.id, 'base_commit': document['base_commit'], 'candidate_commit': final['commit']}
            if ({k: v for k, v in task.result.items() if k != 'completed_at'}
                    != {k: v for k, v in final.items() if k != 'completed_at'} or not plan or plan['specifications'].get(task.id) != _digest(project, task)
                    or (plan.get('pending') != expected and expected not in plan['integrations'])):
                raise CandidateStateUnavailable('Completed task has no exact retained integration lineage.')
            tip = o.workflows._prove_ref(project, plan)
            if _git(project.repo_path, 'merge-base', '--is-ancestor', final['commit'], tip, check=False).returncode != 0:
                raise CandidateStateUnavailable('Normal integration no longer descends from completed candidate.')
        else:
            o._complete_task(task, project, task.worktree_path, root, final)
        _save(db, task, 'COMPLETED', checkpoint)
        o.workflows.advance_all()
        o.workflows.advance_all()
        return
    if row['state'] == 'IMPORTING':
        _import(config, db, task, document)
        row, document, checkpoint = _row(db, task)
    verify_candidate(config, db, task)
    if row['state'] == 'IMPORTED':
        validation = o._validate(task, task.worktree_path, root, commands)
        if not o._all_passed(validation):
            return _fail(o, task, checkpoint, 'FINALIZATION_VALIDATION_FAILED')
        checkpoint['validation'] = [o._validation_to_dict(v) for v in validation]
        checkpoint['validation_evidence'] = _history(root / 'validation-initial')
        _write_immutable(root / 'initial-validation.json', _raw(checkpoint['validation']))
        _save(db, task, 'REVIEW_READY', checkpoint)
        row = db.prerequisite_finalization(task.id)
    if checkpoint.get('review'):
        from .runner import SandboxResolution

        resolution = SandboxResolution(requested=checkpoint['review_sandbox_requested'], sandbox=checkpoint['review_sandbox'], probe=None)
    else:
        resolution = o.runner.resolve_sandbox(config.review_sandbox, cwd=task.worktree_path)
    if resolution.sandbox is None:
        return _fail(o, task, checkpoint, 'FINALIZATION_REVIEW_SANDBOX_UNAVAILABLE')
    if row['state'] == 'REVIEW_READY':
        verify_candidate(config, db, task)
        from .telemetry import ROLE_REVIEWER

        db.update_task(task.id, state=TaskState.REVIEWING)
        result = o._run_codex_phase(task, project, root, phase='review', model=o._review_model(task), effort=o._review_effort(task),
            prompt=review_prompt(project, replace(task, execution_base_commit=document['base_commit']), task.worktree_path,
                                 previous_findings=document['prior_review']['findings'],
                                 validation_results=_validations(checkpoint['validation']), max_output_chars=config.max_output_chars),
            schema='review-result.schema.json', sandbox=resolution.sandbox, role=ROLE_REVIEWER)
        _cache_review(o, task, root, result, resolution)
        row, document, checkpoint = _row(db, task)
    elif row['state'] == 'REVIEW_STARTED':
        from .models import CodexRunResult

        with db.connect() as connection:
            runs = connection.execute('SELECT * FROM runs WHERE task_id=? AND artifact_dir=?', (task.id, str(root / 'review'))).fetchall()
        trusted = root / 'trusted-review.json'
        if len(runs) != 1 or not runs[0]['completed_at'] or runs[0]['exit_code'] != 0 or not trusted.is_file():
            return _fail(o, task, checkpoint, 'FINALIZATION_REVIEW_OUTCOME_UNCERTAIN')
        run = runs[0]
        sealed = json.loads(_plain_bytes(trusted))
        if sealed['run_id'] != run['id'] or sealed['evidence'] != _history(root / 'review'):
            raise CandidateStateUnavailable('Started review lacks an exact controller-certified cached outcome.')
        result = CodexRunResult(exit_code=0, final={}, final_path=root / 'review/review.final.json',
                                jsonl_path=root / 'review/review.events.jsonl', stderr_path=root / 'review/review.stderr.log')
        _cache_review(o, task, root, result, resolution)
        row, document, checkpoint = _row(db, task)
    if row['state'] == 'FAILED':
        return
    if row['state'] == 'REVIEW_PASSED':
        validation = o._validate(task, task.worktree_path, root, commands, suffix='final')
        if not o._all_passed(validation):
            return _fail(o, task, checkpoint, 'FINALIZATION_FINAL_VALIDATION_FAILED')
        checkpoint['final_validation'] = [o._validation_to_dict(v) for v in validation]
        checkpoint['final_validation_evidence'] = _history(root / 'validation-final')
        _write_immutable(root / 'final-validation.json', _raw(checkpoint['final_validation']))
        _save(db, task, 'FINAL_VALIDATED', checkpoint)
    else:
        validation = _validations(checkpoint['final_validation'])
    verify_candidate(config, db, task)
    # Ordinary frozen workflow and completion tail remain the only integration authority.
    plan = o.workflows.get(project.id)
    if plan and plan['state'] == 'BLOCKED':
        o.workflows.resume(project.id)
    summary = ('Controller prerequisite finalization with separately approved typed metadata reconciliation.'
               if document.get('selection') else 'Controller prerequisite finalization; original worker files preserved.')
    result = {**document['blocked_result'], 'summary': summary,
              'blocking_reason': None}
    o._finish_validated_candidate(task, project, task.worktree_path, root, document['base_commit'], result,
                                  checkpoint['review'], validation, resolution)
    _save(db, task, 'COMPLETED', _row(db, task)[2])
    o.workflows.advance_all()
    o.workflows.advance_all()


def _cache_review(o, task, root, result, resolution):
    _, _, checkpoint = _row(o.db, task)
    checkpoint['review_started'] = True
    o._enforce_usage_budget(task)
    if detect_review_infrastructure_block(result, requested_sandbox=resolution.requested, sandbox=resolution.sandbox):
        return _fail(o, task, checkpoint, 'FINALIZATION_REVIEW_INFRASTRUCTURE_FAILED')
    final, _ = o._trusted_review_final(task, root, resolution, result)
    if final is None or final.get('verdict') != 'pass':
        return _fail(o, task, checkpoint, 'FINALIZATION_REVIEW_NOT_PASSED')
    with o.db.connect() as connection:
        row = connection.execute('SELECT * FROM runs WHERE task_id=? AND artifact_dir=?', (task.id, str(root / 'review'))).fetchone()
    checkpoint['review_run_id'] = row['id']
    checkpoint['review'] = final
    checkpoint['review_sha256'] = _hash(_plain_bytes(result.final_path))
    checkpoint['review_evidence'] = _history(root / 'review')
    checkpoint['review_sandbox'] = resolution.sandbox
    checkpoint['review_sandbox_requested'] = resolution.requested
    _write_immutable(root / 'trusted-review.json', _raw({'run_id': row['id'], 'review': final,
                                                      'raw_sha256': checkpoint['review_sha256'],
                                                      'evidence': checkpoint['review_evidence'],
                                                      'sandbox': resolution.sandbox, 'requested_sandbox': resolution.requested}))
    _save(o.db, task, 'REVIEW_PASSED', checkpoint)


def _validations(rows):
    from .models import ValidationResult

    return [ValidationResult(command=v['command'], exit_code=v['exit_code'], passed=v['passed'],
                             duration_seconds=v['duration_seconds'], stdout_path=Path(v['stdout']),
                             stderr_path=Path(v['stderr']), summary='Retained controller validation') for v in rows]


def terminal_outcome(config, task_id, reason, receipt_path, digest):
    """A terminal replay opens SQLite read-only and never initializes controller services."""
    db = ForgeDB(config.database_path, read_only=True)
    row = db.prerequisite_finalization(task_id)
    if row is None or row['state'] not in ('FAILED', 'COMPLETED'):
        return None
    raw = _plain_bytes(receipt_path)
    document = json.loads(row['document_json'])
    if (_hash(raw) != digest or digest != document['receipt_sha256']
            or str(receipt_path.absolute()) != document['receipt_path'] or json.loads(raw) != document['receipt']
            or reason != document['receipt']['reason']):
        raise ValueError('Terminal successor receipt identity changed.')
    return _public(db, db.get_task(task_id), row)
