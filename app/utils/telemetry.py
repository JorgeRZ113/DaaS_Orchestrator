"""Lightweight in-memory telemetry and structured logging helpers.
This module keeps the runtime state as plain objects and can export a
structured ``telemetry_report`` for tests, artifacts and offline analysis.
It intentionally stays backend-agnostic so the storage/export layer can be
evolved later without changing call sites.
Usage:
    from app.utils.telemetry import telemetry
    telemetry.increment_counter('requests_total', labels={'service': 'orchestrator', 'operation': 'create'})
    timer = telemetry.start_timer('orchestrator', 'create', execution_id)
    timer.start()
    ... await work ...
    duration = timer.stop()
    telemetry.log_event('info', 'orchestrator.create.completed', service='orchestrator', operation='create', execution_id=execution_id)
The implementation keeps internal counters, gauges and timing samples as
objects. The exported report filters human-visible timings to durations >= 1s
while keeping all samples available for totals and calculations.
"""

from __future__ import annotations
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from app.config import settings

logger = logging.getLogger("telemetry")
_MIN_VISIBLE_DURATION_SECONDS = 1.0


def _telemetry_root_dir() -> str:
    base = settings.artifacts_dir or "./artifacts"
    if os.getenv("PYTEST_CURRENT_TEST"):
        base = os.path.join(base, "tests")
    return base


def _format_timestamp_human() -> str:
    """Generar timestamp en formato HH:MM:SS-DD/MM/AAAA."""
    now = datetime.now(timezone.utc)
    return now.strftime("%H:%M:%S-%d/%m/%Y")


def format_duration_display(duration_seconds: float) -> str:
    """Format duration as MM:SS:MMM for human-readable reports/logs."""
    total_ms = max(0, int(round(float(duration_seconds) * 1000)))
    minutes, remaining_ms = divmod(total_ms, 60_000)
    seconds, millis = divmod(remaining_ms, 1000)
    return f"{minutes:02d}:{seconds:02d}:{millis:03d}"


def _safe_json_value(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, sort_keys=True, default=str))
    except Exception:
        return str(value)


def _normalize_labels(labels: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return _safe_json_value(labels or {})


def _metric_key(name: str, labels: Optional[Dict[str, Any]]) -> str:
    normalized_labels = _normalize_labels(labels)
    return f"{name}:{json.dumps(normalized_labels, sort_keys=True, default=str)}"


def _infer_phase(message: str) -> str:
    suffix = (message or "").split(".")[-1].lower()
    mapping = {
        "started": "start",
        "start": "start",
        "completed": "completed",
        "success": "completed",
        "done": "completed",
        "failed": "error",
        "error": "error",
        "exhausted": "error",
        "retry": "retry",
        "retries": "retry",
        "timeout": "retry",
        "timed_out": "error",
    }
    return mapping.get(suffix, suffix or "info")


def _status_from_phase(phase: str) -> str:
    mapping = {
        "start": "start",
        "completed": "success",
        "error": "error",
        "retry": "retry",
    }
    return mapping.get(phase, phase)


@dataclass
class MetricRecord:
    name: str
    labels: Dict[str, Any] = field(default_factory=dict)
    value: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "labels": self.labels,
            "value": self.value,
        }


@dataclass
class TimingRecord:
    service: str
    operation: str
    phase: str
    duration_seconds: float
    status: str
    duration_display: str
    execution_id: Optional[str] = None
    attempt: Optional[int] = None
    labels: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "service": self.service,
            "operation": self.operation,
            "phase": self.phase,
            "duration_seconds": self.duration_seconds,
            "duration_display": self.duration_display,
            "status": self.status,
        }
        if self.execution_id is not None:
            payload["execution_id"] = self.execution_id
        if self.attempt is not None:
            payload["attempt"] = self.attempt
        if self.labels:
            payload["labels"] = self.labels
        return payload


@dataclass
class TelemetryReport:
    metadata: Dict[str, Any]
    counters: List[Dict[str, Any]]
    gauges: List[Dict[str, Any]]
    timings: List[Dict[str, Any]]
    totals: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metadata": self.metadata,
            "counters": self.counters,
            "gauges": self.gauges,
            "timings": self.timings,
            "totals": self.totals,
        }


