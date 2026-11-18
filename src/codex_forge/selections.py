"""Typed, byte-exact metadata selection from an independently reviewed source.

There is no general patch/code replacement policy. Python permits one literal
list-element removal; document text permits only bounded explicit UTF8 spans.
"""
from __future__ import annotations

import ast
import base64
import copy
import json
from pathlib import PurePosixPath

from .blockers import _hash, _plain_bytes
from .prerequisites import _blob, _path, _safe_target, _tree


def _call_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return parent + '.' + node.attr if parent else ''
    return ''


def _text(value, *, maximum=4096):
    if not isinstance(value, str) or len(value.encode('utf-8')) > maximum or '\0' in value:
        raise ValueError('Selection text must be bounded UTF8 without NULs.')
    return value.encode('utf-8')


def _plain_document_path(name):
    _path(name)
    path = PurePosixPath(name)
    if (any(part.casefold() in ('tests', 'test', 'evidence', 'artifacts', 'logs') for part in path.parts)
            or path.name.casefold().startswith(('test_', 'test.')) or 'evidence' in path.name.casefold()):
        raise ValueError('Historical evidence and test paths cannot be selected.')
    return path


def validate_change(policy, before, after):
    """Validate an exact typed transformation; return nothing and never edit files."""
    common = {'kind', 'path', 'before_sha256', 'after_sha256'}
    if not isinstance(policy, dict) or not common <= set(policy):
        raise ValueError('A complete typed metadata policy is required.')
    path = _plain_document_path(policy['path'])
    if (len(before) > 1024 * 1024 or len(after) > 1024 * 1024
            or _hash(before) != policy['before_sha256'] or _hash(after) != policy['after_sha256']):
        raise ValueError('Selection before/after bytes do not match their exact approved hashes.')
    before.decode('utf-8')
    after.decode('utf-8')
    if policy['kind'] == 'python_literal_list_removal':
        if set(policy) != common | {'call', 'keyword', 'removed_literal', 'old_span', 'new_span'} or path.suffix != '.py':
            raise ValueError('Python selection requires an exact call/keyword/literal removal policy.')
        if (not isinstance(policy['call'], str) or not policy['call']
                or not all(part.isidentifier() for part in policy['call'].split('.'))
                or not isinstance(policy['keyword'], str) or not policy['keyword'].isidentifier()
                or not isinstance(policy['removed_literal'], str)):
            raise ValueError('Python call/keyword selectors require explicit plain names and a literal string.')
        old, new = _text(policy['old_span']), _text(policy['new_span'])
        if not old or new or before.count(old) != 1 or before.replace(old, b'', 1) != after:
            raise ValueError('Python selection must remove only its one exact byte span.')
        try:
            expected = ast.parse(before.decode('utf-8'))
            observed = ast.parse(after.decode('utf-8'))
        except SyntaxError as error:
            raise ValueError('Selected Python input is not valid syntax.') from error
        matches = []
        for node in ast.walk(expected):
            if not isinstance(node, ast.Call) or _call_name(node.func) != policy['call']:
                continue
            for keyword in node.keywords:
                if keyword.arg == policy['keyword']:
                    matches.append(keyword.value)
        if len(matches) != 1 or not isinstance(matches[0], ast.List):
            raise ValueError('Python call/keyword selector must identify exactly one literal list.')
        selected = matches[0]
        if not all(isinstance(node, ast.Constant) and isinstance(node.value, str) for node in selected.elts):
            raise ValueError('Python metadata list contains executable/nonliteral elements.')
        targets = [node for node in selected.elts if node.value == policy['removed_literal']]
        if len(targets) != 1:
            raise ValueError('Exact removed literal must occur exactly once in the selected list.')
        target = targets[0]
        lines = before.splitlines(keepends=True)
        start = sum(len(line) for line in lines[:target.lineno - 1]) + target.col_offset
        end = sum(len(line) for line in lines[:target.end_lineno - 1]) + target.end_col_offset
        span_start = before.index(old)
        span_end = span_start + len(old)
        if not (span_start <= start < end <= span_end):
            raise ValueError('Removal span does not cover only the selected literal syntax.')
        padding = before[span_start:start] + before[end:span_end]
        if any(byte not in b' \t\r\n,' for byte in padding) or padding.count(b',') > 1:
            raise ValueError('Removal span includes tokens outside the selected literal.')
        selected.elts.remove(target)
        if ast.dump(expected, include_attributes=False) != ast.dump(observed, include_attributes=False):
            raise ValueError('Python AST changed beyond the one approved literal removal.')
    elif policy['kind'] == 'utf8_text_replacements':
        if set(policy) != common | {'replacements'} or path.suffix.casefold() not in ('.md', '.txt'):
            raise ValueError('Text selection is restricted to declared document spans.')
        operations = policy['replacements']
        if not isinstance(operations, list) or not 1 <= len(operations) <= 8:
            raise ValueError('Document selection requires one to eight bounded exact replacements.')
        result, spans, total = before, [], 0
        for operation in operations:
            if not isinstance(operation, dict) or set(operation) != {'old', 'new'}:
                raise ValueError('Each document operation needs exactly old/new UTF8 text.')
            old, new = _text(operation['old'], maximum=1024), _text(operation['new'], maximum=1024)
            if not old or before.count(old) != 1:
                raise ValueError('Each original document span must occur exactly once.')
            start = before.index(old)
            spans.append((start, start + len(old), new))
            total += len(old) + len(new)
        spans.sort()
        if total > 8192 or any(a[1] > b[0] for a, b in zip(spans, spans[1:])):
            raise ValueError('Document spans overlap or exceed the finite selection bound.')
        for start, end, new in reversed(spans):
            result = result[:start] + new + result[end:]
        if result != after:
            raise ValueError('Document changed beyond its approved exact byte replacements.')
    else:
        raise ValueError('Unknown selection policy; arbitrary patches/code are not supported.')


