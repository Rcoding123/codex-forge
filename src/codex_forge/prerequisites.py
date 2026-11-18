"""Governed, disjoint prerequisite imports for controller-sealed model blockers.

No task contract, execution base, branch or budget is changed. The journal is
written before filesystem mutation; recovery accepts only recorded old/new bytes.
"""
from __future__ import annotations

import base64
import json
import os
import stat
import subprocess
from pathlib import Path

from .blockers import _hash, _plain_bytes, candidate_state
from .evidence import safe_path_component
from .gitops import CandidateStateUnavailable, _git, explicit_candidate_commit, require_admission_base_commit
from .models import TaskState
from .task_submission import task_fingerprint
from .utils import flatten_unique
from .workflows import ProjectWorkflows, _digest


def _path(name: str) -> str:
    if (not isinstance(name, str) or not name or '\\' in name or name.startswith('/')
            or any(not safe_path_component(part) or part.casefold().startswith('.git')
                   or part.casefold() in ('agents.md', 'claude.md', 'spec.md', 'skill.md') for part in name.split('/'))):
        raise ValueError('Prerequisite path is not a portable plain repository path.')
    # Authority, configuration, owner datasets and deployment inputs are never imported by this operation.
    if name != 'docs/ARCHITECTURE.md' and not name.startswith(('engine/src/', 'engine/tests/', 'engine/scripts/', 'client/', 'contracts/', 'content/', 'docs/evidence/')):
        raise ValueError('Prerequisite path is outside supported implementation/evidence roots.')
    return name


def _git_bytes(repo, *arguments):
    try:
        return subprocess.run(['git', *arguments], cwd=repo, capture_output=True,
                              timeout=60, check=True).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise CandidateStateUnavailable("Exact prerequisite Git bytes are unavailable.") from exc


def _tree(repo, commit):
    entries = {}
    for line in _git_bytes(repo, 'ls-tree', '-r', '-z', commit).decode('utf-8').split('\0'):
        if line:
            metadata, name = line.split('\t', 1)
            mode, kind, oid = metadata.split(' ')
            entries[name] = {'mode': mode, 'kind': kind, 'oid': oid}
    return entries


def _blob(repo, oid):
    size = int(_git(repo, 'cat-file', '-s', oid).stdout)
    if size > 4 * 1024 * 1024:
        raise ValueError('Prerequisite file exceeds the bounded import size.')
    # cat-file has no text decoding or newline translation; only local canonical object IDs enter argv.
    data = _git_bytes(repo, 'cat-file', 'blob', oid)
    if len(data) != size:
        raise ValueError('Prerequisite blob changed or is unavailable.')
    return data


