"""Independent CLI/HTTP/restart acceptance check; never enables live dialing."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVICE = ROOT / "voice-agent-harness"
KEY = "acceptance-local-only-" + "x" * 32


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run():
    (ROOT / "tmp").mkdir(exist_ok=True)
    control, media = port(), port()
    while media == control:
        media = port()
    base = f"http://127.0.0.1:{control}"

    def request(path, body=None, idem=None, key=KEY):
        headers = {"Authorization": "Bearer " + key}
        if idem:
            headers["Idempotency-Key"] = idem
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(base + path, data=data, headers=headers)
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=3) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    with tempfile.TemporaryDirectory(prefix="vah-acceptance-", dir=ROOT / "tmp") as directory:
        task_dir = Path(directory)
        env = os.environ.copy()
        env.update(
            PYTHONPATH=str(SERVICE / "src"),
            CONTROL_HOST="127.0.0.1",
            CONTROL_PORT=str(control),
            MEDIA_HOST="127.0.0.1",
            MEDIA_PORT=str(media),
            PUBLIC_STREAM_BASE_URL="wss://bridge.example.com",
            BRIDGE_API_KEY=KEY,
            ALLOWED_VOICES="NATF2.pt",
            DEFAULT_VOICE="NATF2.pt",
            DATABASE_PATH=str(task_dir / "calls.sqlite3"),
            VIH_DIAL_MODE="mock",
            ENABLE_LIVE_DIALS="false",
        )
        env.pop("CONTROLLER_API_KEY", None)
        env.pop("CONTROLLER_CALLBACK_BASE_URL", None)

        def start(log):
            process = subprocess.Popen(
                [sys.executable, "-c", "from voice_harness.main import run; run()"],
                cwd=SERVICE,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise AssertionError("CLI exited before readiness")
                    try:
                        status, _ = request("/health/live")
                        if status == 200:
                            return process
                    except (OSError, ValueError):
                        pass
                    time.sleep(0.1)
                raise AssertionError("CLI did not start within 15s")
            except BaseException:
                stop(process)
                raise

        def stop(process):
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

        payload = {
            "leadId": "lead-local-acceptance",
            "customerPhone": "+919876543210",
            "leadContext": {
                "customerName": "Acceptance Customer",
                "companyName": "Acceptance Company",
                "customerEmail": "acceptance@example.com",
                "source": "Website",
                "enquiryNotes": "Synthetic local acceptance facts",
            },
        }
        idem = "acceptance-" + uuid.uuid4().hex
        with (task_dir / "server.log").open("w", encoding="utf-8") as log:
            process = start(log)
            try:
                assert request("/health/live", key="invalid")[0] == 401
                status, created = request("/v1/calls", payload, idem)
                assert status == 202, (status, created)
                call_id = created["callId"]
                status, replay = request("/v1/calls", payload, idem)
                assert status == 202 and replay["callId"] == call_id
                changed = {**payload, "leadContext": {**payload["leadContext"], "source": "Google"}}
                assert request("/v1/calls", changed, idem)[0] == 409
                status, result = request("/v1/calls/" + call_id)
                assert status == 200 and result["simulated"] is True
                assert result["complete"] is True
                assert result["customerTranscript"] is None
                assert KEY not in json.dumps(result)
            finally:
                stop(process)
            process = start(log)
            try:
                status, restored = request("/v1/calls/" + call_id)
                assert status == 200 and restored["callId"] == call_id
                assert restored["complete"] is True and restored["simulated"] is True
                status, replay = request("/v1/calls", payload, idem)
                assert status == 202 and replay["callId"] == call_id
                log.flush()
                log_text = (task_dir / "server.log").read_text(encoding="utf-8")
                events = [json.loads(line) for line in log_text.splitlines()]
                assert any(event["event"] == "service_starting" for event in events)
                assert any(event["event"] == "call_created" and event["call_id"] == call_id for event in events)
                assert any(event["event"] == "api_request_failed" and event["status_code"] == 401 for event in events)
                for private in (KEY, idem, payload["customerPhone"], payload["leadContext"]["customerName"],
                                payload["leadContext"]["companyName"], payload["leadContext"]["customerEmail"],
                                payload["leadContext"]["enquiryNotes"],
                                "bridge.example.com", "Traceback"):
                    assert private not in log_text, "sensitive data appeared in CLI logs"
                print(json.dumps({
                    "checks": ["actual CLI HTTP", "no controller config", "auth rejection",
                               "idempotent replay", "conflict rejection", "raw JSON result",
                               "restart persistence", "replay after restart", "safe structured CLI logs"],
                    "result": restored,
                }, indent=2))
            finally:
                stop(process)


if __name__ == "__main__":
    run()
