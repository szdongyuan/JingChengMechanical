from __future__ import annotations

import os
import zipfile

from peewee import Model, SqliteDatabase

from base.utils import normalize_path
from model.audio_data_table import AudioDataTable


class AudioDataService:
    def list_column_names(self) -> list[str]:
        # 查询：返回 audio_data_table 的字段顺序，给 UI 生成表头用。
        return list(AudioDataTable._meta.sorted_field_names)

    def list_records(self) -> list[dict[str, object]]:
        # 查询：读取音频表数据，按创建时间倒序给数据管理页展示。
        query = AudioDataTable.select().order_by(AudioDataTable.record_date.desc()).dicts()
        return list(query)

    def export_zip(self, target_path: str, records: list[dict[str, object]]) -> None:
        # 导出/保存：生成 zip 包，里面包含 1 个 db 和分类后的音频文件。
        base_name = os.path.splitext(os.path.basename(target_path))[0]
        db_name = f"{base_name}.db"
        temp_db_path = f"{target_path}.tmp.db"
        export_records = [self._build_export_record(record) for record in records]

        self._export_db(temp_db_path, export_records)
        try:
            with zipfile.ZipFile(target_path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.write(temp_db_path, db_name)
                for record in export_records:
                    source_path = normalize_path(record.get("_source_file_path"))
                    archive_path = record.get("file_path")
                    if source_path and archive_path and os.path.exists(source_path):
                        archive.write(source_path, str(archive_path))
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
            barcode = AudioDataTable.barcode.clone()
            analyse_config = AudioDataTable.analyse_config.clone()

            class Meta:
                database = target_db
                table_name = AudioDataTable._meta.table_name

        return ExportAudioDataTable