def _source(config, db, target_project, target_task, base, proof):
    required = {'source_project_id', 'source_registration', 'source_task_id', 'source_candidate',
                'workflow_id', 'gate_id', 'gate_candidate', 'allowed_paths', 'source_specification_digest', 'source_task_fingerprint'}
    if (not isinstance(proof, dict) or set(proof) != required
            or any(not isinstance(proof[key], str) or not proof[key] or len(proof[key]) > 2048
                   for key in required - {'allowed_paths'})):
        raise ValueError('Prerequisite identity and exact allowed paths are required.')
    source = db.get_project(proof['source_project_id'])
    task = db.get_task(proof['source_task_id'])
    checkpoint = db.execution_checkpoint(task.id) if task else None
    if (not source or source.id == target_project.id or source.repository_registration_id != proof['source_registration']
            or source.repo_path.resolve() == target_project.repo_path.resolve() or not task or task.project_id != source.id
            or task.id == target_task.id or task.state != TaskState.COMPLETED or not checkpoint
            or checkpoint['repository_registration_id'] != source.repository_registration_id
            or checkpoint['base_commit'] != base or checkpoint['candidate_commit'] != proof['source_candidate']
            or checkpoint['specification_digest'] != _digest(source, task)
            or checkpoint['specification_digest'] != proof['source_specification_digest']
            or task_fingerprint(task) != proof['source_task_fingerprint'] or checkpoint['completion_json'] is None):
        raise ValueError('Prerequisite is not an independently completed exact-base task of this registration.')
    for repository in (source.repo_path, target_project.repo_path):
        _safe_target(repository.parent, repository.name)
        if _git(repository, 'for-each-ref', '--format=%(refname)', 'refs/replace/').stdout:
            raise ValueError('Replacement Git objects cannot supply prerequisite provenance.')
    final = json.loads(checkpoint['completion_json'])
    commands = flatten_unique([*source.validation_commands, *task.validation_commands])
    if (not commands or final.get('base_commit') != base or final.get('commit') != proof['source_candidate']
            or final.get('review', {}).get('verdict') != 'pass' or final.get('status') != 'completed'
            or task.result.get('commit') != proof['source_candidate'] or task.result.get('base_commit') != base
            or [v.get('command') for v in final.get('controller_validation', [])] != commands
            or any(type(v.get('exit_code')) is not int or v['exit_code'] != 0 or v.get('passed') is not True for v in final['controller_validation'])):
        raise ValueError('Prerequisite lacks independent review and exact final validation.')
    if (_git(source.repo_path, 'status', '--porcelain', '--untracked-files=all').stdout
            or explicit_candidate_commit(source.repo_path, task.worktree_path, base) != proof['source_candidate']):
        raise ValueError('Prerequisite repository or retained candidate is dirty or changed.')
    source_snapshot = candidate_state(config, source, task, base)
    if _git(task.worktree_path, 'symbolic-ref', '--short', 'HEAD').stdout.strip() != task.branch:
        raise ValueError('Prerequisite task branch changed.')
    plan = ProjectWorkflows(config, db).get(source.id)
    if (not plan or plan['id'] != proof['workflow_id'] or plan['registration'] != source.repository_registration_id
            or plan['state'] != 'COMPLETED' or plan['pending'] is not None or plan['head_commit'] != proof['gate_candidate']):
        raise ValueError('Prerequisite has no completed same-registration gate workflow.')
    if ProjectWorkflows._prove_ref(source, plan) != proof['gate_candidate']:
        raise ValueError('Prerequisite gate integration tip changed.')
    for integration in plan['integrations']:
        completed = db.get_task(integration['task_id'])
        sealed = db.execution_checkpoint(integration['task_id'])
        if (not completed or completed.state != TaskState.COMPLETED or not sealed
                or sealed['base_commit'] != integration['base_commit']
                or sealed['candidate_commit'] != integration['candidate_commit']
                or completed.result.get('commit') != integration['candidate_commit']
                or _digest(source, completed) != plan['specifications'][completed.id]):
            raise ValueError('Prerequisite gate integration provenance changed.')
    matches = [g for g in plan['gates'] if g['id'] == proof['gate_id']]
    if len(matches) != 1:
        raise ValueError('Prerequisite gate identity is ambiguous.')
    gate = matches[0]
    if (gate['status'] != 'PASSED' or gate['commit'] != proof['gate_candidate'] or gate['commands'] != commands
            or [v.get('command') for v in gate.get('validation', [])] != commands
            or any(type(v.get('exit_code')) is not int or v['exit_code'] != 0 or v.get('passed') is not True for v in gate['validation'])):
        raise ValueError('Prerequisite gate did not pass the exact admitted commands at its recorded candidate.')
    gate_path = config.data_root / 'project-evidence' / plan['id'] / f"gate-{gate['id']}" / 'result.json'
    gate_raw = _plain_bytes(gate_path)
    if json.loads(gate_raw) != gate:
        raise ValueError('Retained prerequisite gate evidence changed.')
    gate_checkout = config.data_root / 'gate-worktrees' / f"gate-{gate['id']}"
    _safe_target(gate_checkout.parent, gate_checkout.name)
    gate_flags = _git(gate_checkout, 'ls-files', '-v', '-z').stdout
    if any(record and not record.startswith('H ') for record in gate_flags.split('\0')):
        raise ValueError('Prerequisite gate has hidden or unmerged index entries.')
    if (_git(gate_checkout, 'symbolic-ref', '--quiet', 'HEAD', check=False).returncode != 1
            or explicit_candidate_commit(source.repo_path, gate_checkout, proof['gate_candidate']) != proof['gate_candidate']):
        raise ValueError('Prerequisite detached gate checkout changed.')
    if _git(gate_checkout, 'ls-files', '-v', '-z').stdout != gate_flags:
        raise ValueError('Prerequisite gate index visibility changed during proof.')
    require_admission_base_commit(source.repo_path, proof['source_candidate'])
    if _git(source.repo_path, 'merge-base', '--is-ancestor', proof['source_candidate'], proof['gate_candidate'], check=False).returncode:
        raise ValueError('Gate candidate does not descend from the prerequisite candidate.')
    source_tree, gated_tree = _tree(source.repo_path, proof['source_candidate']), _tree(source.repo_path, proof['gate_candidate'])
    gate_extra = sorted(n for n in source_tree.keys() | gated_tree.keys() if source_tree.get(n) != gated_tree.get(n))
    for name in gate_extra:
        _path(name)
        if not name.startswith('docs/evidence/') or any(t.get(name, {}).get('mode', '100644') != '100644' for t in (source_tree, gated_tree)):
            raise ValueError('Subsequent gate task changed prerequisite application inputs.')
    # Pin the complete workflow and task checkpoint; later extensions/relocation are explicit invalidations.
    return source, task, {'proof': proof, 'repo_path': str(source.repo_path), 'task_fingerprint': task_fingerprint(task),
                          'checkpoint': dict(checkpoint), 'source_snapshot': _hash(json.dumps(source_snapshot, ensure_ascii=False).encode('utf-8')),
                          'workflow': plan, 'gate_result_sha256': _hash(gate_raw), 'gate_extra_paths': gate_extra}


