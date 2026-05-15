from __future__ import annotations

import os
import zipfile

from peewee import Model, SqliteDatabase, fn

from base.utils import normalize_path
from consts.time_consts import now_str
from model.audio_data_table import AudioDataTable


class AudioDataService:
    def today_statistics(self):
        # record_date 格式如 2026-04-29T11:11:32.813876+08:00，前 10 位是日期。
        today = now_str()[:10]
        today_condition = fn.Substr(AudioDataTable.record_date, 1, 10) == today
        return {
            "total": AudioDataTable.select().where(today_condition).count(),
            "ok": AudioDataTable.select().where(
                today_condition,
                AudioDataTable.labels == "OK",
            ).count(),
            "ng": AudioDataTable.select().where(
                today_condition,
                AudioDataTable.labels == "NG",
            ).count(),
        }

    def list_column_names(self) -> list[str]:
        # 查询：返回 audio_data_table 的字段顺序，给 UI 生成表头用。
        return list(AudioDataTable._meta.sorted_field_names)

    def list_records(self, filters: dict[str, object] | None = None) -> list[dict[str, object]]:
        # 查询：读取音频表数据，按创建时间倒序给数据管理页展示。
        query = AudioDataTable.select()
        query = self._apply_filters(query, filters)
        query = query.order_by(AudioDataTable.record_date.desc()).dicts()
        return list(query)

    def delete_record(self, audio_data_id: object) -> None:
        record_id = str(audio_data_id or "").strip()
        if not record_id:
            raise ValueError("缺少 audio_id")

        record = AudioDataTable.get_or_none(AudioDataTable.audio_data_id == record_id)
        if record is None:
            raise ValueError("数据不存在")

        file_path = normalize_path(record.file_path)
        if file_path and os.path.exists(file_path):
            os.remove(file_path)

        record.delete_instance()

    def list_product_models(self) -> list[str]:
        # 查询：返回所有产品型号，给筛选下拉框使用。
        query = (
            AudioDataTable.select(AudioDataTable.product_model)
            .where(AudioDataTable.product_model != "")
            .distinct()
            .order_by(AudioDataTable.product_model)
            .tuples()
        )
        return [row[0] for row in query if row and row[0]]

    def record_date_bounds(self) -> tuple[str | None, str | None]:
        # 查询：返回最早和最晚时间，用于筛选输入提示。
        earliest = (
            AudioDataTable.select(AudioDataTable.record_date)
            .order_by(AudioDataTable.record_date.asc())
            .first()
        )
        latest = (
            AudioDataTable.select(AudioDataTable.record_date)
            .order_by(AudioDataTable.record_date.desc())
            .first()
        )
        min_value = earliest.record_date[:19] if earliest and earliest.record_date else None
        max_value = latest.record_date[:19] if latest and latest.record_date else None
        return min_value, max_value

    def export_zip(self, target_path: str, records: list[dict[str, object]], progress_callback=None) -> None:
        # 导出/保存：生成 zip 包，里面包含 1 个 db 和分类后的音频文件。
        base_name = os.path.splitext(os.path.basename(target_path))[0]
        db_name = f"{base_name}.db"
        temp_db_path = f"{target_path}.tmp.db"
        export_records = [self._build_export_record(record) for record in records]

        self._export_db(temp_db_path, export_records)
        if progress_callback:
            # 第一步是写出随 zip 一起导出的临时数据库。
            progress_callback(1)
        try:
            # 只打包不压缩，避免数据量大时压缩音频导致界面长时间卡住。
            with zipfile.ZipFile(target_path, "w", zipfile.ZIP_STORED) as archive:
                archive.write(temp_db_path, db_name)
                for index, record in enumerate(export_records, start=2):
                    source_path = normalize_path(record.get("_source_file_path"))
                    archive_path = record.get("file_path")
                    if source_path and archive_path and os.path.exists(source_path):
                        archive.write(source_path, str(archive_path))
                    if progress_callback:
                        # 每处理完一个音频文件，通知界面更新一次进度。
                        progress_callback(index)
        finally:
            if os.path.exists(temp_db_path):
                os.remove(temp_db_path)

    def _export_db(self, target_path: str, records: list[dict[str, object]]) -> None:
        # 导出/保存：把选中的记录写入一个新的 sqlite db 文件。
        if os.path.exists(target_path):
            os.remove(target_path)

        target_db = SqliteDatabase(target_path)
        export_model = self._build_export_model(target_db)
        target_db.bind([export_model])
        target_db.connect()
        try:
            target_db.create_tables([export_model])
            field_names = self.list_column_names()
            for record in records:
                payload = {field_name: record.get(field_name) for field_name in field_names}
                export_model.create(**payload)
        finally:
            target_db.close()

    def _build_export_record(self, record: dict[str, object]) -> dict[str, object]:
        # 导出前先把 file_path 改成 zip 内相对路径，方便别人解压后直接使用。
        export_record = dict(record)
        source_path = normalize_path(record.get("file_path"))
        file_name = self._build_export_file_name(record)
        folder_name = self._label_folder_name(record.get("labels"))
        export_record["_source_file_path"] = source_path
        export_record["file_path"] = f"{folder_name}/{file_name}"
        return export_record

    def _build_export_file_name(self, record: dict[str, object]) -> str:
        # 导出音频文件名优先使用 audio_data_id，保证名字稳定不重复。
        source_path = normalize_path(record.get("file_path"))
        original_name = os.path.basename(source_path) if source_path else ""
        _, extension = os.path.splitext(original_name)
        if not extension:
            extension = ".wav"
        audio_data_id = str(record.get("audio_data_id") or "").strip()
        if audio_data_id:
            return f"{audio_data_id}{extension}"
        if original_name:
            return original_name
        return f"audio_export{extension}"

    def _label_folder_name(self, label_value: object) -> str:
        # 根据 labels 把音频分到 OK / NG / not_labeled 目录。
        label = str(label_value or "").strip()
        if label == "OK":
            return "OK"
        if label == "NG":
            return "NG"
        return "not_labeled"

    def _apply_filters(self, query, filters: dict[str, object] | None):
        if not filters:
            return query

        record_datetime = fn.datetime(
            fn.replace(fn.Substr(AudioDataTable.record_date, 1, 19), "T", " ")
        )

        product_model = str(filters.get("product_model") or "").strip()
        if product_model and product_model != "ALL":
            query = query.where(AudioDataTable.product_model == product_model)

        barcode = str(filters.get("barcode") or "").strip()
        if barcode:
            query = query.where(AudioDataTable.barcode.contains(barcode))

        start_time = str(filters.get("start_time") or "").strip()
        if start_time:
            query = query.where(record_datetime >= fn.datetime(start_time))

        end_time = str(filters.get("end_time") or "").strip()
        if end_time:
            query = query.where(record_datetime <= fn.datetime(end_time))

        labels = filters.get("labels") or []
        normalized_labels = {str(label).strip() for label in labels if str(label).strip()}
        if normalized_labels and normalized_labels != {"OK", "NG", "not_labeled"}:
            conditions = []
            if "OK" in normalized_labels:
                conditions.append(AudioDataTable.labels == "OK")
            if "NG" in normalized_labels:
                conditions.append(AudioDataTable.labels == "NG")
            if "not_labeled" in normalized_labels:
                conditions.append(
                    (AudioDataTable.labels.is_null(True))
                    | (AudioDataTable.labels == "")
                    | (AudioDataTable.labels == "not_labeled")
                )
            if conditions:
                label_condition = conditions[0]
                for condition in conditions[1:]:
                    label_condition |= condition
                query = query.where(label_condition)
        return query

    @staticmethod
    def _build_export_model(target_db: SqliteDatabase):
        # 给导出用的临时 db 生成一个和 AudioDataTable 结构一致的模型。
        class ExportAudioDataTable(Model):
            audio_data_id = AudioDataTable.audio_data_id.clone()
            file_path = AudioDataTable.file_path.clone()
            product_model = AudioDataTable.product_model.clone()
            sample_rate = AudioDataTable.sample_rate.clone()
            record_date = AudioDataTable.record_date.clone()
            labels = AudioDataTable.labels.clone()
            label_source = AudioDataTable.label_source.clone()
            barcode = AudioDataTable.barcode.clone()
            analyse_config = AudioDataTable.analyse_config.clone()
            config_content = AudioDataTable.config_content.clone()

            class Meta:
                database = target_db
                table_name = AudioDataTable._meta.table_name

        return ExportAudioDataTable
