"""Production-grade secure document and file reading tool supporting PDF, DOCX, TXT, MD, JSON, CSV, and XLSX."""

import csv
import json
import logging
import os
from pathlib import Path
from typing import Any

from src.tools.base import BaseTool
from src.tools.permissions import ToolPermission

logger = logging.getLogger(__name__)

# Maximum file size permitted for reading (20 MB)
MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024

# Default base directory for uploaded documents
DEFAULT_STORAGE_ROOT = Path("data/uploads").resolve()


def get_safe_path(file_path: str, storage_root: Path | str | None = None) -> Path:
    """Resolve and enforce sandbox path boundaries against directory traversal attacks."""
    root = Path(storage_root or DEFAULT_STORAGE_ROOT).resolve()
    # Ensure storage root exists
    root.mkdir(parents=True, exist_ok=True)

    target = Path(file_path)
    if not target.is_absolute():
        target = (root / target).resolve()
    else:
        target = target.resolve()

    # Enforce path containment within storage root
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise PermissionError(f"Access denied: Path '{file_path}' escapes authorized storage directory.") from exc

    if not target.exists():
        raise FileNotFoundError(f"File '{target.name}' not found in storage.")
    if not target.is_file():
        raise ValueError(f"Path '{target.name}' is not a regular file.")

    # Enforce size limit
    size = target.stat().st_size
    if size > MAX_FILE_SIZE_BYTES:
        raise ValueError(f"File '{target.name}' ({size / (1024*1024):.1f}MB) exceeds maximum allowed size of 20MB.")

    return target