def prepare_handoff(config, db, project, task, resolution, receipt):
    proof = receipt.get('prerequisite')
    source, source_task, identity = _source(config, db, project, task, resolution['base_commit'], proof)
    before = candidate_state(config, project, task, resolution['base_commit'])
    if _hash(json.dumps(before, ensure_ascii=False).encode('utf-8')) != resolution['candidate_snapshot']:
        raise ValueError('Original candidate changed before prerequisite preparation.')
    base_tree, source_tree = _tree(source.repo_path, resolution['base_commit']), _tree(source.repo_path, proof['source_candidate'])
    paths = sorted(n for n in base_tree.keys() | source_tree.keys() if base_tree.get(n) != source_tree.get(n))
    allowed = proof['allowed_paths']
    if (not isinstance(allowed, list) or not all(isinstance(name, str) for name in allowed)
            or not paths or len(paths) > 256 or allowed != sorted(set(allowed)) or paths != allowed):
        raise ValueError('Prerequisite delta must equal the exact bounded approved path list.')
    changed = set(_git(task.worktree_path, 'diff', '--name-only', '-z', resolution['base_commit'], '--').stdout.split('\0'))
    changed.update(_git(task.worktree_path, 'ls-files', '-z', '--others', '--exclude-standard').stdout.split('\0'))
    changed.discard('')
    names = list(base_tree) + list(source_tree) + [n for n, _ in before[4]]
    folded = {}
    for name in set(names):
        key = name.casefold()
        if key in folded and folded[key] != name:
            raise ValueError('Prerequisite namespace contains case aliases.')
        folded[key] = name
    entries, total = [], 0
    original_files = dict(before[4])
    for name in paths:
        _path(name)
        for other in names:
            if other != name and (other.casefold().startswith(name.casefold() + '/') or name.casefold().startswith(other.casefold() + '/')):
                raise ValueError('Prerequisite paths have a file/directory namespace conflict.')
        if any(name.casefold() == n.casefold() or name.casefold().startswith(n.casefold() + '/')
               or n.casefold().startswith(name.casefold() + '/') for n in changed):
            raise ValueError('Prerequisite and failed worker deltas overlap.')
        for tree in (base_tree, source_tree):
            if name in tree and (tree[name]['mode'] != '100644' or tree[name]['kind'] != 'blob'):
                raise ValueError('Prerequisite mode, link, submodule or type change is unsupported.')
        payload = _blob(source.repo_path, source_tree[name]['oid']) if name in source_tree else None
        total += len(payload or b'')
        if total > 16 * 1024 * 1024:
            raise ValueError('Prerequisite delta exceeds the bounded import size.')
        temporary = name + '.forge-prerequisite.tmp'
        if temporary.casefold() in folded:
            raise ValueError('Reserved prerequisite temporary path already exists.')
        entries.append({'path': name, 'temporary_path': temporary, 'before_sha256': original_files.get(name),
                        'after_sha256': _hash(payload) if payload is not None else None,
                        'base_entry': base_tree.get(name), 'source_entry': source_tree.get(name),
                        'payload_base64': base64.b64encode(payload).decode('ascii') if payload is not None else None})
    seal = _plain_bytes(Path(resolution['implementation_path']).parent / 'blocked-candidate.json')
    if json.loads(seal) != {'task_id': task.id, 'attempt': task.attempts, 'source_run_id': resolution['source_run_id'],
                            'implementation_sha256': resolution['implementation_sha256'], 'candidate_snapshot_sha256': resolution['candidate_snapshot'],
                            'base_commit': resolution['base_commit'], 'task_fingerprint': resolution['task_fingerprint']}:
        raise ValueError('Original failed controller seal changed during prerequisite proof.')
    return {'source': identity, 'original_candidate': before, 'files': entries, 'failed_seal_sha256': _hash(seal),
            'prior_review': _prior_review(config, db, project, task),
            'source_task_scope': source_task.scope, 'source_task_out_of_scope': source_task.out_of_scope}


