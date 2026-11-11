"""Production-grade JSON and CSV data processing, validation, filtering, transformation, and conversion tool."""

import csv
import io
import json
import logging
from pathlib import Path
import re
from typing import Any

from src.tools.base import BaseTool
from src.tools.permissions import ToolPermission

logger = logging.getLogger(__name__)

# Security boundaries
MAX_RAW_INPUT_CHARS = 5 * 1024 * 1024  # 5 MB
MAX_RECORDS_PROCESSING = 10000
DEFAULT_STORAGE_ROOT = Path("data").resolve()


def _get_safe_path(file_path: str, storage_root: Path | str | None = None) -> Path:
    """Resolve file path ensuring it does not escape the authorized storage sandbox."""
    root = Path(storage_root or DEFAULT_STORAGE_ROOT).resolve()
    target = Path(file_path)
    if not target.is_absolute():
        target = (root / target).resolve()
    else:
        target = target.resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise PermissionError(f"Access denied: Path '{file_path}' escapes authorized storage.") from exc
    if not target.exists() or not target.is_file():
        raise FileNotFoundError(f"File '{target.name}' not found.")
    return target


def _extract_nested_value(data: Any, path: str) -> Any:
    """Extract nested value using dot notation and array indexing e.g. 'users[0].address.city'."""
    tokens = re.split(r"\.|\[(\d+)\]", path)
    current = data
    for token in tokens:
        if token is None or token == "":
            continue
        if token.isdigit():
            idx = int(token)
            if isinstance(current, (list, tuple)) and 0 <= idx < len(current):
                current = current[idx]
            else:
                return None
        elif isinstance(current, dict):
            current = current.get(token)
        else:
            return None
    return current


def _match_condition(row: dict[str, Any], field: str, op: str, value: Any) -> bool:
    """Safely evaluate record filter condition without eval."""
    row_val = row.get(field)
    if row_val is None:
        return op in ("!=", "is_null", "null")

    # Try casting for numeric comparisons if possible
    if isinstance(value, (int, float)) and isinstance(row_val, str):
        try:
            row_val = float(row_val) if "." in row_val else int(row_val)
        except ValueError:
            pass

    try:
        if op in ("==", "eq", "="):
            return row_val == value
        elif op in ("!=", "neq", "<>"):
            return row_val != value
        elif op in (">", "gt"):
            return row_val > value
        elif op in (">=", "gte"):
            return row_val >= value
        elif op in ("<", "lt"):
            return row_val < value
        elif op in ("<=", "lte"):
            return row_val <= value
        elif op in ("contains", "in_str"):
            return str(value).lower() in str(row_val).lower()
        elif op in ("startswith", "starts_with"):
            return str(row_val).lower().startswith(str(value).lower())
        elif op in ("endswith", "ends_with"):
            return str(row_val).lower().endswith(str(value).lower())
        elif op == "in":
            return row_val in (value if isinstance(value, (list, tuple, set)) else [value])
        elif op in ("is_null", "null"):
            return row_val is None
        elif op in ("is_not_null", "not_null"):
            return row_val is not None
    except Exception:
        return False
    return False


