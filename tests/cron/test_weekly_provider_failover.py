import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from cron import scheduler
from cron.jobs import create_job, get_job


def test_weekly_policy_is_scoped(monkeypatch):
    from cron.weekly import weekly_policy
    from hermes_cli.fallback_config import WEEKLY_SCRIPT_ENV, cli_fallback_chain

    fallback = [{"provider": "custom", "model": "sol"}]
    global_fallback = [{"provider": "custom", "model": "other"}]
    policy = {"providers": fallback, "chat_script_job_ids": ["recap"]}
    config = {
        "cron": {"weekly_provider_failover": policy},
        "fallback_providers": global_fallback,
    }
    weekly = {"schedule": {"kind": "cron", "expr": "0 9 * * 1"}}
    daily = {"schedule": {"kind": "cron", "expr": "0 9 * * *"}}
    assert weekly_policy(weekly, config) == policy
    assert weekly_policy(daily, config) == {}
    assert weekly_policy({**weekly, "no_agent": True, "id": "backup"}, config) == {}
    assert weekly_policy({**weekly, "no_agent": True, "id": "recap"}, config) == policy
    monkeypatch.delenv(WEEKLY_SCRIPT_ENV, raising=False)
    assert cli_fallback_chain(config) == global_fallback
    monkeypatch.setenv(WEEKLY_SCRIPT_ENV, "1")
    assert cli_fallback_chain(config) == fallback


@pytest.mark.parametrize("statuses", [(429, 200), (503, 429, 200), (429, 503, 429)])
def test_weekly_cron_recovers_or_dispatches_once(tmp_path, monkeypatch, statuses):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_TEST_MODE", "1")
    monkeypatch.setenv("HERMES_QUIET", "1")
    seen = []
    models = ["claude-opus-5-5", "gpt-6.2-sol", "gemini-3.8-flash-high"]
    codes = dict(zip(models, statuses))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            model = body["model"]
            seen.append(model)
            code = codes[model]
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if code == 200:
                data = {
                    "id": "test",
                    "object": "chat.completion",
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": "PROBE OK"},
                        "finish_reason": "stop",
                    }],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
                }
            elif code == 503:
                data = {"error": {"message": "auth_unavailable: last upstream error: rate_limit_error"}}
            else:
                data = {"error": {"message": "All credentials are cooling down"}}
            self.wfile.write(json.dumps(data).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    sink = tmp_path / "dispatch.jsonl"
    script = tmp_path / "fake_dispatch.py"
    script.write_text(
        "import sys\nfrom pathlib import Path\n"
        "with Path(sys.argv[1]).open('a') as out:\n    out.write(sys.stdin.read())\n"
    )
    url = f"http://127.0.0.1:{server.server_port}/v1"
    config = {
        "model": {"provider": "custom", "default": models[0], "base_url": url},
        "agent": {"api_max_retries": 1},
        "cron": {"weekly_provider_failover": {
            "providers": [
                {"provider": "custom", "model": model, "base_url": url, "api_key": "fake"}
                for model in models[1:]
            ],
            "failure_command": [sys.executable, str(script), str(sink)],
        }},
    }
    (tmp_path / "config.yaml").write_text(json.dumps(config))
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    monkeypatch.setenv("OPENAI_BASE_URL", url)
    deliveries = []
    monkeypatch.setattr(scheduler, "_deliver_result", lambda job, text, **_: deliveries.append(text))
    try:
        job = create_job(prompt="Say PROBE OK.", schedule="0 9 * * 1", deliver="local")
        job["enabled_toolsets"] = []
        assert scheduler.run_one_job(job)
        assert list(dict.fromkeys(seen)) == models[:len(statuses)]
        recorded = get_job(job["id"])
        if statuses[-1] == 200:
            assert recorded["last_status"] == "ok"
            assert not sink.exists()
            assert deliveries == ["PROBE OK"]
        else:
            assert recorded["last_status"] == "error"
            alerts = sink.read_text().splitlines()
            assert len(alerts) == 1
            assert deliveries == []
            assert json.loads(alerts[0])["job_id"] == job["id"]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