def _prove_source(config, db, project, task, document, *, retained=True):
    if retained:
        root = Path(document['artifact_root'])
        if (_hash(_plain_bytes(Path(document['receipt_path']))) != document['receipt_sha256']
                or _hash(_plain_bytes(root / 'operator-receipt.raw')) != document['receipt_sha256']
                or json.loads(_plain_bytes(root / 'resolution.json', maximum=32 * 1024 * 1024)) != document
                or _hash(_plain_bytes(Path(document['implementation_path']))) != document['implementation_sha256']
                or _hash(_plain_bytes(Path(document['implementation_path']).parent / 'blocked-candidate.json')) != document['prerequisite']['failed_seal_sha256']):
            raise CandidateStateUnavailable('Prerequisite receipt, reservation or original failed evidence changed.')
    prior = document['prerequisite'].get('prior_review')
    if prior and _hash(_plain_bytes(Path(prior['path']))) != prior['sha256']:
        raise CandidateStateUnavailable('Carried prior independent review changed.')
    if prior and _hash(_plain_bytes(Path(prior['events_path']), maximum=256 * 1024 * 1024)) != prior['events_sha256']:
        raise CandidateStateUnavailable('Carried prior independent review event evidence changed.')
    old = document['prerequisite']['source']
    _, _, current = _source(config, db, project, task, document['base_commit'], old['proof'])
    if current != old:
        raise CandidateStateUnavailable('Prerequisite provenance changed after reservation.')


def reserve_handoff(config, db, connection, project, task, document):
    _prove_source(config, db, project, task, document, retained=False)
    source = document['prerequisite']['source']['proof']
    if connection.execute('SELECT 1 FROM prerequisite_receipts WHERE receipt_sha256=? OR (source_registration=? AND source_task=? AND source_candidate=?)',
                          (document['receipt_sha256'], source['source_registration'], source['source_task_id'], source['source_candidate'])).fetchone():
        raise ValueError('Prerequisite authority or receipt has already been used.')
    connection.execute('INSERT INTO prerequisite_receipts VALUES(?,?,?,?,?,?)',
                       (document['receipt_sha256'], source['source_registration'], source['source_task_id'], source['source_candidate'], task.id, task.attempts))


def _safe_target(root, name):
    path = root / name
    for parent in (path, *path.parents):
        if parent == root.parent:
            break
        if os.path.lexists(parent):
            info = os.lstat(parent)
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise CandidateStateUnavailable('Prerequisite target traverses a link or reparse point.')
    if not path.resolve().is_relative_to(root.resolve()):
        raise CandidateStateUnavailable('Prerequisite target escaped its owned checkout.')
    return path


def _import_file(path, payload):
    """Small restart seam: only a previously journaled exact old/new regular file is mutated."""
    if payload is None:
        if path.exists():
            path.unlink()
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + '.forge-prerequisite.tmp')
        _safe_target(path.parent, temporary.name)
        if temporary.exists() and not payload.startswith(_plain_bytes(temporary, maximum=4 * 1024 * 1024)):
            raise CandidateStateUnavailable('Reserved import temporary bytes are not a journaled payload prefix.')
        with temporary.open('wb') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)


def _event(db, task):
    with db.connect() as connection:
        row = connection.execute("SELECT document_json FROM prerequisite_events WHERE task_id=? AND attempt=? AND event='IMPORTED'", (task.id, task.attempts)).fetchone()
    return json.loads(row[0]) if row else None