class FileReaderTool(BaseTool):
    """Tool for reading and inspecting uploaded PDF, DOCX, TXT, MD, JSON, CSV, and XLSX files."""

    def __init__(self, storage_root: Path | str | None = None) -> None:
        self.storage_root = Path(storage_root).resolve() if storage_root else DEFAULT_STORAGE_ROOT

    @property
    def name(self) -> str:
        return "file_reader"

    @property
    def description(self) -> str:
        return (
            "Safely read and inspect authorized uploaded files: extract text, read tables, "
            "and inspect metadata from PDF, DOCX, TXT, Markdown, JSON, CSV, and XLSX spreadsheets. "
            "Supports pagination, sheet selection, and line limits."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Relative filename or path of the uploaded file inside the uploads directory.",
                },
                "action": {
                    "type": "string",
                    "description": "Operation to perform.",
                    "enum": ["read_text", "inspect_metadata", "read_table", "list_files"],
                },
                "page_start": {
                    "type": "integer",
                    "description": "1-based starting page index for PDF documents.",
                },
                "page_end": {
                    "type": "integer",
                    "description": "1-based ending page index for PDF documents.",
                },
                "max_lines": {
                    "type": "integer",
                    "description": "Maximum text lines to read (default 200).",
                },
                "sheet_name": {
                    "type": "string",
                    "description": "Sheet name for XLSX spreadsheets (defaults to first sheet).",
                },
                "max_rows": {
                    "type": "integer",
                    "description": "Maximum rows to extract for CSV or XLSX tables (default 50).",
                },
            },
            "required": ["action"],
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        action = kwargs.get("action", "read_text")

        if action == "list_files":
            self.storage_root.mkdir(parents=True, exist_ok=True)
            files = []
            for item in self.storage_root.rglob("*"):
                if item.is_file() and not item.name.startswith("."):
                    files.append({
                        "filename": str(item.relative_to(self.storage_root)),
                        "size_bytes": item.stat().st_size,
                        "extension": item.suffix.lower(),
                    })
            return {
                "action": "list_files",
                "total_files": len(files),
                "files": files[:50],
            }

        file_path_str = kwargs.get("file_path")
        if not file_path_str:
            raise ValueError("The 'file_path' parameter is required for file operations.")

        safe_file = get_safe_path(file_path_str, self.storage_root)
        ext = safe_file.suffix.lower()
        file_size = safe_file.stat().st_size

        if action == "inspect_metadata":
            meta: dict[str, Any] = {
                "filename": safe_file.name,
                "extension": ext,
                "size_bytes": file_size,
                "size_kb": round(file_size / 1024.0, 2),
            }

            if ext == ".pdf":
                try:
                    import pypdf
                    reader = pypdf.PdfReader(str(safe_file))
                    meta["page_count"] = len(reader.pages)
                    meta["is_encrypted"] = reader.is_encrypted
                    doc_info = reader.metadata or {}
                    meta["pdf_info"] = {k.strip("/"): str(v) for k, v in doc_info.items() if v}
                except Exception as exc:
                    meta["pdf_error"] = str(exc)

            elif ext == ".docx":
                try:
                    import docx
                    doc = docx.Document(str(safe_file))
                    meta["paragraphs_count"] = len(doc.paragraphs)
                    meta["tables_count"] = len(doc.tables)
                except Exception as exc:
                    meta["docx_error"] = str(exc)

            elif ext == ".xlsx":
                try:
                    import openpyxl
                    wb = openpyxl.load_workbook(str(safe_file), read_only=True)
                    meta["sheets"] = wb.sheetnames
                    wb.close()
                except Exception as exc:
                    meta["xlsx_error"] = str(exc)

            elif ext in (".csv", ".json", ".txt", ".md"):
                meta["is_text_based"] = True

            return {
                "action": "inspect_metadata",
                "metadata": meta,
            }

        elif action == "read_text":
            max_lines = kwargs.get("max_lines", 200)

            if ext in (".txt", ".md"):
                try:
                    lines = []
                    with open(safe_file, "r", encoding="utf-8", errors="replace") as f:
                        for line in f:
                            lines.append(line)
                            if len(lines) >= max_lines:
                                break
                    content = "".join(lines)
                    return {
                        "action": "read_text",
                        "filename": safe_file.name,
                        "type": ext,
                        "lines_read": len(lines),
                        "content": content,
                    }
                except Exception as exc:
                    raise ValueError(f"Error reading text file '{safe_file.name}': {exc}") from exc

            elif ext == ".json":
                try:
                    with open(safe_file, "r", encoding="utf-8", errors="replace") as f:
                        data = json.load(f)
                    preview = json.dumps(data, indent=2)[:10000]
                    return {
                        "action": "read_text",
                        "filename": safe_file.name,
                        "type": "json",
                        "content_preview": preview,
                        "is_dict": isinstance(data, dict),
                        "top_keys": list(data.keys()) if isinstance(data, dict) else len(data),
                    }
                except Exception as exc:
                    raise ValueError(f"Malformed JSON in file '{safe_file.name}': {exc}") from exc

            elif ext == ".pdf":
                try:
                    import pypdf
                    reader = pypdf.PdfReader(str(safe_file))
                    if reader.is_encrypted:
                        raise ValueError(f"PDF '{safe_file.name}' is password protected or encrypted.")

                    total_pages = len(reader.pages)
                    p_start = max(1, kwargs.get("page_start", 1))
                    p_end = min(total_pages, kwargs.get("page_end", total_pages))
                    if p_start > p_end:
                        p_start = 1

                    extracted_pages = []
                    for idx in range(p_start - 1, p_end):
                        page = reader.pages[idx]
                        page_text = page.extract_text() or ""
                        extracted_pages.append(f"--- Page {idx + 1} ---\n{page_text.strip()}")
                        if sum(len(p) for p in extracted_pages) > 20000:
                            extracted_pages.append("... [Truncated remaining pages to respect size limit]")
                            break

                    return {
                        "action": "read_text",
                        "filename": safe_file.name,
                        "total_pages": total_pages,
                        "pages_extracted": f"{p_start} to {min(p_end, idx + 1)}",
                        "content": "\n\n".join(extracted_pages),
                    }
                except Exception as exc:
                    raise ValueError(f"Failed extracting PDF text from '{safe_file.name}': {exc}") from exc

            elif ext == ".docx":
                try:
                    import docx
                    doc = docx.Document(str(safe_file))
                    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
                    return {
                        "action": "read_text",
                        "filename": safe_file.name,
                        "paragraphs_count": len(paragraphs),
                        "content": "\n\n".join(paragraphs[:max_lines]),
                    }
                except Exception as exc:
                    raise ValueError(f"Error reading DOCX '{safe_file.name}': {exc}") from exc

            elif ext == ".csv":
                try:
                    with open(safe_file, "r", encoding="utf-8", errors="replace") as f:
                        reader = csv.reader(f)
                        rows = [row for _, row in zip(range(max_lines), reader)]
                    header = rows[0] if rows else []
                    return {
                        "action": "read_text",
                        "filename": safe_file.name,
                        "headers": header,
                        "rows_count": len(rows),
                        "rows": rows[:max_lines],
                    }
                except Exception as exc:
                    raise ValueError(f"Error reading CSV '{safe_file.name}': {exc}") from exc

            elif ext == ".xlsx":
                try:
                    import openpyxl
                    wb = openpyxl.load_workbook(str(safe_file), read_only=True, data_only=True)
                    sheet_name = kwargs.get("sheet_name") or wb.sheetnames[0]
                    sheet = wb[sheet_name]
                    rows = []
                    for r_idx, row in enumerate(sheet.iter_rows(values_only=True)):
                        if r_idx >= max_lines:
                            break
                        rows.append([str(c) if c is not None else "" for c in row])
                    wb.close()

                    return {
                        "action": "read_text",
                        "filename": safe_file.name,
                        "sheet_name": sheet_name,
                        "rows_read": len(rows),
                        "rows": rows,
                    }
                except Exception as exc:
                    raise ValueError(f"Error reading XLSX '{safe_file.name}': {exc}") from exc

            raise ValueError(f"Unsupported file format '{ext}'. Supported: PDF, DOCX, TXT, MD, JSON, CSV, XLSX.")

        elif action == "read_table":
            max_rows = kwargs.get("max_rows", 50)
            if ext == ".csv":
                with open(safe_file, "r", encoding="utf-8", errors="replace") as f:
                    reader = csv.DictReader(f)
                    records = [row for _, row in zip(range(max_rows), reader)]
                return {
                    "action": "read_table",
                    "filename": safe_file.name,
                    "fieldnames": reader.fieldnames,
                    "rows_extracted": len(records),
                    "data": records,
                }
            elif ext == ".xlsx":
                import openpyxl
                wb = openpyxl.load_workbook(str(safe_file), read_only=True, data_only=True)
                sheet_name = kwargs.get("sheet_name") or wb.sheetnames[0]
                sheet = wb[sheet_name]
                rows = list(sheet.iter_rows(values_only=True))
                wb.close()
                if not rows:
                    return {"action": "read_table", "filename": safe_file.name, "data": []}
                headers = [str(h) if h is not None else f"col_{i}" for i, h in enumerate(rows[0])]
                records = []
                for r in rows[1:max_rows + 1]:
                    row_dict = {h: (str(v) if v is not None else "") for h, v in zip(headers, r)}
                    records.append(row_dict)
                return {
                    "action": "read_table",
                    "filename": safe_file.name,
                    "sheet_name": sheet_name,
                    "headers": headers,
                    "data": records,
                }
            elif ext == ".docx":
                import docx
                doc = docx.Document(str(safe_file))
                tables_data = []
                for t_idx, table in enumerate(doc.tables[:3]):
                    table_rows = []
                    for row in table.rows[:max_rows]:
                        table_rows.append([cell.text.strip() for cell in row.cells])
                    tables_data.append({"table_index": t_idx + 1, "rows": table_rows})
                return {
                    "action": "read_table",
                    "filename": safe_file.name,
                    "tables_found": len(doc.tables),
                    "tables": tables_data,
                }

            raise ValueError(f"Table extraction is only supported for CSV, XLSX, and DOCX files (got '{ext}').")

        raise ValueError(f"Unknown action '{action}' for file_reader.")

