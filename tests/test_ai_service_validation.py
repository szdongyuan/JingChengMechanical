"""Tests for AiService response validation."""
from __future__ import annotations

import json

import pytest

from app.service.ai_service import AiService


@pytest.fixture
def ai_service():
    return AiService()


def _write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


class TestValidateResponse:
    def test_missing_file(self, ai_service, tmp_path):
        result = ai_service.validate_response(
            str(tmp_path / "nonexistent.json"), "req_1"
        )
        assert result["status"] == "protocol_error"

    def test_invalid_json(self, ai_service, tmp_path):
        p = str(tmp_path / "bad.json")
        with open(p, "w") as f:
            f.write("not json{{{")
        result = ai_service.validate_response(p, "req_1")
        assert result["status"] == "invalid_output"

    def test_root_not_object(self, ai_service, tmp_path):
        p = str(tmp_path / "arr.json")
        with open(p, "w") as f:
            json.dump([1, 2, 3], f)
        result = ai_service.validate_response(p, "req_1")
        assert result["status"] == "invalid_output"

    def test_protocol_version_mismatch(self, ai_service, tmp_path):
        p = str(tmp_path / "resp.json")
        _write_json(p, {"protocol_version": 2, "request_id": "req_1",
                        "ai_result": {"ai_label": "PASS"}})
        result = ai_service.validate_response(p, "req_1")
        assert result["status"] == "protocol_error"

    def test_request_id_mismatch(self, ai_service, tmp_path):
        p = str(tmp_path / "resp.json")
        _write_json(p, {"protocol_version": 1, "request_id": "wrong",
                        "ai_result": {"ai_label": "PASS"}})
        result = ai_service.validate_response(p, "req_1")
        assert result["status"] == "protocol_error"

    def test_missing_ai_result(self, ai_service, tmp_path):
        p = str(tmp_path / "resp.json")
        _write_json(p, {"protocol_version": 1, "request_id": "req_1"})
        result = ai_service.validate_response(p, "req_1")
        assert result["status"] == "protocol_error"

    def test_missing_ai_label(self, ai_service, tmp_path):
        p = str(tmp_path / "resp.json")
        _write_json(p, {"protocol_version": 1, "request_id": "req_1",
                        "ai_result": {"score": 0.9}})
        result = ai_service.validate_response(p, "req_1")
        assert result["status"] == "protocol_error"

    def test_success_full_response(self, ai_service, tmp_path):
        p = str(tmp_path / "resp.json")
        _write_json(p, {
            "protocol_version": 1,
            "request_id": "req_1",
            "toolchain_name": "Motor AI",
            "toolchain_version": "1.0",
            "ai_result": {
                "ai_label": "PASS",
                "score": 0.93,
                "summary": "OK",
                "confidence": 0.88,
                "artifacts": [{"type": "report_json", "relative_path": "report.json"}],
                "extra": {"cluster_id": 3},
            },
        })
        result = ai_service.validate_response(p, "req_1")
        assert result["status"] == "success"
        assert result["ai_result"]["ai_label"] == "PASS"
        assert result["ai_result"]["score"] == 0.93
        assert result["toolchain_name"] == "Motor AI"
        assert result["toolchain_version"] == "1.0"

    def test_invalid_score_type(self, ai_service, tmp_path):
        p = str(tmp_path / "resp.json")
        _write_json(p, {"protocol_version": 1, "request_id": "req_1",
                        "ai_result": {"ai_label": "PASS", "score": "not_a_number"}})
        result = ai_service.validate_response(p, "req_1")
        assert result["status"] == "invalid_output"

    def test_bool_score_rejected(self, ai_service, tmp_path):
        """bool is subclass of int but should be rejected for score/confidence."""
        p = str(tmp_path / "resp.json")
        _write_json(p, {"protocol_version": 1, "request_id": "req_1",
                        "ai_result": {"ai_label": "PASS", "score": True}})
        result = ai_service.validate_response(p, "req_1")
        assert result["status"] == "invalid_output"

    def test_non_string_ai_label_rejected(self, ai_service, tmp_path):
        p = str(tmp_path / "resp.json")
        _write_json(p, {"protocol_version": 1, "request_id": "req_1",
                        "ai_result": {"ai_label": 123}})
        result = ai_service.validate_response(p, "req_1")
        assert result["status"] == "invalid_output"

    def test_artifact_path_traversal_rejected(self, ai_service, tmp_path):
        p = str(tmp_path / "resp.json")
        _write_json(p, {"protocol_version": 1, "request_id": "req_1",
                        "ai_result": {"ai_label": "PASS",
                                      "artifacts": [{"type": "report", "relative_path": "../escape.txt"}]}})
        result = ai_service.validate_response(p, "req_1")
        assert result["status"] == "invalid_output"

    def test_artifacts_not_list_rejected(self, ai_service, tmp_path):
        p = str(tmp_path / "resp.json")
        _write_json(p, {"protocol_version": 1, "request_id": "req_1",
                        "ai_result": {"ai_label": "PASS", "artifacts": "not_a_list"}})
        result = ai_service.validate_response(p, "req_1")
        assert result["status"] == "invalid_output"

    def test_artifact_absolute_path_rejected(self, ai_service, tmp_path):
        p = str(tmp_path / "resp.json")
        _write_json(p, {"protocol_version": 1, "request_id": "req_1",
                        "ai_result": {"ai_label": "PASS",
                                      "artifacts": [{"type": "report", "relative_path": "/tmp/escape.txt"}]}})
        result = ai_service.validate_response(p, "req_1")
        assert result["status"] == "invalid_output"