def prepare(config, db, project, task, base, source, source_tree, state, worker, authority, *, retained=None):
    """Prove C preserves gated W and current worker files except typed selected metadata."""
    if not isinstance(authority, dict) or set(authority) != {'manifest_path', 'manifest_sha256', 'policy', 'capability'}:
        raise ValueError('Selection requires an exact versioned manifest and independently gated capability.')
    manifest_path = _path(authority['manifest_path'])
    prefix = str(PurePosixPath(manifest_path).parent) + '/'
    if not manifest_path.startswith('docs/evidence/') or prefix == 'docs/evidence/':
        raise ValueError('Selection manifest requires its own new evidence directory.')
    entry = source_tree.get(manifest_path)
    if not entry or entry['mode'] != '100644' or entry['kind'] != 'blob':
        raise ValueError('Selection manifest is not an exact committed regular file.')
    raw = _blob(source.repo_path, entry['oid'])
    if _hash(raw) != authority['manifest_sha256'] or json.loads(raw) != authority['policy']:
        raise ValueError('Selection policy differs from its reviewed source blob.')
    policy = authority['policy']
    if (not isinstance(policy, dict) or set(policy) != {'version', 'capability_candidate', 'prospective_worker_inputs', 'selections'}
            or policy['version'] != '1' or policy['capability_candidate'] != authority['capability']['source_candidate']):
        raise ValueError('Selection manifest shape/capability identity is unsupported.')
    from .finalization import _checked_source

    capability, _, identity = _checked_source(config, db, project, task, base, authority['capability'])
    capability_tree, base_tree = _tree(capability.repo_path, policy['capability_candidate']), _tree(source.repo_path, base)
    if any(name in capability_tree for name in identity.get('gate_extra_paths', [])):
        raise ValueError('Capability gate modified/deleted retained source evidence.')
    original = dict(state[4])
    if policy['prospective_worker_inputs'] != {name: original.get(name) for name in worker}:
        raise ValueError('Selection must bind every current prospective worker input, not a historical tree.')
    selections = policy['selections']
    if not isinstance(selections, list) or not 1 <= len(selections) <= 2:
        raise ValueError('Selection is limited to one or two explicitly typed metadata files.')
    selected = {}
    for operation in selections:
        if not isinstance(operation, dict) or operation.get('path') not in worker or operation['path'] in selected:
            raise ValueError('Selection must name distinct existing original worker-scope inputs.')
        name = operation['path']
        if not original.get(name) or name not in source_tree or source_tree[name]['mode'] != '100644':
            raise ValueError('Selection cannot invent/remove a worker input or change its mode.')
        before = (base64.b64decode(retained[name]['before_base64'], validate=True) if retained is not None
                  else _plain_bytes(_safe_target(task.worktree_path, name)))
        if _hash(before) != original[name]:
            raise ValueError('Prospective worker bytes changed during selection proof.')
        after = _blob(source.repo_path, source_tree[name]['oid'])
        validate_change(operation, before, after)
        selected[name] = {'policy': copy.deepcopy(operation), 'before_base64': base64.b64encode(before).decode('ascii')}
    changed = sorted(n for n in capability_tree.keys() | source_tree.keys() if capability_tree.get(n) != source_tree.get(n))
    for name in set(worker):
        if capability_tree.get(name) != base_tree.get(name):
            raise ValueError('Capability source cannot own the original task worker delta.')
        if name not in source_tree or source_tree[name]['mode'] != '100644':
            raise ValueError('Reconciliation source must retain all original worker inputs.')
        if name not in selected and _hash(_blob(source.repo_path, source_tree[name]['oid'])) != original.get(name):
            raise ValueError('Unselected worker/test/history bytes changed in reconciliation source.')
    for name in changed:
        if name in worker:
            continue
        _path(name)
        if (name in capability_tree or not name.startswith(prefix) or name not in source_tree
                or source_tree[name]['mode'] != '100644' or source_tree[name]['kind'] != 'blob'):
            raise ValueError('Reconciliation source changed capability/baseline/history outside its new evidence directory.')
    return {'authority': authority, 'capability_identity': identity, 'selected': selected}