def import_handoff(config, db, project, task, document):
    _prove_source(config, db, project, task, document)
    handoff = document['prerequisite']
    root = Path(document['artifact_root'])
    intent_path = root / 'prerequisite-import-intent.json'
    intent = json.dumps(handoff, indent=2, ensure_ascii=False).encode('utf-8')
    _write_immutable(intent_path, intent)
    event = _event(db, task)
    current = candidate_state(config, project, task, document['base_commit'])
    if event:
        if _plain_bytes(root / 'prerequisite-imported.json') != json.dumps(event, indent=2).encode('utf-8'):
            raise CandidateStateUnavailable('Prerequisite imported receipt changed.')
        if _hash(json.dumps(current, ensure_ascii=False).encode('utf-8')) != event['candidate_snapshot_sha256']:
            raise CandidateStateUnavailable('Imported candidate changed before the reserved repair.')
        return
    original = handoff['original_candidate']
    imported = {f['path'] for f in handoff['files']}
    temporary_names = {f['temporary_path'] for f in handoff['files']}
    excluded = imported | temporary_names
    # HEAD/index and every non-imported file are fixed through a partial filesystem import.
    if (current[:3] != original[:3]
            or {n: sha for n, sha in current[4] if n not in excluded} != {n: sha for n, sha in original[4] if n not in excluded}):
        raise CandidateStateUnavailable('Failed candidate changed during prerequisite import.')
    actual = dict(current[4])
    for item in handoff['files']:
        _safe_target(task.worktree_path, item['path'])
        temporary = _safe_target(task.worktree_path, item['temporary_path'])
        if temporary.exists():
            payload = base64.b64decode(item['payload_base64'], validate=True) if item['payload_base64'] is not None else None
            if payload is None or not payload.startswith(_plain_bytes(temporary, maximum=4 * 1024 * 1024)):
                raise CandidateStateUnavailable('Reserved import temporary bytes changed.')
        if actual.get(item['path']) not in (item['before_sha256'], item['after_sha256']):
            raise CandidateStateUnavailable('Prerequisite import found an unjournaled file state.')
    for item in handoff['files']:
        target = _safe_target(task.worktree_path, item['path'])
        payload = base64.b64decode(item['payload_base64'], validate=True) if item['payload_base64'] is not None else None
        # A changed file is detected again immediately before its write.
        observed = _hash(_plain_bytes(target, maximum=4 * 1024 * 1024)) if target.exists() else None
        if observed not in (item['before_sha256'], item['after_sha256']):
            raise CandidateStateUnavailable('Prerequisite file changed during import.')
        _import_file(target, payload)
    verify_locked(config, db, task, document=document, require_event=False)
    current = candidate_state(config, project, task, document['base_commit'])
    if current[:3] != original[:3] or {n: sha for n, sha in current[4] if n not in excluded} != {n: sha for n, sha in original[4] if n not in excluded}:
        raise CandidateStateUnavailable('Candidate changed while importing prerequisite bytes.')
    event = {'continuation_id': document['id'], 'source_candidate': handoff['source']['proof']['source_candidate'],
             'original_candidate_snapshot_sha256': document['candidate_snapshot'],
             'candidate_snapshot_sha256': _hash(json.dumps(current, ensure_ascii=False).encode('utf-8'))}
    event_raw = json.dumps(event, indent=2).encode('utf-8')
    seal = root / 'prerequisite-imported.json'
    _write_immutable(seal, event_raw)
    with db.connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        connection.execute('INSERT INTO prerequisite_events VALUES(?,?,?,?)', (task.id, task.attempts, 'IMPORTED', json.dumps(event)))


