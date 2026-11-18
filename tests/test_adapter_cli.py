import json

from codex_forge.adapter import cli


def test_invalid_request_returns_nonzero_json(tmp_path, capsys):
    path = tmp_path / "bad.json"
    path.write_text('{"x": 1, "x": 2}')
    assert cli.main(["run", "--request", str(path)]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "request_input"


def test_run_returns_single_result_and_restores_signal(tmp_path, capsys, monkeypatch):
    path = tmp_path / "job.json"
    path.write_text('{}')
    calls = []

    def run(data, **kwargs):
        calls.append((data, kwargs))
        return {"adapter_status": "completed", "factory_acceptance": "not_determined"}

    monkeypatch.setattr(cli, "run_job", run)
    assert cli.main(["run", "--request", str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["adapter_status"] == "completed"
    assert len(calls) == 1 and calls[0][1]["cancel_event"] is not None


def test_json_output_works_on_ascii_console(tmp_path, capsys, monkeypatch):
    path = tmp_path / "job.json"
    path.write_text('{}')
    monkeypatch.setattr(cli, "run_job", lambda *_a, **_kw: {"adapter_status": "partial", "text": "\u03c0"})
    assert cli.main(["run", "--request", str(path)]) == 2
    output = capsys.readouterr().out
    assert output.isascii()
    assert json.loads(output)["text"] == "\u03c0"


def test_invalid_unicode_final_is_a_protocol_error():
    from codex_forge.adapter.contract import RequestError, parse_final
    import pytest
    raw = json.dumps({"status": "done", "summary": "\ud800", "blockers": [], "decisions": [],
                      "assumptions": [], "patterns": []})
    with pytest.raises(RequestError) as caught:
        parse_final(raw)
    assert caught.value.code == "invalid_final"
