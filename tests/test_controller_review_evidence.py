"""Local review decisions and public evidence only; no model requests or live state."""
from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest
from test_evidence_contract import TASK_ID, _configured, _seed, _task_root, _write
from test_review_infrastructure import (
    INSPECTED_REVIEW_EVENTS, PASSING_REVIEW_FINAL, build_orchestrator, run_result,
)
from test_specification_conformance import CRITERIA, OVERWEIGHTED_PASS_FINAL

from codex_forge.evidence import EvidenceError, evidence_list, evidence_show, sanitize_structured
from codex_forge.gitops import CandidateStateUnavailable
from codex_forge.models import TaskState
from codex_forge.orchestrator import enforce_specification_conformance
from codex_forge.review_evidence import CONTROLLER_REVIEW_FILENAME, retain_controller_review


def _decision(source, model=None, effective=None):
    model = model or PASSING_REVIEW_FINAL
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(json.dumps(model).encode())
    retain_controller_review(source, model, effective or model)
    return source.parent / CONTROLLER_REVIEW_FILENAME


def test_exhausted_raw_pass_retains_actual_controller_findings_for_every_review(tmp_path, monkeypatch):
    o, db, runner, task = build_orchestrator(
        tmp_path, monkeypatch,
        finals={phase: (OVERWEIGHTED_PASS_FINAL, INSPECTED_REVIEW_EVENTS)
                for phase in ('review', 'review-2', 'review-3')},
    )
    task = replace(task, attempts=3, acceptance=list(CRITERIA))
    o._execute_task_body(task)
    assert db.state == TaskState.FAILED
    assert runner.phases == ['implementation', 'review', 'review-repair-1', 'review-2', 'review-repair-2', 'review-3']
    root = o.config.evidence_root / db.project.name / task.id / 'attempt-03'
    expected = enforce_specification_conformance(task, OVERWEIGHTED_PASS_FINAL)
    for phase in ('review', 'review-2', 'review-3'):
        source = root / phase / f'{phase}.final.json'
        model_bytes = source.read_bytes()
        record = json.loads((source.parent / CONTROLLER_REVIEW_FILENAME).read_bytes())
        assert record['source_review_sha256'] == hashlib.sha256(model_bytes).hexdigest()
        assert record['model_verdict'] == 'pass'
        assert record['effective_verdict'] == 'changes_required'
        assert record['findings'] == expected['findings']
        assert record['acceptance_criteria'] == OVERWEIGHTED_PASS_FINAL['acceptance_criteria']
        assert json.loads(model_bytes) == OVERWEIGHTED_PASS_FINAL


@pytest.mark.parametrize('invalid', ['missing', 'malformed', 'schema', 'unclosed'])
def test_untrusted_review_never_publishes_a_controller_decision(tmp_path, monkeypatch, invalid):
    o, _, runner, task = build_orchestrator(tmp_path, monkeypatch, finals={})
    result = run_result(tmp_path / 'review', 'review', PASSING_REVIEW_FINAL, INSPECTED_REVIEW_EVENTS)
    if invalid == 'missing':
        result.final_path.unlink()
    elif invalid == 'malformed':
        result.final_path.write_bytes(b'{')
    elif invalid == 'schema':
        result.final_path.write_bytes(b'{"verdict":"pass"}')
    else:
        result.jsonl_path.write_text(json.dumps({'type': 'item.started', 'item': {
            'id': 'pending', 'type': 'command_execution', 'command': 'pytest', 'status': 'in_progress'
        }}) + '\n', encoding='utf-8')
    monkeypatch.setattr(o, '_retry_or_fail_review_output_unusable', lambda *_, **__: 'unusable')
    assert o._trusted_review_final(task, tmp_path, runner.resolution, result) == (None, 'unusable')
    assert not (result.final_path.parent / CONTROLLER_REVIEW_FILENAME).exists()
    assert runner.phases == []


