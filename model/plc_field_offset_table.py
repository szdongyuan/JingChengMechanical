from peewee import IntegerField, TextField

from model.base_mode import BaseModel


class PlcFieldOffsetTable(BaseModel):
    plc_id = IntegerField(primary_key=True)
    start_signal_offset = TextField(null=True)
    current_value_offset = TextField(null=True)
    sn_code_offset = TextField(null=True)
    product_model_offset = TextField(null=True)
    res_finish_signal_offset = TextField(null=True)
    res_error_code_offset = TextField(null=True)
    res_sn_code_offset = TextField(null=True)
    res_result_offset = TextField(null=True)
    res_score_offset = TextField(null=True)
    res_spl_offset = TextField(null=True)
    res_loudness_offset = TextField(null=True)
    res_vibrate_data_offset = TextField(null=True)
    res_ai_score_offset = TextField(null=True)
    res_product_model_offset = TextField(null=True)
    res_spl_limit_up_offset = TextField(null=True)
    res_spl_limit_down_offset = TextField(null=True)
    res_loudness_limit_up_offset = TextField(null=True)
    res_loudness_limit_down_offset = TextField(null=True)
    res_vibrate_limit_up_offset = TextField(null=True)
    res_vibrate_limit_down_offset = TextField(null=True)
    res_ai_score_limit_down_offset = TextField(null=True)

    class Meta:
        table_name = "plc_field_offset_table"
