from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from nff_diagnosis.capture.parser import ESP32PanicParser
from nff_diagnosis.schemas import CrashRecord
from nff_diagnosis.schemas.diagnosis_output import DiagnosisOutput
from nff_diagnosis.server.app import create_app


def _mock_anthropic_factory(text: str = "Root cause narrative."):
    msg = MagicMock()
    msg.content = [MagicMock(text=text)]
    client = MagicMock()
    client.messages.create.return_value = msg
    return MagicMock(return_value=client)


@pytest.fixture()
def client(tmp_path: Path):
    with patch("nff_diagnosis.engine.diagnosis.anthropic.Anthropic", _mock_anthropic_factory()):
        app = create_app(
            store_root=tmp_path / "store",
            db_path=tmp_path / "test.db",
            api_key="fake",
        )
        with TestClient(app) as c:
            yield c


def _crash_body(raw_fixture: str) -> str:
    fields = ESP32PanicParser().parse(raw_fixture)
    fields["build_id"] = "fakebuildid"
    fields["raw"] = raw_fixture
    return CrashRecord(**fields).model_dump_json()


def test_ingest_watchdog(client: TestClient, watchdog_raw: str):
    resp = client.post(
        "/ingest",
        content=_crash_body(watchdog_raw),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 201
    diag = DiagnosisOutput.model_validate(resp.json())
    assert diag.crash_class == "watchdog"
    assert diag.confidence >= 0.95


def test_ingest_null_deref(client: TestClient, null_deref_raw: str):
    resp = client.post(
        "/ingest",
        content=_crash_body(null_deref_raw),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 201
    diag = DiagnosisOutput.model_validate(resp.json())
    assert diag.crash_class == "null_deref"
    assert diag.engine_version == "nff-diagnosis/0.1.0"


def test_ingest_stores_crash(client: TestClient, watchdog_raw: str, tmp_path: Path):
    with patch("nff_diagnosis.engine.diagnosis.anthropic.Anthropic", _mock_anthropic_factory()):
        app = create_app(
            store_root=tmp_path / "store2",
            db_path=tmp_path / "test2.db",
            api_key="fake",
        )
        with TestClient(app) as c:
            body = _crash_body(watchdog_raw)
            resp = c.post("/ingest", content=body, headers={"Content-Type": "application/json"})
            diag = DiagnosisOutput.model_validate(resp.json())
            storage = app.state.storage
            assert storage.get_crash(diag.crash_id) is not None
            assert storage.get_diagnosis(diag.crash_id) is not None