@pytest.mark.parametrize('failure', ['conflict', 'io'])
def test_retention_fault_fails_closed_without_repair_or_retry(tmp_path, monkeypatch, failure):
    import codex_forge.review_evidence as module

    o, db, runner, task = build_orchestrator(
        tmp_path, monkeypatch, finals={'review': (PASSING_REVIEW_FINAL, INSPECTED_REVIEW_EVENTS)}
    )
    root = o.config.evidence_root / db.project.name / task.id / 'attempt-01/review'
    root.mkdir(parents=True)
    target = root / CONTROLLER_REVIEW_FILENAME
    if failure == 'conflict':
        target.write_bytes(b'old immutable evidence')
    else:
        monkeypatch.setattr(module.os, 'link', lambda *_: (_ for _ in ()).throw(OSError('test disk fault')))
    with pytest.raises(CandidateStateUnavailable) as caught:
        o._execute_task_body(task)
    o._handle_unexpected_failure(task, caught.value)
    assert db.state == TaskState.FAILED
    assert runner.phases == ['implementation', 'review']
    assert 'CANDIDATE_STATE_UNAVAILABLE' in db.last_error
    assert json.loads((root / 'review.final.json').read_bytes()) == PASSING_REVIEW_FINAL
    if failure == 'conflict':
        assert target.read_bytes() == b'old immutable evidence'


def test_identical_concurrent_writers_are_idempotent_and_conflicting_writer_cannot_replace(tmp_path):
    source = tmp_path / 'review.final.json'
    source.write_bytes(json.dumps(PASSING_REVIEW_FINAL).encode())
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: retain_controller_review(source, PASSING_REVIEW_FINAL, PASSING_REVIEW_FINAL), range(12)))
    target = tmp_path / CONTROLLER_REVIEW_FILENAME
    before = target.read_bytes(), target.stat().st_mtime_ns
    retain_controller_review(source, PASSING_REVIEW_FINAL, PASSING_REVIEW_FINAL)
    assert (target.read_bytes(), target.stat().st_mtime_ns) == before
    with pytest.raises(CandidateStateUnavailable):
        retain_controller_review(source, PASSING_REVIEW_FINAL, {**PASSING_REVIEW_FINAL, 'verdict': 'changes_required'})
    assert (target.read_bytes(), target.stat().st_mtime_ns) == before
    assert list(tmp_path.glob('*.pending')) == []


def test_decoded_source_drift_cannot_publish_a_trusted_decision(tmp_path):
    source = tmp_path / 'review.final.json'
    source.write_bytes(json.dumps({**PASSING_REVIEW_FINAL, 'summary': 'changed after decoding'}).encode())
    with pytest.raises(CandidateStateUnavailable):
        retain_controller_review(source, PASSING_REVIEW_FINAL, PASSING_REVIEW_FINAL)
    assert not (tmp_path / CONTROLLER_REVIEW_FILENAME).exists()


def test_competing_different_decisions_publish_exactly_one_complete_document(tmp_path, monkeypatch):
    import codex_forge.review_evidence as module

    source = tmp_path / 'review.final.json'
    source.write_bytes(json.dumps(PASSING_REVIEW_FINAL).encode())
    barrier, link = Barrier(2), module.os.link

    def compete(*args):
        barrier.wait(timeout=10)
        return link(*args)

    monkeypatch.setattr(module.os, 'link', compete)

    def publish(verdict):
        try:
            retain_controller_review(source, PASSING_REVIEW_FINAL, {**PASSING_REVIEW_FINAL, 'verdict': verdict})
        except CandidateStateUnavailable:
            return 'conflict'
        return verdict

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(publish, ('pass', 'changes_required')))
    assert outcomes.count('conflict') == 1
    record = json.loads((tmp_path / CONTROLLER_REVIEW_FILENAME).read_bytes())
    assert record['effective_verdict'] == next(outcome for outcome in outcomes if outcome != 'conflict')
    assert list(tmp_path.glob('*.pending')) == []


