from peewee import AutoField, TextField

from model.base_mode import BaseModel


GOOD_MALFUNCTION_TYPE_NAME = "良品"
BAD_MALFUNCTION_TYPE_NAME = "不良"
BUILTIN_MALFUNCTION_TYPES = (
    (2, "NG", BAD_MALFUNCTION_TYPE_NAME),
)
BUILTIN_MALFUNCTION_TYPE_IDS = frozenset(
    type_id for type_id, _number, _name in BUILTIN_MALFUNCTION_TYPES
)
BUILTIN_MALFUNCTION_TYPE_NUMBERS = frozenset(
    number for _type_id, number, _name in BUILTIN_MALFUNCTION_TYPES
)
BUILTIN_MALFUNCTION_TYPE_NAMES = frozenset(
    name for _type_id, _number, name in BUILTIN_MALFUNCTION_TYPES
)


class MalfunctionTypeTable(BaseModel):
    """对应表：malfunction_type_table"""

    id = AutoField()
    malfunction_number = TextField()
    malfunction_name = TextField()
    malfunction_notes = TextField(null=True)

    class Meta:
        table_name = "malfunction_type_table"


def ensure_builtin_malfunction_type() -> None:
    """保证系统默认“不良”固定在故障类型表中。"""

    database = MalfunctionTypeTable._meta.database
    with database.atomic():
        rows = list(
            MalfunctionTypeTable.select().order_by(MalfunctionTypeTable.id)
        )
        expected = [
            (type_id, number, name)
            for type_id, number, name in BUILTIN_MALFUNCTION_TYPES
        ]
        current_head = [
            (row.id, row.malfunction_number, row.malfunction_name)
            for row in rows[: len(expected)]
        ]
        trailing_rows = rows[len(expected):]
        if current_head == expected and all(
            row.id not in BUILTIN_MALFUNCTION_TYPE_IDS
            and row.malfunction_number not in BUILTIN_MALFUNCTION_TYPE_NUMBERS
            and row.malfunction_name not in BUILTIN_MALFUNCTION_TYPE_NAMES
            for row in trailing_rows
        ):
            return

        normal_rows = [
            row
            for row in rows
            if row.malfunction_number not in BUILTIN_MALFUNCTION_TYPE_NUMBERS
            and row.malfunction_name not in BUILTIN_MALFUNCTION_TYPE_NAMES
        ]

        MalfunctionTypeTable.delete().execute()

        for type_id, number, name in BUILTIN_MALFUNCTION_TYPES:
            MalfunctionTypeTable.create(
                id=type_id,
                malfunction_number=number,
                malfunction_name=name,
                malfunction_notes="系统默认类型，不可删除",
            )

        next_id = max(BUILTIN_MALFUNCTION_TYPE_IDS) + 1
        for row in normal_rows:
            MalfunctionTypeTable.create(
                id=next_id,
                malfunction_number=row.malfunction_number,
                malfunction_name=row.malfunction_name,
                malfunction_notes=row.malfunction_notes,
            )
            next_id += 1


def is_builtin_malfunction_type_id(value: object) -> bool:
    try:
        type_id = int(value)
    except (TypeError, ValueError):
        return False
    return type_id in BUILTIN_MALFUNCTION_TYPE_IDS