@dataclass
class Timer:
    telemetry: "Telemetry"
    service: str
    operation: str
    execution_id: Optional[str]
    labels: Dict[str, Any] = field(default_factory=dict)
    _start: Optional[float] = None

    def start(self) -> None:
        self._start = time.time()

    def stop(
        self,
        *,
        status: str = "success",
        phase: str = "completed",
        attempt: Optional[int] = None,
    ) -> float:
        if self._start is None:
            raise RuntimeError("Timer was not started")
        duration = time.time() - self._start
        self.telemetry.observe_duration(
            service=self.service,
            operation=self.operation,
            execution_id=self.execution_id,
            duration_seconds=duration,
            labels=self.labels,
            phase=phase,
            status=status,
            attempt=attempt,
        )
        log_fields: Dict[str, Any] = {
            "service": self.service,
            "operation": self.operation,
            "execution_id": self.execution_id,
            "phase": phase,
            "status": status,
        }
        if attempt is not None:
            log_fields["attempt"] = attempt
        if self.labels:
            log_fields["labels"] = self.labels
        if duration >= _MIN_VISIBLE_DURATION_SECONDS:
            log_fields["duration_display"] = format_duration_display(duration)
        level = "error" if status in {"error", "failed"} or phase == "error" else "info"
        self.telemetry.log_event(level, f"{self.service}.{self.operation}.{phase}", **log_fields)
        return duration


