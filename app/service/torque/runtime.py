"""One torque decision using only a fixed cycle and injected PLC I/O."""
import math

from app.service.torque.types import TorqueCycle, TorqueStatus
from app.service.torque.validation import normalize_key, resolve_addresses, validate_torque


class TorqueRuntime:
    def process(self, cycle: TorqueCycle, *, read_real, write_real, publish) -> TorqueStatus | None:
        if not cycle.config.enabled:
            return None

        match_type = cycle.config.match_type
        key = cycle.model if match_type == 'model' else cycle.sn
        try:
            key = normalize_key(key)
        except ValueError:
            key = ''
        read_value = None
        target_db = target_offset = None

        def emit(state, value=None, source=None, detail=''):
            status = TorqueStatus(
                cycle.plc_id, cycle.trigger_ns, state, value, source,
                match_type, key, cycle.rules.revision, detail,
                read_value, target_db, target_offset,
                cycle_id=cycle.cycle_id,
            )
            publish(status)
            return status

        try:
            read_address, (target_db, target_offset) = resolve_addresses(cycle.config, cycle.inherited_db)
        except ValueError as exc:
            return emit('config_error', detail=str(exc))

        emit('reading')
        try:
            read_value = read_real(*read_address)
        except Exception as exc:
            return emit('read_error', detail=str(exc))

        if not isinstance(read_value, (int, float)) or isinstance(read_value, bool) or not math.isfinite(read_value):
            return emit('invalid_value', detail='PLC 扭矩必须为有限 REAL')
        if read_value != 0.0:
            return emit('plc', value=read_value, source='plc')

        if not key or (match_type, key) not in cycle.rules.values:
            return emit('unmatched')
        try:
            value = validate_torque(cycle.rules.values[match_type, key])
        except ValueError as exc:
            return emit('invalid_value', detail=str(exc))
        emit('table_pending', value, 'table')
        try:
            write_real(target_db, target_offset, value)
        except Exception as exc:
            return emit('write_error', value, 'table', str(exc))
        return emit('table_sent', value, 'table')
