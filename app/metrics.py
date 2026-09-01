"""Privacy-friendly XML counters for lightweight site metrics."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from xml.etree import ElementTree as ET

try:  # pragma: no cover - Linux/macOS
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

try:  # pragma: no cover - Windows
    import msvcrt
except ImportError:  # pragma: no cover - Unix
    msvcrt = None

_METRIC_KEYS = (
    "total_visitors",
    "total_actions",
    "successful_actions",
    "failed_actions",
    "satisfied_responses",
    "not_satisfied_responses",
    "contact_requests",
    "contact_issues",
    "contact_concerns",
    "contact_suggestions",
    "contact_improvements",
    "contact_other",
    "xml_file_count_metrics",
)

_MEMORY_METRICS: dict[str, int] = {}


def _metrics_path() -> Path:
    # Serverless deployments (including Vercel) often mount the source tree as
    # read-only. Store metrics in a writable temp location instead of beside the
    # app code, and fall back to an in-memory store if the filesystem is not
    # writable for the current runtime.
    base_dir = os.environ.get("METRICS_DIR") or tempfile.gettempdir()
    return Path(base_dir) / "spot-xml-merger-site-metrics.xml"


def _default_metrics() -> dict[str, int | str]:
    data = {key: 0 for key in _METRIC_KEYS}
    data["xml_file_count_metrics"] = "{}"
    return data


def _memory_metrics() -> dict[str, int]:
    values = _default_metrics()
    values.update(_MEMORY_METRICS)
    return values


def _as_int(value, default=0) -> int:
    try:
        return int((value or "").strip() or default)
    except (TypeError, ValueError):
        return default


def _tree_from_data(values: dict[str, int | str]) -> ET.Element:
    root = ET.Element("site_metrics")
    for key in _METRIC_KEYS:
        node = ET.SubElement(root, key)
        value = values.get(key, 0)
        if key == "xml_file_count_metrics":
            node.text = str(value if isinstance(value, str) else json.dumps(value, sort_keys=True))
        else:
            node.text = str(int(value or 0))
    return root


def _read_metrics_file(path: Path) -> dict[str, int | str]:
    if not path.exists():
        return _default_metrics()
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return _default_metrics()
    data = _default_metrics()
    for key in _METRIC_KEYS:
        node = root.find(key)
        if node is None:
            continue
        if key == "xml_file_count_metrics":
            raw = (node.text or "").strip()
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                parsed = {}
            data[key] = json.dumps(parsed, sort_keys=True)
        else:
            data[key] = _as_int(node.text)
    return data


@contextmanager
def _locked_metrics(path: Path):
    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "a+b") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        elif msvcrt is not None:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)

        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


def _write_metrics_file(path: Path, values: dict[str, int]) -> None:
    root = _tree_from_data(values)
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="site_metrics.", suffix=".tmp", dir=str(parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(b'<?xml version="1.0" encoding="utf-8"?>\n')
            handle.write(ET.tostring(root, encoding="utf-8", xml_declaration=False))
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def ensure_metrics_file() -> Path:
    path = _metrics_path()
    if path.exists():
        return path
    try:
        _write_metrics_file(path, _default_metrics())
    except (OSError, PermissionError, RuntimeError, ValueError):
        # Serverless runtimes can make the project tree read-only or mount it
        # into an isolated filesystem. The in-memory fallback below keeps the
        # endpoints alive instead of crashing the invocation.
        return path
    return path


def read_metrics() -> dict[str, int]:
    try:
        path = ensure_metrics_file()
        with _locked_metrics(path):
            return _read_metrics_file(path)
    except (OSError, PermissionError, RuntimeError, ValueError):
        return _memory_metrics()


def update_metrics(**changes: int) -> dict[str, int]:
    values = _memory_metrics()
    try:
        path = ensure_metrics_file()
        with _locked_metrics(path):
            values = _read_metrics_file(path)
            for key, delta in changes.items():
                if key not in values:
                    continue
                values[key] = int(values.get(key, 0)) + int(delta)
            _write_metrics_file(path, values)
    except (OSError, PermissionError, RuntimeError, ValueError):
        values = _memory_metrics()
        for key, delta in changes.items():
            if key not in values:
                continue
            values[key] = int(values.get(key, 0)) + int(delta)
    _MEMORY_METRICS.clear()
    _MEMORY_METRICS.update(values)
    return values.copy()


def record_visitor() -> dict[str, int]:
    return update_metrics(total_visitors=1)


def record_action(success: bool, satisfied: bool | None = None) -> dict[str, int]:
    values = {"total_actions": 1}
    if success:
        values["successful_actions"] = 1
    else:
        values["failed_actions"] = 1
    if satisfied is True:
        values["satisfied_responses"] = 1
    elif satisfied is False:
        values["not_satisfied_responses"] = 1
    return update_metrics(**values)


def record_contact_submission(category: str) -> dict[str, int]:
    normalized = (category or "other").strip().lower()
    category_map = {
        "issue": "contact_issues",
        "concern": "contact_concerns",
        "suggestion": "contact_suggestions",
        "improvement": "contact_improvements",
    }
    metric_key = category_map.get(normalized, "contact_other")
    return update_metrics(contact_requests=1, **{metric_key: 1})


def record_xml_file_count(file_count: int, completed: bool) -> dict[str, object]:
    count = int(file_count or 0)
    if count <= 0:
        return {"xml_file_count_metrics": {}}

    payload = {}
    existing = read_metrics().get("xml_file_count_metrics") or "{}"
    try:
        payload = json.loads(existing) if isinstance(existing, str) else dict(existing)
    except json.JSONDecodeError:
        payload = {}

    key = str(count)
    bucket = payload.setdefault(key, {"completed": 0, "not_completed": 0})
    if not isinstance(bucket, dict):
        bucket = {"completed": 0, "not_completed": 0}
        payload[key] = bucket

    if completed:
        bucket["completed"] = int(bucket.get("completed", 0)) + 1
    else:
        bucket["not_completed"] = int(bucket.get("not_completed", 0)) + 1

    serialized = json.dumps(payload, sort_keys=True)
    values = _memory_metrics()
    values["xml_file_count_metrics"] = serialized
    _MEMORY_METRICS.clear()
    _MEMORY_METRICS.update(values)
    try:
        path = ensure_metrics_file()
        with _locked_metrics(path):
            current = _read_metrics_file(path)
            current["xml_file_count_metrics"] = serialized
            _write_metrics_file(path, current)
    except (OSError, PermissionError, RuntimeError, ValueError):
        pass
    return {"xml_file_count_metrics": payload}


def metrics_payload() -> dict[str, float | int | dict]:
    data = read_metrics()
    visitors = int(data.get("total_visitors", 0))
    actions = int(data.get("total_actions", 0))
    successful = int(data.get("successful_actions", 0))
    failed = int(data.get("failed_actions", 0))
    satisfied = int(data.get("satisfied_responses", 0))
    not_satisfied = int(data.get("not_satisfied_responses", 0))
    feedback_responses = satisfied + not_satisfied

    raw_xml_metrics = data.get("xml_file_count_metrics", "{}") or "{}"
    try:
        xml_file_count_metrics = json.loads(raw_xml_metrics) if isinstance(raw_xml_metrics, str) else dict(raw_xml_metrics)
    except json.JSONDecodeError:
        xml_file_count_metrics = {}

    payload = {
        "total_visitors": visitors,
        "total_actions": actions,
        "successful_actions": successful,
        "failed_actions": failed,
        "satisfied_responses": satisfied,
        "not_satisfied_responses": not_satisfied,
        "contact_requests": int(data.get("contact_requests", 0)),
        "contact_issues": int(data.get("contact_issues", 0)),
        "contact_concerns": int(data.get("contact_concerns", 0)),
        "contact_suggestions": int(data.get("contact_suggestions", 0)),
        "contact_improvements": int(data.get("contact_improvements", 0)),
        "contact_other": int(data.get("contact_other", 0)),
        "xml_file_count_metrics": xml_file_count_metrics,
        "action_rate": round((actions / visitors), 4) if visitors else 0.0,
        "conversion_rate": round((successful / actions), 4) if actions else 0.0,
        "satisfaction_rate": round((satisfied / feedback_responses), 4) if feedback_responses else 0.0,
        "feedback_responses": feedback_responses,
    }
    return payload
