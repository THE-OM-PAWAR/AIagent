"""Production-grade bounded spreadsheet analysis tool supporting CSV and XLSX files."""

import csv
import logging
import math
from pathlib import Path
import statistics
from typing import Any

from src.tools.base import BaseTool
from src.tools.permissions import ToolPermission

logger = logging.getLogger(__name__)

DEFAULT_STORAGE_ROOT = Path("data").resolve()
MAX_ANALYSIS_ROWS = 10000


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
        raise FileNotFoundError(f"Spreadsheet file '{target.name}' not found.")
    return target


def _infer_type(values: list[Any]) -> str:
    """Infer dominant column data type from sample values."""
    types = []
    for v in values:
        if v is None or v == "":
            continue
        if isinstance(v, (int, float)):
            types.append("numeric")
        elif isinstance(v, bool):
            types.append("boolean")
        else:
            s = str(v).strip()
            try:
                float(s)
                types.append("numeric")
            except ValueError:
                types.append("text")
    if not types:
        return "empty"
    numeric_count = types.count("numeric")
    if numeric_count / len(types) >= 0.8:
        return "numeric"
    return "text"


class SpreadsheetAnalyzerTool(BaseTool):
    """Deep analytical inspection, statistics, aggregation, and comparison for CSV and XLSX spreadsheets."""

    def __init__(self, storage_root: Path | str | None = None) -> None:
        self.storage_root = Path(storage_root).resolve() if storage_root else DEFAULT_STORAGE_ROOT

    @property
    def name(self) -> str:
        return "spreadsheet_analyzer"

    @property
    def description(self) -> str:
        return (
            "Analyze CSV and Excel (.xlsx) spreadsheets with bounded memory. "
            "Inspect worksheets and schemas, compute statistical summaries (mean, median, std dev, quartiles), "
            "detect missing values and duplicates, compute group aggregations, compare datasets, "
            "and generate concise analytical reports without altering source files."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Relative path to CSV or XLSX spreadsheet in storage.",
                },
                "action": {
                    "type": "string",
                    "description": "Analysis action to perform.",
                    "enum": [
                        "inspect",
                        "summary_statistics",
                        "missing_values",
                        "detect_duplicates",
                        "group_aggregates",
                        "filter_and_sort",
                        "compare_sheets",
                        "generate_report",
                    ],
                },
                "sheet_name": {
                    "type": "string",
                    "description": "Worksheet name for XLSX files (default is first sheet).",
                },
                "comparison_file_path": {
                    "type": "string",
                    "description": "Second spreadsheet path for action='compare_sheets'.",
                },
                "comparison_sheet_name": {
                    "type": "string",
                    "description": "Second worksheet name for action='compare_sheets'.",
                },
                "group_by_column": {
                    "type": "string",
                    "description": "Categorical column name for action='group_aggregates'.",
                },
                "aggregate_column": {
                    "type": "string",
                    "description": "Numeric column name for action='group_aggregates'.",
                },
                "max_rows": {
                    "type": "integer",
                    "description": "Maximum rows to inspect or sample (default 1000, max 10000).",
                },
            },
            "required": ["file_path", "action"],
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    def _load_sheet_data(
        self, file_path: str, sheet_name: str | None = None, max_rows: int = MAX_ANALYSIS_ROWS
    ) -> tuple[list[str], list[list[Any]], list[str]]:
        """Load sheet headers, rows, and list of available sheets safely."""
        target = _get_safe_path(file_path, self.storage_root)
        ext = target.suffix.lower()

        if ext == ".csv":
            sheet_names = ["Sheet1"]
            with open(target, "r", encoding="utf-8", errors="replace") as f:
                reader = csv.reader(f)
                headers = next(reader, [])
                rows = []
                for _, row in zip(range(max_rows), reader):
                    rows.append(row)
            return headers, rows, sheet_names

        elif ext == ".xlsx":
            import openpyxl

            wb = openpyxl.load_workbook(str(target), read_only=True, data_only=True)
            sheet_names = wb.sheetnames
            chosen_sheet = sheet_name if sheet_name and sheet_name in sheet_names else sheet_names[0]
            ws = wb[chosen_sheet]

            rows_iter = ws.iter_rows(values_only=True)
            first_row = next(rows_iter, None)
            if not first_row:
                wb.close()
                return [], [], sheet_names

            headers = [str(h) if h is not None else f"col_{i}" for i, h in enumerate(first_row)]
            rows = []
            for _, r in zip(range(max_rows), rows_iter):
                rows.append(list(r))
            wb.close()
            return headers, rows, sheet_names

        else:
            raise ValueError(f"Unsupported spreadsheet format '{ext}'. Supported formats: .csv, .xlsx.")

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        file_path = kwargs.get("file_path")
        if not file_path:
            raise ValueError("The 'file_path' parameter is required.")

        action = kwargs.get("action", "inspect")
        sheet_name = kwargs.get("sheet_name")
        max_rows = min(max(int(kwargs.get("max_rows", 1000)), 10), MAX_ANALYSIS_ROWS)

        headers, rows, available_sheets = self._load_sheet_data(file_path, sheet_name, max_rows)
        active_sheet = sheet_name or available_sheets[0]

        if action == "inspect":
            col_types = {}
            for col_idx, col_name in enumerate(headers):
                sample_vals = [r[col_idx] for r in rows[:100] if col_idx < len(r)]
                col_types[col_name] = _infer_type(sample_vals)

            return {
                "action": "inspect",
                "file": Path(file_path).name,
                "available_sheets": available_sheets,
                "active_sheet": active_sheet,
                "total_rows_inspected": len(rows),
                "total_columns": len(headers),
                "columns": [{"name": h, "inferred_type": col_types.get(h, "text")} for h in headers],
                "sample_first_row": rows[0] if rows else [],
            }

        elif action == "summary_statistics":
            stats: dict[str, Any] = {}
            for col_idx, col_name in enumerate(headers):
                vals = [r[col_idx] for r in rows if col_idx < len(r) and r[col_idx] is not None and r[col_idx] != ""]
                t = _infer_type(vals)

                if t == "numeric" and vals:
                    num_vals = []
                    for v in vals:
                        try:
                            num_vals.append(float(v))
                        except (ValueError, TypeError):
                            pass

                    if num_vals:
                        sorted_vals = sorted(num_vals)
                        n = len(sorted_vals)
                        q25 = sorted_vals[int(0.25 * n)]
                        q75 = sorted_vals[int(0.75 * n)]
                        std = statistics.stdev(num_vals) if len(num_vals) > 1 else 0.0

                        stats[col_name] = {
                            "type": "numeric",
                            "count": len(num_vals),
                            "mean": round(statistics.mean(num_vals), 4),
                            "median": round(statistics.median(num_vals), 4),
                            "std_dev": round(std, 4),
                            "min": min(num_vals),
                            "max": max(num_vals),
                            "q25": q25,
                            "q75": q75,
                        }
                else:
                    str_vals = [str(v) for v in vals]
                    unique_count = len(set(str_vals))
                    most_common = max(set(str_vals), key=str_vals.count) if str_vals else None
                    stats[col_name] = {
                        "type": "categorical",
                        "count": len(str_vals),
                        "unique_count": unique_count,
                        "most_frequent": most_common,
                    }

            return {
                "action": "summary_statistics",
                "file": Path(file_path).name,
                "sheet": active_sheet,
                "rows_analyzed": len(rows),
                "column_statistics": stats,
            }

        elif action == "missing_values":
            missing_report = []
            for col_idx, col_name in enumerate(headers):
                null_count = sum(1 for r in rows if col_idx >= len(r) or r[col_idx] is None or r[col_idx] == "")
                pct = (null_count / len(rows) * 100.0) if rows else 0.0
                missing_report.append({
                    "column": col_name,
                    "missing_count": null_count,
                    "missing_percentage": round(pct, 2),
                })

            return {
                "action": "missing_values",
                "file": Path(file_path).name,
                "sheet": active_sheet,
                "total_rows": len(rows),
                "columns": missing_report,
            }

        elif action == "detect_duplicates":
            seen: set[str] = set()
            duplicates = 0
            sample_dups = []
            for r in rows:
                key = str(r)
                if key in seen:
                    duplicates += 1
                    if len(sample_dups) < 5:
                        sample_dups.append(r)
                else:
                    seen.add(key)

            return {
                "action": "detect_duplicates",
                "file": Path(file_path).name,
                "sheet": active_sheet,
                "total_rows": len(rows),
                "duplicate_count": duplicates,
                "unique_rows": len(seen),
                "sample_duplicates": sample_dups,
            }

        elif action == "group_aggregates":
            grp_col = kwargs.get("group_by_column")
            agg_col = kwargs.get("aggregate_column")
            if not grp_col or not agg_col:
                raise ValueError("Both 'group_by_column' and 'aggregate_column' are required for action='group_aggregates'.")

            if grp_col not in headers:
                raise ValueError(f"Column '{grp_col}' not found in headers {headers}.")
            if agg_col not in headers:
                raise ValueError(f"Column '{agg_col}' not found in headers {headers}.")

            grp_idx = headers.index(grp_col)
            agg_idx = headers.index(agg_col)

            groups: dict[str, list[float]] = {}
            for r in rows:
                if grp_idx < len(r) and agg_idx < len(r):
                    grp_val = str(r[grp_idx]) if r[grp_idx] is not None else "Unknown"
                    raw_agg = r[agg_idx]
                    try:
                        num = float(raw_agg)
                        groups.setdefault(grp_val, []).append(num)
                    except (ValueError, TypeError):
                        pass

            aggregated = []
            for g_name, nums in groups.items():
                if nums:
                    aggregated.append({
                        "group": g_name,
                        "count": len(nums),
                        "sum": round(sum(nums), 2),
                        "mean": round(statistics.mean(nums), 2),
                        "min": min(nums),
                        "max": max(nums),
                    })

            # Sort descending by sum
            aggregated.sort(key=lambda x: x["sum"], reverse=True)

            return {
                "action": "group_aggregates",
                "file": Path(file_path).name,
                "sheet": active_sheet,
                "group_by": grp_col,
                "aggregate_column": agg_col,
                "total_groups": len(aggregated),
                "groups": aggregated[:50],
            }

        elif action == "compare_sheets":
            comp_path = kwargs.get("comparison_file_path") or file_path
            comp_sheet = kwargs.get("comparison_sheet_name")
            if not comp_sheet and comp_path == file_path:
                raise ValueError("When comparing the same file, 'comparison_sheet_name' must differ.")

            headers2, rows2, _ = self._load_sheet_data(comp_path, comp_sheet, max_rows)

            cols_added = [c for c in headers2 if c not in headers]
            cols_removed = [c for c in headers if c not in headers2]
            common_cols = [c for c in headers if c in headers2]

            return {
                "action": "compare_sheets",
                "file_1": Path(file_path).name,
                "sheet_1": active_sheet,
                "rows_1": len(rows),
                "file_2": Path(comp_path).name,
                "sheet_2": comp_sheet or "Default",
                "rows_2": len(rows2),
                "columns_matching": cols_added == [] and cols_removed == [],
                "columns_added_in_target": cols_added,
                "columns_removed_in_target": cols_removed,
                "common_columns": common_cols,
            }

        elif action == "generate_report":
            # Concise markdown report summarizing dataset
            col_count = len(headers)
            row_count = len(rows)

            report_lines = [
                f"# Spreadsheet Analysis Report: {Path(file_path).name}",
                f"**Worksheet:** {active_sheet} | **Dimensions:** {row_count} rows × {col_count} columns",
                "",
                "## Column Overview",
                "| Column Name | Inferred Type | Completeness |",
                "| --- | --- | --- |",
            ]
            for col_idx, col_name in enumerate(headers):
                vals = [r[col_idx] for r in rows if col_idx < len(r)]
                t = _infer_type(vals)
                nulls = sum(1 for v in vals if v is None or v == "")
                comp_pct = round((1.0 - (nulls / len(rows))) * 100.0, 1) if rows else 0.0
                report_lines.append(f"| {col_name} | {t} | {comp_pct}% |")

            return {
                "action": "generate_report",
                "file": Path(file_path).name,
                "sheet": active_sheet,
                "report_markdown": "\n".join(report_lines),
            }

        raise ValueError(f"Unknown action '{action}' for spreadsheet_analyzer.")

