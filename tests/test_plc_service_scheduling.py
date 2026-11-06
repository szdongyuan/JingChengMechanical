from __future__ import annotations

import ctypes
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QThread, Qt
from PyQt6.QtWidgets import QApplication

from app.service.plc_service import PlcService


_QT_APP = None


@pytest.mark.skipif(os.name != "nt", reason="Windows thread priority readback")
@pytest.mark.parametrize("connected", [False, True])
def test_plc_owner_priority_and_precise_poll_preserve_configured_intervals(connected):
    global _QT_APP
    _QT_APP = QApplication.instance() or QApplication([])
    observations = {}

    class OwnerThread(QThread):
        def run(self):
            try:
                service = PlcService(
                    SimpleNamespace(log=MagicMock()), lambda *_args: None,
                    "127.0.0.1", poll_interval_ms=137, reconnect_interval_ms=2345,
                )
                service.logger = MagicMock()
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.GetCurrentThread.restype = ctypes.c_void_p
                kernel32.GetThreadPriority.argtypes = (ctypes.c_void_p,)
                kernel32.GetThreadPriority.restype = ctypes.c_int

                def connect_once():
                    observations["priority_at_connect"] = kernel32.GetThreadPriority(
                        kernel32.GetCurrentThread()
                    )
                    service.plc_client = object() if connected else None

                service._connect_once = connect_once
                try:
                    service.start_plc_service()
                    timer = service.timer
                    observations.update(
                        timer_type=timer.timerType(), interval=timer.interval(),
                        active=timer.isActive(), reconnect=service._reconnect_interval_ms,
                    )
                    service.start_plc_service()
                    observations["same_timer"] = service.timer is timer
                finally:
                    service.stop_ingress_polling()
            except BaseException as exc:
                observations["error"] = exc

    owner = OwnerThread()
    owner.start()
    assert owner.wait(5000), "PLC owner did not finish"
    assert "error" not in observations, observations.get("error")
    assert observations == {
        "priority_at_connect": 1,
        "timer_type": Qt.TimerType.PreciseTimer,
        "interval": 137,
        "active": True,
        "reconnect": 2345,
        "same_timer": True,
    }