@pytest.mark.parametrize('version', ['1.0', '1.1', '1.2', '1.3', '1.4'])
def test_additive_public_kind_preserves_old_review_projection_and_six_availability_keys(tmp_path, version):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path, state=TaskState.FAILED)
    source = _task_root(config) / 'attempt-03/review-3/review-3.final.json'
    model = {**PASSING_REVIEW_FINAL, 'acceptance_criteria': [
        {'criterion': 'Required acceptance', 'met': False, 'evidence': 'not present'}
    ]}
    effective = {**model, 'verdict': 'changes_required', 'findings': [{
        'severity': 'critical', 'file': None, 'line': None,
        'issue': 'Unmet acceptance criterion', 'required_fix': 'Implement required acceptance'
    }]}
    target = _decision(source, model, effective)
    listed = evidence_list(config, TASK_ID, schema_version=version)
    assert set(listed['availability']) == {
        'final_result', 'validation_result', 'review_result', 'patch', 'repair_summary', 'implementation_result'
    }
    assert listed['availability']['review_result'] is True
    new = next(a for a in listed['artifacts'] if a['artifact_kind'] == 'controller_review_result')
    old = next(a for a in listed['artifacts'] if a['artifact_kind'] == 'review_result')
    old_view = evidence_show(config, TASK_ID, old['artifact_id'], schema_version=version)
    assert set(old_view['structured_data']) == {'verdict', 'summary', 'findings'}
    view = evidence_show(config, TASK_ID, new['artifact_id'], schema_version=version)
    assert view['structured_data'] == json.loads(target.read_bytes())
    assert 'Controller verdict: changes_required' in view['formatted_text']
    assert 'Required acceptance: met=False' in view['formatted_text']
    target.unlink()
    assert not any(a['artifact_kind'] == 'controller_review_result'
                   for a in evidence_list(config, TASK_ID)['artifacts'])
    with pytest.raises(EvidenceError):
        evidence_show(config, TASK_ID, new['artifact_id'])


def test_controller_report_projection_is_bounded_allowlisted_and_redacted(tmp_path):
    config, db = _configured(tmp_path)
    _seed(db, tmp_path)
    secret = 'sk-' + 'privatevalue' * 6
    report = {'criterion': 'check ' + secret, 'met': False,
              'evidence': str(config.workspace_root / 'private') + ' ' + secret, 'raw_prompt': 'hidden'}
    document = {'format_version': '1', 'source_review_sha256': 'a' * 64,
                'model_verdict': 'pass', 'effective_verdict': 'changes_required',
                'acceptance_criteria': [report] * 501,
                'binding_requirements': [{'text': secret, 'met': False, 'evidence': secret}],
                'findings': [{'severity': 'high', 'file': str(config.workspace_root / 'private'),
                              'line': 1, 'issue': secret, 'required_fix': secret, 'raw_events': 'hidden'}],
                'raw_prompt': secret, 'thread_id': 'hidden'}
    _write(_task_root(config) / 'attempt-01/review/controller-review.json', json.dumps(document))
    listed = evidence_list(config, TASK_ID)
    artifact = listed['artifacts'][0]
    view = evidence_show(config, TASK_ID, artifact['artifact_id'])
    payload = view['structured_data']
    assert len(payload['acceptance_criteria']) == 500
    assert payload['acceptance_criteria'][0]['met'] is False
    rendered = json.dumps(payload) + view['formatted_text']
    assert secret not in rendered and str(config.workspace_root) not in rendered
    assert 'raw_prompt' not in rendered and 'thread_id' not in rendered and 'raw_events' not in rendered
    assert sanitize_structured('review_result', document, lambda value: value) == {
        'verdict': None, 'summary': None, 'findings': [{k: v for k, v in document['findings'][0].items() if k != 'raw_events'}]
    }


def test_deleted_task_keeps_controller_diagnostic_publicly_readable(tmp_path):
    config, db = _configured(tmp_path)
    task = _seed(db, tmp_path, state=TaskState.FAILED)
    source = _task_root(config) / 'attempt-03/review/review.final.json'
    target = _decision(source)
    original = target.read_bytes()
    artifact = next(a for a in evidence_list(config, TASK_ID)['artifacts']
                    if a['artifact_kind'] == 'controller_review_result')
    assert db.delete_task(task.id)[0] == 'deleted'
    assert target.read_bytes() == original
    shown = evidence_show(config, TASK_ID, artifact['artifact_id'])
    assert shown['structured_data']['source_review_sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()