def verify_locked(config, db, task, *, document=None, require_event=True, committed=False):
    if document is None and getattr(db, 'prerequisite_finalization', lambda *_: None)(task.id):
        from .finalization import verify_candidate

        return verify_candidate(config, db, task, committed=committed)
    row = getattr(db, 'blocker_continuation', lambda *_: None)(task.id, task.attempts)
    if document is None:
        if not row:
            return
        document = json.loads(row['document_json'])
    if not document.get('prerequisite'):
        return
    project = db.get_project(task.project_id)
    _prove_source(config, db, project, task, document)
    event = _event(db, task)
    if require_event and event is None:
        raise CandidateStateUnavailable('Prerequisite import lacks its durable receipt.')
    if event is not None:
        root = Path(document['artifact_root'])
        if (_plain_bytes(root / 'prerequisite-imported.json') != json.dumps(event, indent=2).encode('utf-8')
                or _plain_bytes(root / 'prerequisite-import-intent.json', maximum=32 * 1024 * 1024)
                != json.dumps(document['prerequisite'], indent=2, ensure_ascii=False).encode('utf-8')):
            raise CandidateStateUnavailable('Append-only prerequisite import evidence changed.')
    if row and row['state'] == 'CLAIMED' and event is not None:
        state = candidate_state(config, project, task, document['base_commit'])
        if _hash(json.dumps(state, ensure_ascii=False).encode('utf-8')) != event['candidate_snapshot_sha256']:
            raise CandidateStateUnavailable('Complete imported candidate changed before the paid repair.')
    committed_tree = _tree(task.worktree_path, 'HEAD') if committed else None
    for item in document['prerequisite']['files']:
        path = _safe_target(task.worktree_path, item['path'])
        actual = _hash(_plain_bytes(path, maximum=4 * 1024 * 1024)) if path.exists() else None
        if path.exists() and os.name != 'nt' and os.stat(path).st_mode & 0o111:
            raise CandidateStateUnavailable('Prerequisite executable mode changed.')
        if actual != item['after_sha256']:
            raise CandidateStateUnavailable('Worker or validation modified a controller-owned prerequisite file.')
        # The index must either retain its original entry or stage the exact imported blob.
        flags = _git(task.worktree_path, 'ls-files', '-v', '-z', '--', item['path']).stdout
        if flags and not flags.startswith('H '):
            raise CandidateStateUnavailable('Prerequisite index visibility flags changed.')
        line = _git(task.worktree_path, 'ls-files', '--stage', '-z', '--', item['path']).stdout
        possible = {''}
        for entry in (item['base_entry'], item['source_entry']):
            if entry:
                possible.add(f"{entry['mode']} {entry['oid']} 0\t{item['path']}\0")
        if line not in possible:
            raise CandidateStateUnavailable('Prerequisite index mode/blob changed.')
        if committed:
            if committed_tree.get(item['path']) != item['source_entry']:
                raise CandidateStateUnavailable('Final committed prerequisite blob/mode differs from its approved source.')


def prompt_context(config, db, task):
    if getattr(db, 'prerequisite_finalization', lambda *_: None)(task.id):
        from .finalization import review_context

        return review_context(config, db, task)
    row = getattr(db, 'blocker_continuation', lambda *_: None)(task.id, task.attempts)
    if not row:
        return ''
    document = json.loads(row['document_json'])
    handoff = document.get('prerequisite')
    if not handoff:
        return ''
    proof = handoff['source']['proof']
    files = [{k: f[k] for k in ('path', 'after_sha256', 'source_entry')} for f in handoff['files']]
    locked = {f['path'] for f in handoff['files']}
    changed = _git(task.worktree_path, 'diff', '--no-renames', '--name-only', '-z', document['base_commit'], '--').stdout.split('\0')
    changed += _git(task.worktree_path, 'ls-files', '-z', '--others', '--exclude-standard').stdout.split('\0')
    worker_paths = sorted(set(changed) - locked - {''})
    if len(worker_paths) > 512:
        raise CandidateStateUnavailable('Worker delta exceeds the bounded handoff review context.')
    return ('\n\nCONTROLLER-OWNED PREREQUISITE HANDOFF\n'
            + json.dumps({'source_task_id': proof['source_task_id'], 'source_candidate': proof['source_candidate'],
                          'gate_candidate': proof['gate_candidate'], 'workflow_id': proof['workflow_id'], 'gate_id': proof['gate_id'],
                          'original_execution_base': document['base_commit'], 'locked_files': files, 'current_worker_changed_paths': worker_paths,
                          'source_scope': handoff['source_task_scope'], 'source_out_of_scope': handoff['source_task_out_of_scope'],
                          'unresolved_prior_review': handoff['prior_review']}, ensure_ascii=False)
            + '\nThe original task contract and scope above are unchanged. These exact prerequisite files are separately '
              'approved controller-owned inputs, not scope granted to this worker. Never edit, delete, rename, change modes, '
              'or stage different bytes for them. If they need changes, report blocked.\n'
              'Review the full original-base..current candidate for correctness and interactions. Attribute the exact locked '
              'base..prerequisite file delta to the separately reviewed prerequisite task; assess all other worker changes '
              'against the original task scope. Inspect staged, unstaged and untracked files. The imported prerequisite '
              'commit may belong to another registered clone and is identified by the pinned file/blob manifest above; '
              'do not substitute the project default branch. Report problems in imported inputs as findings; the original '
              'worker cannot repair locked files. Prior independent findings remain repair/review requirements until verified resolved; '
              'a later blocked implementation did not resolve them. Preserve all historical evidence.')