class DataProcessorTool(BaseTool):
    """Production-grade tool for processing, filtering, transforming, validating, and converting JSON and CSV."""

    def __init__(self, storage_root: Path | str | None = None) -> None:
        self.storage_root = Path(storage_root).resolve() if storage_root else DEFAULT_STORAGE_ROOT

    @property
    def name(self) -> str:
        return "data_processor"

    @property
    def description(self) -> str:
        return (
            "Safely parse, validate, filter, transform, sort, summarize, and convert structured data. "
            "Supports JSON and CSV formats from strings or authorized sandboxed files. "
            "Enforces strict memory bounds without arbitrary code execution."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Processing action to perform.",
                    "enum": [
                        "parse_validate",
                        "filter",
                        "select_columns",
                        "transform",
                        "sort",
                        "extract_nested",
                        "convert",
                        "summarize",
                    ],
                },
                "data_string": {
                    "type": "string",
                    "description": "Raw JSON or CSV string content to process.",
                },
                "file_path": {
                    "type": "string",
                    "description": "Relative path to a sandboxed JSON or CSV file in storage.",
                },
                "format": {
                    "type": "string",
                    "description": "Data format ('json' or 'csv'). Autodetected if omitted.",
                    "enum": ["json", "csv"],
                },
                "filter_conditions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "field": {"type": "string"},
                            "operator": {"type": "string"},
                            "value": {},
                        },
                        "required": ["field", "operator", "value"],
                    },
                    "description": "List of filter conditions for action='filter'.",
                },
                "columns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of column names for action='select_columns'.",
                },
                "rename_map": {
                    "type": "object",
                    "description": "Key-value mapping of old_column -> new_column for action='transform'.",
                },
                "sort_key": {
                    "type": "string",
                    "description": "Field name to sort by for action='sort'.",
                },
                "sort_descending": {
                    "type": "boolean",
                    "description": "Whether to sort in descending order (default false).",
                },
                "json_path": {
                    "type": "string",
                    "description": "Dot notation path for action='extract_nested' (e.g. 'users[0].address.city').",
                },
                "convert_target": {
                    "type": "string",
                    "description": "Target format for action='convert' ('json' or 'csv').",
                    "enum": ["json", "csv"],
                },
                "max_rows": {
                    "type": "integer",
                    "description": "Maximum rows to return or process (default 100, max 1000).",
                },
            },
            "required": ["action"],
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    def _load_data(self, kwargs: dict[str, Any]) -> tuple[Any, str]:
        """Load and parse input data from string or file, detecting format."""
        raw_str = kwargs.get("data_string")
        file_path = kwargs.get("file_path")
        fmt = kwargs.get("format")

        if not raw_str and not file_path:
            raise ValueError("Either 'data_string' or 'file_path' must be provided.")

        content = ""
        if file_path:
            target = _get_safe_path(file_path, self.storage_root)
            if not fmt:
                fmt = "json" if target.suffix.lower() == ".json" else "csv" if target.suffix.lower() == ".csv" else None
            content = target.read_text(encoding="utf-8", errors="replace")[:MAX_RAW_INPUT_CHARS]
        elif raw_str:
            if len(raw_str) > MAX_RAW_INPUT_CHARS:
                raise ValueError(f"Input string exceeds maximum allowed limit of {MAX_RAW_INPUT_CHARS / (1024*1024):.1f}MB.")
            content = raw_str

        # Format detection if not specified
        if not fmt:
            stripped = content.strip()
            if (stripped.startswith("{") and stripped.endswith("}")) or (stripped.startswith("[") and stripped.endswith("]")):
                fmt = "json"
            else:
                fmt = "csv"

        if fmt == "json":
            try:
                parsed = json.loads(content)
                return parsed, "json"
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON syntax at line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc
        else:
            try:
                reader = csv.DictReader(io.StringIO(content))
                rows = list(reader)
                return rows, "csv"
            except Exception as exc:
                raise ValueError(f"Failed parsing CSV content: {exc}") from exc

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        action = kwargs.get("action", "summarize")

        data, fmt = self._load_data(kwargs)
        max_rows = min(max(int(kwargs.get("max_rows", 100)), 1), 1000)

        if action == "parse_validate":
            is_valid = True
            data_type = type(data).__name__
            item_count = len(data) if isinstance(data, (list, dict)) else 1
            keys = list(data.keys()) if isinstance(data, dict) else list(data[0].keys()) if isinstance(data, list) and data and isinstance(data[0], dict) else []
            return {
                "action": "parse_validate",
                "valid": is_valid,
                "detected_format": fmt,
                "root_type": data_type,
                "element_count": item_count,
                "top_keys": keys[:50],
            }

        elif action == "extract_nested":
            path = kwargs.get("json_path")
            if not path:
                raise ValueError("The 'json_path' parameter is required for action='extract_nested'.")
            val = _extract_nested_value(data, path)
            return {
                "action": "extract_nested",
                "path": path,
                "found": val is not None,
                "value": val,
            }

        # Normalize to list of records for tabular operations
        records: list[dict[str, Any]] = []
        if isinstance(data, list) and all(isinstance(x, dict) for x in data):
            records = data[:MAX_RECORDS_PROCESSING]
        elif isinstance(data, dict):
            # Check if dict wraps a list
            for v in data.values():
                if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                    records = v[:MAX_RECORDS_PROCESSING]
                    break
            if not records:
                records = [data]
        else:
            raise ValueError("Tabular operations require a list of objects or a CSV dataset.")

        if action == "filter":
            conds = kwargs.get("filter_conditions") or []
            if not conds:
                raise ValueError("At least one filter condition in 'filter_conditions' is required.")

            filtered = []
            for row in records:
                match = True
                for c in conds:
                    field = c.get("field")
                    op = c.get("operator", "==")
                    target_val = c.get("value")
                    if not _match_condition(row, field, op, target_val):
                        match = False
                        break
                if match:
                    filtered.append(row)

            return {
                "action": "filter",
                "original_count": len(records),
                "matched_count": len(filtered),
                "records": filtered[:max_rows],
            }

        elif action == "select_columns":
            cols = kwargs.get("columns") or []
            if not cols:
                raise ValueError("The 'columns' array parameter is required for action='select_columns'.")

            projected = []
            for row in records:
                projected.append({col: row.get(col) for col in cols})

            return {
                "action": "select_columns",
                "columns_selected": cols,
                "records_count": len(projected),
                "records": projected[:max_rows],
            }

        elif action == "transform":
            rename_map = kwargs.get("rename_map") or {}
            transformed = []
            for row in records:
                new_row = {}
                for k, v in row.items():
                    target_k = rename_map.get(k, k)
                    new_row[target_k] = v
                transformed.append(new_row)

            return {
                "action": "transform",
                "transformed_count": len(transformed),
                "records": transformed[:max_rows],
            }

        elif action == "sort":
            sort_key = kwargs.get("sort_key")
            if not sort_key:
                raise ValueError("The 'sort_key' parameter is required for action='sort'.")
            descending = bool(kwargs.get("sort_descending", False))

            def _sort_val(row: dict[str, Any]) -> Any:
                v = row.get(sort_key)
                if v is None:
                    return ""
                if isinstance(v, (int, float)):
                    return v
                try:
                    return float(v)
                except (ValueError, TypeError):
                    return str(v).lower()

            sorted_records = sorted(records, key=_sort_val, reverse=descending)
            return {
                "action": "sort",
                "sort_key": sort_key,
                "descending": descending,
                "records_count": len(sorted_records),
                "records": sorted_records[:max_rows],
            }

        elif action == "convert":
            target_fmt = kwargs.get("convert_target", "json" if fmt == "csv" else "csv")
            if target_fmt == "json":
                out_str = json.dumps(records[:max_rows], indent=2)
                return {
                    "action": "convert",
                    "from_format": fmt,
                    "to_format": "json",
                    "records_count": len(records[:max_rows]),
                    "output": out_str,
                }
            elif target_fmt == "csv":
                if not records:
                    return {"action": "convert", "from_format": fmt, "to_format": "csv", "output": ""}
                fieldnames = list(records[0].keys())
                buf = io.StringIO()
                writer = csv.DictWriter(buf, fieldnames=fieldnames)
                writer.writeheader()
                for r in records[:max_rows]:
                    writer.writerow({k: r.get(k, "") for k in fieldnames})
                return {
                    "action": "convert",
                    "from_format": fmt,
                    "to_format": "csv",
                    "records_count": len(records[:max_rows]),
                    "output": buf.getvalue(),
                }
            else:
                raise ValueError(f"Unsupported convert target '{target_fmt}'. Supported: 'json', 'csv'.")

        elif action == "summarize":
            if not records:
                return {"action": "summarize", "total_records": 0, "columns": []}

            cols = list(records[0].keys())
            null_counts = {c: 0 for c in cols}
            inferred_types = {c: set() for c in cols}

            for row in records:
                for c in cols:
                    val = row.get(c)
                    if val is None or val == "":
                        null_counts[c] += 1
                    else:
                        inferred_types[c].add(type(val).__name__)

            col_summaries = []
            for c in cols:
                types_str = ", ".join(sorted(inferred_types[c])) or "null"
                col_summaries.append({
                    "column": c,
                    "inferred_types": types_str,
                    "null_count": null_counts[c],
                    "completeness_pct": round((1.0 - (null_counts[c] / len(records))) * 100.0, 1),
                })

            return {
                "action": "summarize",
                "total_records": len(records),
                "column_count": len(cols),
                "columns": col_summaries,
                "sample_preview": records[:3],
            }

        raise ValueError(f"Unknown action '{action}' for data_processor.")

