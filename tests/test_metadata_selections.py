"""Generic typed policies cannot replace code or reinterpret retained historical input."""
from copy import deepcopy

import pytest

from codex_forge.blockers import _hash
from codex_forge.selections import validate_change


BEFORE = b'metadata = Metadata(warnings=[\n    "keep numeric limitation",\n    "obsolete " "shell pending",\n])\nanswer = 42\n'
SPAN = '    "obsolete " "shell pending",\n'
AFTER = BEFORE.replace(SPAN.encode(), b'')


def policy():
    return {'kind': 'python_literal_list_removal', 'path': 'engine/src/demo.py',
            'before_sha256': _hash(BEFORE), 'after_sha256': _hash(AFTER), 'call': 'Metadata',
            'keyword': 'warnings', 'removed_literal': 'obsolete shell pending', 'old_span': SPAN, 'new_span': ''}


def test_literal_removal_preserves_all_other_ast_and_raw_bytes():
    validate_change(policy(), BEFORE, AFTER)


@pytest.mark.parametrize('attack', ['numeric', 'other_warning', 'call', 'keyword', 'hash', 'extra_key', 'arbitrary_patch',
                                   'test', 'history', 'authority', 'syntax', 'span_extra', 'dynamic_call', 'kwargs_selector'])
def test_python_selection_rejects_code_warnings_and_unapproved_inputs(attack):
    p, before, after = policy(), BEFORE, AFTER
    if attack == 'numeric':
        after = after.replace(b'42', b'43')
        p['after_sha256'] = _hash(after)
    elif attack == 'other_warning':
        p['old_span'] = '    "keep numeric limitation",\n'
        after = before.replace(p['old_span'].encode(), b'')
        p['after_sha256'] = _hash(after)
    elif attack in ('call', 'keyword'):
        p[attack] = 'Wrong'
    elif attack == 'dynamic_call':
        p['call'] = ''
    elif attack == 'kwargs_selector':
        p['keyword'] = None
    elif attack == 'hash':
        p['before_sha256'] = '0' * 64
    elif attack == 'extra_key':
        p['patch'] = 'arbitrary'
    elif attack == 'arbitrary_patch':
        p['kind'] = 'patch'
    elif attack == 'test':
        p['path'] = 'engine/tests/test_demo.py'
    elif attack == 'history':
        p['path'] = 'docs/evidence/old/demo.py'
    elif attack == 'authority':
        p['path'] = 'engine/src/AGENTS.md'
    elif attack == 'syntax':
        before = b'broken syntax!\n' + before
        after = before.replace(SPAN.encode(), b'')
        p.update(before_sha256=_hash(before), after_sha256=_hash(after))
    elif attack == 'span_extra':
        p['old_span'] = SPAN + '])\nanswer = 42\n'
        after = before.replace(p['old_span'].encode(), b'')
        p['after_sha256'] = _hash(after)
    with pytest.raises(ValueError):
        validate_change(p, before, after)


def test_exact_document_spans_and_ambiguity_guards():
    before = b'Heading\nObsolete shell status.\nPreserved learner instructions.\n'
    after = before.replace(b'Obsolete shell status.\n', b'')
    p = {'kind': 'utf8_text_replacements', 'path': 'content/demo.md', 'before_sha256': _hash(before),
         'after_sha256': _hash(after), 'replacements': [{'old': 'Obsolete shell status.\n', 'new': ''}]}
    validate_change(p, before, after)
    for changes in ([{'old': 'Heading', 'new': ''}], p['replacements'] * 2,
                    [{'old': 'not present', 'new': ''}], [{'old': 'x' * 1025, 'new': ''}]):
        bad = deepcopy(p)
        bad['replacements'] = changes
        with pytest.raises(ValueError):
            validate_change(bad, before, after)