def preflight_handoff(config, task_id, reason, pins):
    """Public read-only authority: root need not read private state to create a receipt."""
    from .blockers import queue_resolution, candidate_snapshot
    from .db import ForgeDB

    db = ForgeDB(config.database_path, read_only=True)
    task = db.get_task(task_id)
    source_task = db.get_task(pins.source_task) if pins.source_task else None
    if not task or not source_task:
        raise ValueError("Exact source and target task identities are required.")
    project = db.get_project(task.project_id)
    checkpoint = db.execution_checkpoint(task.id)
    source = db.get_project(source_task.project_id)
    if not checkpoint or not source:
        raise ValueError("Task checkpoint and source registration are required.")
    proof = {"source_project_id": source.id, "source_registration": pins.source_registration,
             "source_task_id": source_task.id, "source_candidate": pins.source_candidate,
             "workflow_id": pins.workflow_id, "gate_id": pins.gate_id, "gate_candidate": pins.gate_candidate}
    for name in ("source_candidate", "gate_candidate"):
        require_admission_base_commit(source.repo_path, proof[name])
    a, b = _tree(source.repo_path, checkpoint["base_commit"]), _tree(source.repo_path, pins.source_candidate)
    proof["allowed_paths"] = sorted(n for n in a.keys() | b.keys() if a.get(n) != b.get(n))
    source_checkpoint = db.execution_checkpoint(source_task.id)
    if not source_checkpoint:
        raise ValueError("Prerequisite completion checkpoint is required.")
    proof["source_specification_digest"] = source_checkpoint["specification_digest"]
    proof["source_task_fingerprint"] = task_fingerprint(source_task)
    output = config.evidence_root / project.name / task.id / f"attempt-{task.attempts:02d}" / "implementation" / "implementation.final.json"
    receipt = {"task_id": task.id, "attempt": task.attempts, "candidate_base": checkpoint["base_commit"],
               "candidate_snapshot_sha256": candidate_snapshot(config, project, task, checkpoint["base_commit"]),
               "implementation_sha256": _hash(_plain_bytes(output)), "prerequisite": proof}
    raw = json.dumps(receipt, indent=2).encode("utf-8")
    return queue_resolution(config, task.id, reason, Path("prerequisite-preflight.json"), _hash(raw),
                            prerequisite=True, dry_run=True, _receipt_bytes=raw)


def _prior_review(config, db, project, task):
    """Carry only command-closed, conformance-enforced independent verdicts."""
    from .models import CodexRunResult
    from .orchestrator import enforce_specification_conformance
    from .runner import decode_review_result, detect_review_infrastructure_block, review_command_closure
    from .schema_resources import load_schema

    with db.connect() as connection:
        rows = connection.execute("SELECT * FROM runs WHERE task_id=? ORDER BY id DESC", (task.id,)).fetchall()
    root = config.evidence_root / project.name / task.id
    for row in rows:
        phase = row["phase"]
        if phase != "review" and not (phase.startswith("review-") and phase[7:].isdigit()):
            continue
        directory = Path(row["artifact_dir"])
        if not directory.absolute().is_relative_to(root.absolute()) or directory.name != phase:
            raise ValueError("Prior review provenance is ambiguous.")
        if row["exit_code"] != 0 or not row["completed_at"]:
            continue
        path = directory / f"{phase}.final.json"
        events = directory / f"{phase}.events.jsonl"
        raw = _plain_bytes(path)
        event_raw = _plain_bytes(events, maximum=256 * 1024 * 1024)
        result = CodexRunResult(exit_code=0, final={}, final_path=path, jsonl_path=events,
                                stderr_path=directory / f"{phase}.stderr.log")
        final, _ = decode_review_result(result, load_schema("review-result.schema.json"))
        if (final is None or not review_command_closure(result).certified
                or detect_review_infrastructure_block(result, requested_sandbox=config.review_sandbox,
                                                       sandbox=config.review_sandbox) is not None):
            continue
        final = enforce_specification_conformance(task, final)
        if final["verdict"] == "pass":
            return None
        return {"path": str(path), "sha256": _hash(raw), "events_path": str(events), "events_sha256": _hash(event_raw),
                "source_run_id": row["id"], "findings": final["findings"]}
    return None


