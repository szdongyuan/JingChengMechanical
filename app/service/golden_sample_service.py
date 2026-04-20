from __future__ import annotations

import os
import shutil
import uuid
from dataclasses import dataclass

from scipy.io import wavfile

from consts.running_consts import base_dir


@dataclass(frozen=True)
class StagedGoldenSample:
    staged_path: str
    label: str
    source_channel: int
    start_sample: int
    num_samples: int
    start_ms: int
    duration_ms: int
    sample_rate: float

    @classmethod
    def from_mapping(cls, data: StagedGoldenSample | dict) -> StagedGoldenSample:
        if isinstance(data, cls):
            return data
        return cls(
            staged_path=str(data.get("staged_path", "")),
            label=str(data.get("label") or ""),
            source_channel=data.get("source_channel"),
            start_sample=data.get("start_sample"),
            num_samples=data.get("num_samples"),
            start_ms=data.get("start_ms"),
            duration_ms=data.get("duration_ms"),
            sample_rate=data.get("sample_rate"),
        )


class GoldenSampleService:
    def __init__(self, *, root_dir: str | None = None):
        root = root_dir or os.path.join(base_dir, "data", "golden_samples")
        self._root_dir = os.path.abspath(root)

    def managed_root(self) -> str:
        return self._root_dir

    def is_managed_path(self, path: str) -> bool:
        return self._is_within_root(path)

    def staged_root(self) -> str:
        return os.path.join(self.managed_root(), "staged")

    def is_managed_staged_path(self, path: str) -> bool:
        staged_root = os.path.realpath(self.staged_root())
        candidate = os.path.realpath(os.path.abspath(str(path)))
        try:
            return os.path.commonpath([staged_root, candidate]) == staged_root
        except ValueError:
            return False

    def create_staged_sample(
        self,
        *,
        source_path: str,
        source_channel: int,
        start_sample: int,
        num_samples: int,
        sample_rate: float,
        label: str | None = None,
    ) -> StagedGoldenSample:
        if not self._is_non_negative_int(source_channel):
            raise ValueError("黄金样本源通道必须大于等于 0")
        if not self._is_non_negative_int(start_sample):
            raise ValueError("黄金样本起始采样点必须大于等于 0")
        if not self._is_positive_int(num_samples):
            raise ValueError("黄金样本长度必须大于 0")
        if not self._is_positive_number(sample_rate):
            raise ValueError("黄金样本采样率无效")

        actual_sample_rate, data = self._read_wav(source_path)
        if float(actual_sample_rate) != float(sample_rate):
            raise ValueError("黄金样本采样率与当前输入采样率不一致")

        mono = self._select_channel(data, source_channel)
        end_sample = start_sample + num_samples
        if end_sample > mono.shape[0]:
            raise ValueError("黄金样本窗口超出音频长度")

        staged_dir = self.staged_root()
        os.makedirs(staged_dir, exist_ok=True)
        staged_path = os.path.abspath(
            os.path.join(staged_dir, f"{uuid.uuid4().hex}.wav")
        )
        wavfile.write(staged_path, int(actual_sample_rate), mono[start_sample:end_sample])
        start_ms, duration_ms = self._normalize_ms(
            start_sample=start_sample,
            num_samples=num_samples,
            sample_rate=sample_rate,
        )
        normalized_label = str(label or os.path.basename(source_path)).strip()
        return StagedGoldenSample(
            staged_path=staged_path,
            label=normalized_label or os.path.basename(source_path),
            source_channel=source_channel,
            start_sample=start_sample,
            num_samples=num_samples,
            start_ms=start_ms,
            duration_ms=duration_ms,
            sample_rate=float(sample_rate),
        )

    def validate_staged_binding(
        self,
        staged_sample: StagedGoldenSample | dict,
        *,
        analysis_duration: float,
        sample_rate: float,
    ) -> list[str]:
        staged = StagedGoldenSample.from_mapping(staged_sample)
        errors: list[str] = []

        if not self._is_non_negative_int(staged.source_channel):
            errors.append("黄金样本源通道必须大于等于 0")
        if not self._is_non_negative_int(staged.start_sample):
            errors.append("黄金样本起始采样点必须大于等于 0")
        if not self._is_positive_int(staged.num_samples):
            errors.append("黄金样本长度必须大于 0")
        if not self._is_positive_number(sample_rate):
            errors.append("黄金样本需要有效的输入采样率")
            return errors
        if not self._is_positive_number(analysis_duration):
            errors.append("黄金样本需要有效的分析时长")
            return errors
        if not self._is_positive_number(staged.sample_rate):
            errors.append("黄金样本采样率无效")
            return errors
        if float(staged.sample_rate) != float(sample_rate):
            errors.append("黄金样本采样率与当前输入采样率不一致")

        required_samples = round(float(sample_rate) * float(analysis_duration))
        if staged.num_samples != required_samples:
            errors.append("黄金样本长度与当前分析时长不一致")

        expected_start_ms, expected_duration_ms = self._normalize_ms(
            start_sample=staged.start_sample,
            num_samples=staged.num_samples,
            sample_rate=staged.sample_rate,
        )
        if staged.start_ms != expected_start_ms:
            errors.append("黄金样本起始时间元数据已过期")
        if staged.duration_ms != expected_duration_ms:
            errors.append("黄金样本时长元数据已过期")

        if errors:
            return errors

        try:
            actual_sample_rate, data = self._read_wav(staged.staged_path)
        except ValueError:
            return ["黄金样本暂存文件不存在或不可读"]

        if float(actual_sample_rate) != float(staged.sample_rate):
            errors.append("黄金样本采样率与当前输入采样率不一致")
        if self._sample_count(data) != staged.num_samples:
            errors.append("黄金样本文件长度与记录元数据不一致")
        return errors

    def validate_committed_binding(
        self,
        *,
        channel_config: dict,
        analysis_duration: float,
        sample_rate: float,
    ) -> list[str]:
        errors: list[str] = []
        path = str(channel_config.get("golden_sample_path") or "").strip()
        if not path:
            return ["黄金样本路径不存在或不可读"]
        if not self._is_within_root(path):
            errors.append("黄金样本路径必须位于受管目录内")
        elif self.is_managed_staged_path(path):
            errors.append("黄金样本必须位于受管已提交目录")
        if not self._is_non_negative_int(channel_config.get("golden_sample_start_sample")):
            errors.append("黄金样本起始采样点必须大于等于 0")
        if not self._is_non_negative_int(channel_config.get("golden_sample_source_channel")):
            errors.append("黄金样本源通道必须大于等于 0")

        stored_sample_rate = channel_config.get("golden_sample_sample_rate")
        if not self._is_positive_number(sample_rate):
            errors.append("黄金样本需要有效的输入采样率")
            return errors
        if not self._is_positive_number(analysis_duration):
            errors.append("黄金样本需要有效的分析时长")
            return errors
        if not self._is_positive_number(stored_sample_rate):
            errors.append("黄金样本采样率无效")
        elif float(stored_sample_rate) != float(sample_rate):
            errors.append("黄金样本采样率与当前输入采样率不一致")

        num_samples = channel_config.get("golden_sample_num_samples")
        if not self._is_positive_int(num_samples):
            errors.append("黄金样本长度必须大于 0")
        else:
            required_samples = round(float(sample_rate) * float(analysis_duration))
            if int(num_samples) != required_samples:
                errors.append("黄金样本长度与当前分析时长不一致")

        if self._is_non_negative_int(channel_config.get("golden_sample_start_sample")) and self._is_positive_number(
            stored_sample_rate
        ):
            expected_start_ms, expected_duration_ms = self._normalize_ms(
                start_sample=int(channel_config["golden_sample_start_sample"]),
                num_samples=int(num_samples or 0),
                sample_rate=float(stored_sample_rate),
            )
            if channel_config.get("golden_sample_start_ms") != expected_start_ms:
                errors.append("黄金样本起始时间元数据已过期")
            if channel_config.get("golden_sample_duration_ms") != expected_duration_ms:
                errors.append("黄金样本时长元数据已过期")

        if errors:
            return errors

        try:
            actual_sample_rate, data = self._read_wav(path)
        except ValueError:
            return ["黄金样本路径不存在或不可读"]

        if float(actual_sample_rate) != float(stored_sample_rate):
            errors.append("黄金样本采样率与当前输入采样率不一致")
        if getattr(data, "ndim", 1) != 1:
            errors.append("黄金样本必须是单声道 WAV")
        if self._sample_count(data) != int(num_samples):
            errors.append("黄金样本文件长度与记录元数据不一致")
        return errors

    def promote_staged_samples(
        self,
        *,
        config_id: int,
        staged_by_channel: dict[int, StagedGoldenSample | dict],
    ) -> dict[int, dict]:
        promoted: dict[int, dict] = {}
        promoted_paths: list[str] = []
        try:
            for channel_index, staged_sample in sorted(staged_by_channel.items()):
                staged = StagedGoldenSample.from_mapping(staged_sample)
                if self.validate_staged_binding(
                    staged,
                    analysis_duration=staged.num_samples / float(staged.sample_rate),
                    sample_rate=staged.sample_rate,
                ):
                    raise ValueError("黄金样本暂存文件不存在或不可读")

                committed_path = self._build_committed_path(
                    config_id=config_id,
                    channel_index=channel_index,
                )
                shutil.copy2(staged.staged_path, committed_path)
                promoted_paths.append(committed_path)
                promoted[channel_index] = {
                    "golden_sample_path": committed_path,
                    "golden_sample_label": staged.label,
                    "golden_sample_source_channel": staged.source_channel,
                    "golden_sample_start_sample": staged.start_sample,
                    "golden_sample_num_samples": staged.num_samples,
                    "golden_sample_start_ms": staged.start_ms,
                    "golden_sample_duration_ms": staged.duration_ms,
                    "golden_sample_sample_rate": staged.sample_rate,
                }
        except Exception:
            self.rollback_promoted_samples(promoted_paths)
            raise
        return promoted

    def restore_imported_sample(
        self,
        config_id: int,
        channel_index: int,
        source_path: str,
        metadata: dict,
    ) -> dict:
        committed_path = self._build_committed_path(
            config_id=config_id,
            channel_index=channel_index,
        )
        try:
            shutil.copy2(source_path, committed_path)
            restored = self._normalize_imported_metadata(
                source_path=source_path,
                committed_path=committed_path,
                metadata=metadata,
            )
            errors = self.validate_committed_binding(
                channel_config=restored,
                analysis_duration=restored["golden_sample_num_samples"]
                / float(restored["golden_sample_sample_rate"]),
                sample_rate=restored["golden_sample_sample_rate"],
            )
            if errors:
                raise ValueError("\n".join(errors))
            return restored
        except Exception:
            self.cleanup_paths([committed_path])
            raise

    def discard_staged_samples(self, staged_paths: list[str]) -> None:
        managed_staged_paths = [
            path for path in staged_paths if self.is_managed_staged_path(path)
        ]
        self.cleanup_paths(managed_staged_paths)

    def rollback_promoted_samples(self, promoted_paths: list[str]) -> None:
        self.cleanup_paths(promoted_paths)

    def cleanup_paths(self, paths: list[str]) -> None:
        seen: set[str] = set()
        for raw_path in paths:
            path = os.path.abspath(str(raw_path))
            if not path or path in seen:
                continue
            seen.add(path)
            if not os.path.exists(path):
                continue
            os.remove(path)

    def _read_wav(self, path: str) -> tuple[int, object]:
        normalized = os.path.abspath(str(path))
        if not os.path.isfile(normalized):
            raise ValueError("missing wav")
        try:
            return wavfile.read(normalized)
        except Exception as exc:  # pragma: no cover - scipy error shape is not stable
            raise ValueError("unreadable wav") from exc

    @staticmethod
    def _select_channel(data, source_channel: int):
        if getattr(data, "ndim", 1) == 1:
            if source_channel != 0:
                raise ValueError("黄金样本源通道无效")
            return data
        if source_channel >= data.shape[1]:
            raise ValueError("黄金样本源通道无效")
        return data[:, source_channel]

    @staticmethod
    def _sample_count(data) -> int:
        return int(data.shape[0]) if getattr(data, "shape", ()) else int(len(data))

    @staticmethod
    def _normalize_ms(
        *,
        start_sample: int,
        num_samples: int,
        sample_rate: float,
    ) -> tuple[int, int]:
        return (
            round(int(start_sample) * 1000 / float(sample_rate)),
            round(int(num_samples) * 1000 / float(sample_rate)),
        )

    def _build_committed_path(self, *, config_id: int, channel_index: int) -> str:
        config_dir = os.path.join(self.managed_root(), f"config_{config_id}")
        os.makedirs(config_dir, exist_ok=True)
        return os.path.abspath(
            os.path.join(
                config_dir,
                f"channel_{channel_index}_{uuid.uuid4().hex}.wav",
            )
        )

    def _normalize_imported_metadata(
        self,
        *,
        source_path: str,
        committed_path: str,
        metadata: dict,
    ) -> dict:
        return {
            "golden_sample_path": committed_path,
            "golden_sample_label": self._normalize_import_text(
                metadata.get("golden_sample_label"),
                fallback=metadata.get("label"),
                default=os.path.basename(source_path),
            ),
            "golden_sample_source_channel": self._metadata_value(
                metadata,
                "golden_sample_source_channel",
                "source_channel",
            ),
            "golden_sample_start_sample": self._metadata_value(
                metadata,
                "golden_sample_start_sample",
                "start_sample",
            ),
            "golden_sample_num_samples": self._metadata_value(
                metadata,
                "golden_sample_num_samples",
                "num_samples",
            ),
            "golden_sample_start_ms": self._metadata_value(
                metadata,
                "golden_sample_start_ms",
                "start_ms",
            ),
            "golden_sample_duration_ms": self._metadata_value(
                metadata,
                "golden_sample_duration_ms",
                "duration_ms",
            ),
            "golden_sample_sample_rate": self._metadata_value(
                metadata,
                "golden_sample_sample_rate",
                "sample_rate",
            ),
        }

    def _is_within_root(self, path: str) -> bool:
        root = os.path.realpath(self.managed_root())
        candidate = os.path.realpath(os.path.abspath(str(path)))
        try:
            return os.path.commonpath([root, candidate]) == root
        except ValueError:
            return False

    @staticmethod
    def _is_non_negative_int(value) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    @staticmethod
    def _is_positive_int(value) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value > 0

    @staticmethod
    def _is_positive_number(value) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0

    @staticmethod
    def _metadata_value(metadata: dict, primary: str, fallback: str):
        if primary in metadata:
            return metadata.get(primary)
        return metadata.get(fallback)

    @staticmethod
    def _normalize_import_text(value, *, fallback=None, default: str | None = None) -> str | None:
        for candidate in (value, fallback, default):
            if candidate is None:
                continue
            text = str(candidate).strip()
            if text:
                return text
        return None