def source_selection_fixture(tmp_path, monkeypatch):
    import base64
    import json
    from types import SimpleNamespace

    import codex_forge.selections as module

    worker = {'engine/src/demo.py': BEFORE, 'engine/tests/demo_evidence.md': b'Historical blocked record\n'}
    for name, data in worker.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    manifest_path = 'docs/evidence/transition/selection.json'
    policy_document = {'version': '1', 'capability_candidate': 'w',
                       'prospective_worker_inputs': {name: _hash(data) for name, data in worker.items()},
                       'selections': [policy()]}
    payloads = {'w-ui': b'Gated capability\n', 'module': AFTER, 'history': worker['engine/tests/demo_evidence.md'],
                'manifest': json.dumps(policy_document).encode(), 'extra': b'Unreviewed unrelated change\n'}

    def entry(oid):
        return {'mode': '100644', 'kind': 'blob', 'oid': oid}
    capability_tree = {'client/ui.cs': entry('w-ui')}
    source_tree = {**capability_tree, 'engine/src/demo.py': entry('module'),
                   'engine/tests/demo_evidence.md': entry('history'), manifest_path: entry('manifest')}
    authority = {'manifest_path': manifest_path, 'manifest_sha256': _hash(payloads['manifest']),
                 'policy': policy_document, 'capability': {'source_candidate': 'w'}}
    identity = {'proof': authority['capability']}
    monkeypatch.setattr('codex_forge.finalization._checked_source', lambda *args: (SimpleNamespace(repo_path=tmp_path), None, identity))
    monkeypatch.setattr(module, '_tree', lambda repo, commit: capability_tree if commit == 'w' else {})
    monkeypatch.setattr(module, '_blob', lambda repo, oid: payloads[oid])
    state = ['head', 'branch', 'index', 'status', list((name, _hash(data)) for name, data in worker.items()), []]
    task = SimpleNamespace(worktree_path=tmp_path)
    args = (None, None, None, task, 'base', SimpleNamespace(repo_path=tmp_path), source_tree, state, sorted(worker), authority)
    retained = {name: {'before_base64': base64.b64encode(data).decode()} for name, data in worker.items()}
    return module, args, payloads, capability_tree, source_tree, authority, retained


def test_source_selection_preserves_whole_capability_and_unselected_history(tmp_path, monkeypatch):
    module, args, *_ = source_selection_fixture(tmp_path, monkeypatch)
    result = module.prepare(*args)
    assert list(result['selected']) == ['engine/src/demo.py']
    assert module.prepare(*args, retained=result['selected']) == result


@pytest.mark.parametrize('attack', ['capability_change', 'history_change', 'unrelated', 'old_evidence', 'worker_owned_by_w',
                                   'missing_worker', 'mode', 'manifest_hash', 'unselected', 'extra_policy', 'before_bytes'])
def test_source_selection_rejects_undeclared_or_unsealed_authority(tmp_path, monkeypatch, attack):
    module, args, payloads, capability_tree, source_tree, authority, retained = source_selection_fixture(tmp_path, monkeypatch)
    entry = {'mode': '100644', 'kind': 'blob', 'oid': 'extra'}
    if attack == 'capability_change':
        source_tree['client/ui.cs'] = entry
    elif attack == 'history_change':
        source_tree['engine/tests/demo_evidence.md'] = entry
    elif attack == 'unrelated':
        source_tree['client/unrelated.cs'] = entry
    elif attack == 'old_evidence':
        capability_tree['docs/evidence/transition/old.md'] = {'mode': '100644', 'kind': 'blob', 'oid': 'history'}
        source_tree['docs/evidence/transition/old.md'] = entry
    elif attack == 'worker_owned_by_w':
        capability_tree['engine/src/demo.py'] = source_tree['engine/src/demo.py']
    elif attack == 'missing_worker':
        del source_tree['engine/tests/demo_evidence.md']
    elif attack == 'mode':
        source_tree['engine/src/demo.py'] = {**source_tree['engine/src/demo.py'], 'mode': '100755'}
    elif attack == 'manifest_hash':
        authority['manifest_sha256'] = '0' * 64
    elif attack == 'unselected':
        authority['policy']['prospective_worker_inputs']['unselected.py'] = '0' * 64
    elif attack == 'extra_policy':
        authority['policy']['arbitrary_patch'] = 'malicious'
    elif attack == 'before_bytes':
        (args[3].worktree_path / 'engine/src/demo.py').write_bytes(BEFORE + b'# external drift\n')
    if attack in ('unselected', 'extra_policy'):
        import json
        payloads['manifest'] = json.dumps(authority['policy']).encode()
        authority['manifest_sha256'] = _hash(payloads['manifest'])
    with pytest.raises(ValueError):
        module.prepare(*args)
