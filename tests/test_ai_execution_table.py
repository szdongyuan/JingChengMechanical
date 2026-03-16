"""Tests for AiExecutionTable model."""
from __future__ import annotations

import pytest
from peewee import IntegrityError

from model.ai_execution_table import AiExecutionTable


class TestAiExecutionTable:
    def test_create_success_record_all_fields(self):
        """Verify every field from the spec can be stored and read back."""
        AiExecutionTable.create(
            request_id="req_001",
            test_run_id="run_001",
            config_id=1,
            toolchain_id="motor-dual-segment-ai",
            module_status="success",
            started_time="2026-03-13T10:00:00+08:00",
            finished_time="2026-03-13T10:00:00.742+08:00",
            duration_ms=742,
            exit_code=0,
            error_summary=None,
            stdout_excerpt="processing...",
            stderr_excerpt="",
            toolchain_name="Motor Dual Segment AI",
            toolchain_version="1.2.0",
            protocol_version=1,
            window_overridden=False,
            default_window_offset_ms=0,
            default_window_duration_ms=3000,
            final_window_offset_ms=0,
            final_window_duration_ms=3000,
            is_current_effective=True,
            ai_label="PASS",
            score=0.93,
            summary="双片段联合分析正常",
            confidence=0.88,
            extra_json='{"cluster_id": 3}',
            artifacts_json='[{"type": "report_json", "relative_path": "report.json"}]',
            raw_response_json='{"protocol_version": 1}',
            request_json_path="D:/data/ai_runs/req_001/request.json",
            response_json_path="D:/data/ai_runs/req_001/response.json",
            artifacts_dir="D:/data/ai_runs/req_001/artifacts",
        )
        fetched = AiExecutionTable.get_by_id("req_001")
        assert fetched.test_run_id == "run_001"
        assert fetched.config_id == 1
        assert fetched.toolchain_id == "motor-dual-segment-ai"
        assert fetched.module_status == "success"
        assert fetched.started_time == "2026-03-13T10:00:00+08:00"
        assert fetched.finished_time == "2026-03-13T10:00:00.742+08:00"
        assert fetched.duration_ms == 742
        assert fetched.exit_code == 0
        assert fetched.error_summary is None
        assert fetched.stdout_excerpt == "processing..."
        assert fetched.stderr_excerpt == ""
        assert fetched.toolchain_name == "Motor Dual Segment AI"
        assert fetched.toolchain_version == "1.2.0"
        assert fetched.protocol_version == 1
        assert fetched.window_overridden is False
        assert fetched.default_window_offset_ms == 0
        assert fetched.default_window_duration_ms == 3000
        assert fetched.final_window_offset_ms == 0
        assert fetched.final_window_duration_ms == 3000
        assert fetched.is_current_effective is True
        assert fetched.ai_label == "PASS"
        assert fetched.score == 0.93
        assert fetched.summary == "双片段联合分析正常"
        assert fetched.confidence == 0.88
        assert fetched.extra_json == '{"cluster_id": 3}'
        assert fetched.artifacts_json == '[{"type": "report_json", "relative_path": "report.json"}]'
        assert fetched.raw_response_json == '{"protocol_version": 1}'
        assert fetched.request_json_path == "D:/data/ai_runs/req_001/request.json"
        assert fetched.response_json_path == "D:/data/ai_runs/req_001/response.json"
        assert fetched.artifacts_dir == "D:/data/ai_runs/req_001/artifacts"

    def test_create_preflight_failure_nullable_fields(self):
        """Pre-launch failures (not_configured, preflight_error) have null paths and AI fields."""
        AiExecutionTable.create(
            request_id="req_002",
            test_run_id="run_001",
            config_id=1,
            toolchain_id="",
            module_status="not_configured",
            error_summary="No AI binding for this config",
            protocol_version=1,
            is_current_effective=False,
        )
        fetched = AiExecutionTable.get_by_id("req_002")
        assert fetched.module_status == "not_configured"
        assert fetched.ai_label is None
        assert fetched.score is None
        assert fetched.summary is None
        assert fetched.confidence is None
        assert fetched.extra_json is None
        assert fetched.artifacts_json is None
        assert fetched.raw_response_json is None
        assert fetched.exit_code is None
        assert fetched.stdout_excerpt is None
        assert fetched.started_time is None
        assert fetched.finished_time is None
        assert fetched.duration_ms is None
        assert fetched.error_summary == "No AI binding for this config"
        assert fetched.is_current_effective is False
        assert fetched.request_json_path is None
        assert fetched.response_json_path is None
        assert fetched.artifacts_dir is None

    def test_create_post_launch_failure_has_paths(self):
        """Post-launch failures (timeout, process_error) still have request/artifacts paths."""
        AiExecutionTable.create(
            request_id="req_003",
            test_run_id="run_001",
            config_id=1,
            toolchain_id="motor-ai",
            module_status="timeout",
            started_time="2026-03-13T10:01:00+08:00",
            finished_time="2026-03-13T10:01:30+08:00",
            duration_ms=30000,
            error_summary="Process timed out after 30s",
            stderr_excerpt="timeout waiting for response",
            protocol_version=1,
            is_current_effective=False,
            request_json_path="D:/data/ai_runs/req_003/request.json",
            response_json_path=None,
            artifacts_dir="D:/data/ai_runs/req_003/artifacts",
        )
        fetched = AiExecutionTable.get_by_id("req_003")
        assert fetched.module_status == "timeout"
        assert fetched.ai_label is None
        assert fetched.score is None
        assert fetched.request_json_path == "D:/data/ai_runs/req_003/request.json"
        assert fetched.response_json_path is None
        assert fetched.artifacts_dir == "D:/data/ai_runs/req_003/artifacts"

    def test_request_id_is_primary_key(self):
        AiExecutionTable.create(
            request_id="req_dup",
            test_run_id="run_x",
            config_id=1,
            toolchain_id="tc",
            module_status="success",
        )
        with pytest.raises(IntegrityError):
            AiExecutionTable.create(
                request_id="req_dup",
                test_run_id="run_y",
                config_id=2,
                toolchain_id="tc",
                module_status="success",
            )

    def test_current_effective_query(self):
        """Only one record per test_run should be is_current_effective=True."""
        for i in range(3):
            AiExecutionTable.create(
                request_id=f"req_{i}",
                test_run_id="run_A",
                config_id=1,
                toolchain_id="tc",
                module_status="success",
                is_current_effective=(i == 2),
            )
        effective = (
            AiExecutionTable.select()
            .where(
                AiExecutionTable.test_run_id == "run_A",
                AiExecutionTable.is_current_effective == True,
            )
        )
        assert effective.count() == 1
        assert effective[0].request_id == "req_2"

    def test_multiple_runs_share_test_run_id(self):
        """Multiple execution attempts can share the same test_run_id (retries)."""
        for i in range(3):
            AiExecutionTable.create(
                request_id=f"retry_{i}",
                test_run_id="run_retry",
                config_id=1,
                toolchain_id="tc",
                module_status="success" if i == 2 else "process_error",
                is_current_effective=(i == 2),
            )
        all_for_run = AiExecutionTable.select().where(
            AiExecutionTable.test_run_id == "run_retry"
        )
        assert all_for_run.count() == 3

    def test_test_run_id_is_indexed(self):
        """test_run_id should be indexed for efficient lookups."""
        field = AiExecutionTable._meta.fields["test_run_id"]
        assert field.index is True