class Telemetry:
    def __init__(self) -> None:
        self._counters: Dict[str, MetricRecord] = {}
        self._gauges: Dict[str, MetricRecord] = {}
        self._timings: List[TimingRecord] = []
        self._lock = threading.Lock()
        # separate lock used when writing the telemetry file
        self._file_lock = threading.Lock()

    def _telemetry_file_path(self, execution_id: Optional[str] = None) -> str:
        try:
            base = _telemetry_root_dir()
        except Exception:
            base = "./artifacts"

        if execution_id:
            base = os.path.join(base, str(execution_id))

        # ensure directory exists
        try:
            os.makedirs(base, exist_ok=True)
        except Exception:
            # best-effort: ignore filesystem errors
            pass
        return os.path.join(base, "telemetry.log")

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._timings.clear()

    # IDs
    def ensure_execution_id(self, execution_id: Optional[str] = None) -> str:
        if execution_id:
            return execution_id
        return str(uuid.uuid4())

    # Counters
    def increment_counter(
        self, name: str, labels: Optional[Dict[str, Any]] = None, amount: int = 1
    ) -> None:
        key = _metric_key(name, labels)
        normalized_labels = _normalize_labels(labels)
        with self._lock:
            record = self._counters.get(key)
            if record is None:
                record = MetricRecord(name=name, labels=normalized_labels, value=0.0)
                self._counters[key] = record
            record.value += amount

    # Gauges
    def set_gauge(self, name: str, value: float, labels: Optional[Dict[str, Any]] = None) -> None:
        key = _metric_key(name, labels)
        normalized_labels = _normalize_labels(labels)
        with self._lock:
            record = self._gauges.get(key)
            if record is None:
                record = MetricRecord(name=name, labels=normalized_labels, value=float(value))
                self._gauges[key] = record
            else:
                record.value = float(value)

    def change_gauge(
        self, name: str, delta: float, labels: Optional[Dict[str, Any]] = None
    ) -> None:
        key = _metric_key(name, labels)
        normalized_labels = _normalize_labels(labels)
        with self._lock:
            record = self._gauges.get(key)
            if record is None:
                record = MetricRecord(name=name, labels=normalized_labels, value=float(delta))
                self._gauges[key] = record
            else:
                record.value = float(record.value + delta)

    # Durations
    def observe_duration(
        self,
        *,
        service: str,
        operation: str,
        execution_id: Optional[str],
        duration_seconds: float,
        labels: Optional[Dict[str, Any]] = None,
        phase: str = "completed",
        status: str = "success",
        attempt: Optional[int] = None,
    ) -> TimingRecord:
        normalized_labels = _normalize_labels(labels)
        duration_seconds = float(duration_seconds)
        timing = TimingRecord(
            service=service,
            operation=operation,
            phase=phase,
            duration_seconds=duration_seconds,
            status=status,
            duration_display=format_duration_display(duration_seconds),
            execution_id=execution_id,
            attempt=attempt,
            labels=normalized_labels,
        )
        with self._lock:
            self._timings.append(timing)
        return timing

    # Timers
    def start_timer(
        self,
        service: str,
        operation: str,
        execution_id: Optional[str] = None,
        labels: Optional[Dict[str, Any]] = None,
    ) -> Timer:
        execution_id = self.ensure_execution_id(execution_id)
        return Timer(self, service, operation, execution_id, labels or {})

    # Logs
    def log_event(self, level: str, message: str, **fields: Any) -> None:
        phase = fields.pop("phase", None) or _infer_phase(message)
        status = fields.get("status") or _status_from_phase(phase)
        payload = {
            "timestamp": _format_timestamp_human(),
            "message": message,
            "phase": phase,
            "status": status,
            **fields,
        }
        # Use telemetry logger; write JSON as message for structured ingestors
        try:
            text = json.dumps(payload, default=str)
        except Exception:
            text = str(payload)
        if level.lower() == "debug":
            logger.debug(text)
        elif level.lower() == "warning" or level.lower() == "warn":
            logger.warning(text)
        elif level.lower() == "error":
            logger.error(text)
        else:
            logger.info(text)
        # Append to the telemetry log file for the related execution (JSON lines), best-effort and thread-safe.
        try:
            path = self._telemetry_file_path(fields.get("execution_id"))
            line = text + "\n"
            with self._file_lock:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(line)
        except Exception:
            # Never fail the main flow due to telemetry file I/O
            logger.debug("Could not append to telemetry log file", exc_info=True)

    # Telemetry report for tests/exports
    def telemetry_report(
        self,
        *,
        execution_id: Optional[str],
        stage: str,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        normalized_stage = (stage or "unknown").strip().lower().replace(" ", "_")
        metadata = {
            "execution_id": execution_id,
            "stage": normalized_stage,
            "generated_at": _format_timestamp_human(),
            "snapshot_type": "telemetry_report",
            "status": status or _status_from_phase(_infer_phase(normalized_stage)),
        }
        with self._lock:
            counters = [record.to_dict() for record in self._counters.values()]
            gauges = [record.to_dict() for record in self._gauges.values()]
            timings = [
                record.to_dict()
                for record in self._timings
                if record.duration_seconds >= _MIN_VISIBLE_DURATION_SECONDS
            ]
            totals = self._build_totals(self._timings)
        report = TelemetryReport(
            metadata=metadata,
            counters=counters,
            gauges=gauges,
            timings=timings,
            totals=totals,
        )
        return report.to_dict()

    def _build_totals(self, timings: List[TimingRecord]) -> Dict[str, Any]:
        return {
            "tnlcm": {
                "creacion": self._aggregate_timings(
                    timings, {("orchestrator", "create"), ("tnlcm", "create")}
                ),
                "activacion": self._aggregate_timings(timings, {("tnlcm", "activate")}),
                "destruccion": self._aggregate_timings(timings, {("tnlcm", "destroy")}),
                "purged": self._aggregate_timings(timings, {("tnlcm", "purged")}),
            },
            "elcm": {
                "experimento_completo": self._aggregate_timings(
                    timings, {("orchestrator", "elcm_phase")}
                ),
            },
        }

    def _aggregate_timings(
        self,
        timings: List[TimingRecord],
        match_pairs: set[tuple[str, str]],
    ) -> Dict[str, Any]:
        matched = [item for item in timings if (item.service, item.operation) in match_pairs]
        total_seconds = sum(item.duration_seconds for item in matched)
        return {
            "count": len(matched),
            "duration_seconds": total_seconds,
            "duration_display": format_duration_display(total_seconds),
        }


# Singleton instance used by the application
telemetry = Telemetry()