def terminal_evidence(config, db, task, artifact_root, base, candidate, changed_files):
    """Expose full and separately attributable committed deltas without private reservation data."""
    if getattr(db, 'prerequisite_finalization', lambda *_: None)(task.id):
        from .finalization import terminal_evidence as finalize_evidence

        return finalize_evidence(config, db, task, artifact_root, base, candidate, changed_files)
    row = getattr(db, 'blocker_continuation', lambda *_: None)(task.id, task.attempts)
    if not row:
        return None
    document = json.loads(row['document_json'])
    handoff = document.get('prerequisite')
    if not handoff:
        return None
    verify_locked(config, db, task, committed=True)
    proof = handoff['source']['proof']
    source = db.get_project(proof['source_project_id'])
    locked = [f['path'] for f in handoff['files']]
    manifest = [{k: f[k] for k in ('path', 'after_sha256', 'base_entry', 'source_entry')} for f in handoff['files']]
    source_patch = _git_bytes(source.repo_path, 'diff', '--no-ext-diff', '--no-textconv', '--binary', base, proof['source_candidate'], '--')
    exclusions = [':(exclude,literal)' + name for name in locked]
    worker_patch = _git_bytes(task.worktree_path, 'diff', '--no-ext-diff', '--no-textconv', '--binary', base, candidate, '--', '.', *exclusions)
    for name, raw in (('prerequisite.patch', source_patch), ('worker-changes.patch', worker_patch)):
        path = artifact_root / name
        if path.exists():
            if _plain_bytes(path, maximum=32 * 1024 * 1024) != raw:
                raise CandidateStateUnavailable('Terminal prerequisite attribution evidence changed.')
        else:
            with path.open('xb') as handle:
                handle.write(raw)
    return {'continuation_id': document['id'], 'source_task_id': proof['source_task_id'],
            'source_registration': proof['source_registration'], 'source_candidate': proof['source_candidate'],
            'source_specification_digest': proof['source_specification_digest'], 'source_task_fingerprint': proof['source_task_fingerprint'],
            'operator_receipt_sha256': document['receipt_sha256'],
            'original_candidate_snapshot_sha256': document['candidate_snapshot'],
            'imported_candidate_snapshot_sha256': _event(db, task)['candidate_snapshot_sha256'],
            'original_implementation_sha256': document['implementation_sha256'],
            'gate_candidate': proof['gate_candidate'], 'workflow_id': proof['workflow_id'], 'gate_id': proof['gate_id'],
            'original_base_commit': base, 'final_candidate_commit': candidate, 'locked_files': manifest,
            'worker_changed_files': [name for name in changed_files if name not in locked],
            'prerequisite_patch_sha256': _hash(source_patch), 'worker_patch_sha256': _hash(worker_patch),
            'carried_review_run_id': handoff['prior_review']['source_run_id'] if handoff['prior_review'] else None}


def _write_immutable(path, raw):
    _safe_target(path.parent, path.name)
    _safe_target(path.parent, path.name + '.pending')
    if path.exists():
        if _plain_bytes(path, maximum=32 * 1024 * 1024) != raw:
            raise CandidateStateUnavailable("Immutable prerequisite journal bytes changed.")
        return
    temporary = path.with_name(path.name + '.pending')
    if temporary.exists() and not raw.startswith(_plain_bytes(temporary, maximum=32 * 1024 * 1024)):
        raise CandidateStateUnavailable("Pending prerequisite journal bytes changed.")
    with temporary.open('wb') as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.link(temporary, path)  # Atomic create-if-absent; never overwrite historical metadata.
    except FileExistsError:
        if _plain_bytes(path, maximum=32 * 1024 * 1024) != raw:
            raise CandidateStateUnavailable("Competing prerequisite journal bytes were refused.")
    temporary.unlink()


def public_attribution(task):
    """Explicit projection for the new handoff operation; existing evidence schemas stay unchanged."""
    result = task.result.get('prerequisite_handoff')
    if not isinstance(result, dict):
        return None
    fields = ('continuation_id', 'source_task_id', 'source_registration', 'source_candidate',
              'source_specification_digest', 'source_task_fingerprint', 'operator_receipt_sha256',
              'original_candidate_snapshot_sha256', 'imported_candidate_snapshot_sha256',
              'original_implementation_sha256', 'gate_candidate', 'workflow_id', 'gate_id',
              'original_base_commit', 'final_candidate_commit', 'worker_changed_files',
              'prerequisite_patch_sha256', 'worker_patch_sha256', 'carried_review_run_id')
    projected = {key: result[key] for key in fields if key in result}
    projected['locked_files'] = []
    for item in result.get('locked_files', []):
        locked = {key: item[key] for key in ('path', 'after_sha256') if key in item}
        for key in ('base_entry', 'source_entry'):
            entry = item.get(key)
            locked[key] = {field: entry[field] for field in ('mode', 'kind', 'oid') if field in entry} if isinstance(entry, dict) else None
        projected['locked_files'].append(locked)
    return projected
