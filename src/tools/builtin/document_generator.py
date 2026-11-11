"""Production-grade document generator for Markdown, plain text, DOCX, and PDF reports."""

import logging
from pathlib import Path
import re
import time
from typing import Any

from src.tools.base import BaseTool
from src.tools.permissions import ToolPermission

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = Path("data/generated").resolve()
MAX_FILE_BYTES = 25 * 1024 * 1024  # 25 MB max generation limit


def _sanitize_filename(name: str, fallback_prefix: str, extension: str) -> str:
    """Sanitize custom user filename and append extension securely."""
    clean = re.sub(r"[^a-zA-Z0-9_\-.]", "_", name.strip()) if name else ""
    if not clean or clean.startswith("."):
        clean = f"{fallback_prefix}_{int(time.time())}"
    if not clean.lower().endswith(extension.lower()):
        clean = f"{clean}{extension}"
    return clean


class DocumentGeneratorTool(BaseTool):
    """Generates structured Markdown, plain text, DOCX, and PDF documents from sections, tables, and lists."""

    def __init__(self, output_dir: Path | str | None = None) -> None:
        self.output_dir = Path(output_dir).resolve() if output_dir else DEFAULT_OUTPUT_DIR

    @property
    def name(self) -> str:
        return "document_generator"

    @property
    def description(self) -> str:
        return (
            "Generate professional formatted documents in Markdown (.md), plain text (.txt), "
            "Microsoft Word (.docx), or PDF (.pdf) format. Supports headings, paragraphs, bullet lists, "
            "and structured data tables. Saves outputs to authorized storage and prepares them for Telegram delivery."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "format": {
                    "type": "string",
                    "description": "Output document format.",
                    "enum": ["markdown", "text", "docx", "pdf"],
                },
                "title": {
                    "type": "string",
                    "description": "Main title of the document or report.",
                },
                "filename": {
                    "type": "string",
                    "description": "Optional desired filename (e.g. 'quarterly_report.docx').",
                },
                "sections": {
                    "type": "array",
                    "description": "Structured sections containing headings, paragraphs, bullet points, and tables.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "heading": {"type": "string", "description": "Section heading."},
                            "content": {"type": "string", "description": "Section body text or paragraphs."},
                            "bullet_points": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "List of bullet points for this section.",
                            },
                            "table": {
                                "type": "object",
                                "properties": {
                                    "headers": {"type": "array", "items": {"type": "string"}},
                                    "rows": {
                                        "type": "array",
                                        "items": {"type": "array", "items": {"type": "string"}},
                                    },
                                },
                                "description": "Optional data table for this section.",
                            },
                        },
                    },
                },
                "raw_content": {
                    "type": "string",
                    "description": "Optional raw content string for direct markdown or plain text generation.",
                },
                "user_id": {
                    "type": "integer",
                    "description": "User ID to organize storage by user sandbox.",
                },
            },
            "required": ["format", "title"],
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.WRITE

    def _get_target_dir(self, user_id: int | None) -> Path:
        """Resolve sandboxed user output directory."""
        if user_id:
            target = self.output_dir / str(user_id)
        else:
            target = self.output_dir / "general"
        target.mkdir(parents=True, exist_ok=True)
        return target

    def _generate_markdown(self, title: str, sections: list[dict[str, Any]], raw_content: str | None) -> str:
        """Construct clean Markdown text."""
        if raw_content:
            return f"# {title}\n\n{raw_content.strip()}\n"

        lines = [f"# {title}", ""]
        for sec in sections:
            heading = sec.get("heading")
            if heading:
                lines.extend([f"## {heading}", ""])
            content = sec.get("content")
            if content:
                lines.extend([content.strip(), ""])
            bullets = sec.get("bullet_points") or []
            for b in bullets:
                lines.append(f"- {b}")
            if bullets:
                lines.append("")
            tbl = sec.get("table")
            if tbl and tbl.get("headers") and tbl.get("rows"):
                headers = [str(h) for h in tbl["headers"]]
                lines.append("| " + " | ".join(headers) + " |")
                lines.append("| " + " | ".join(["---"] * len(headers)) + " |")
                for r in tbl["rows"]:
                    row_cells = [str(c) for c in r]
                    lines.append("| " + " | ".join(row_cells) + " |")
                lines.append("")
        return "\n".join(lines)

    def _generate_text(self, title: str, sections: list[dict[str, Any]], raw_content: str | None) -> str:
        """Construct plain text document."""
        if raw_content:
            return f"{title.upper()}\n{'=' * len(title)}\n\n{raw_content.strip()}\n"

        lines = [title.upper(), "=" * len(title), ""]
        for sec in sections:
            heading = sec.get("heading")
            if heading:
                lines.extend([heading, "-" * len(heading), ""])
            content = sec.get("content")
            if content:
                lines.extend([content.strip(), ""])
            bullets = sec.get("bullet_points") or []
            for b in bullets:
                lines.append(f"  • {b}")
            if bullets:
                lines.append("")
            tbl = sec.get("table")
            if tbl and tbl.get("headers") and tbl.get("rows"):
                headers = [str(h) for h in tbl["headers"]]
                lines.append("\t".join(headers))
                lines.append("-" * 40)
                for r in tbl["rows"]:
                    lines.append("\t".join(str(c) for c in r))
                lines.append("")
        return "\n".join(lines)

    def _generate_docx(self, target_path: Path, title: str, sections: list[dict[str, Any]], raw_content: str | None) -> None:
        """Construct Word document using python-docx."""
        import docx
        from docx.shared import Inches, Pt, RGBColor

        doc = docx.Document()

        # Document Title
        title_p = doc.add_heading(title, level=0)
        title_p.runs[0].font.size = Pt(24)
        title_p.runs[0].font.color.rgb = RGBColor(0x1A, 0x36, 0x5D)

        if raw_content:
            doc.add_paragraph(raw_content.strip())
            doc.save(str(target_path))
            return

        for sec in sections:
            heading = sec.get("heading")
            if heading:
                h_p = doc.add_heading(heading, level=1)
                h_p.runs[0].font.size = Pt(16)
                h_p.runs[0].font.color.rgb = RGBColor(0x2B, 0x6C, 0xB0)

            content = sec.get("content")
            if content:
                doc.add_paragraph(content.strip())

            bullets = sec.get("bullet_points") or []
            for b in bullets:
                doc.add_paragraph(b, style="List Bullet")

            tbl = sec.get("table")
            if tbl and tbl.get("headers") and tbl.get("rows"):
                headers = [str(h) for h in tbl["headers"]]
                rows_data = tbl["rows"]
                table = doc.add_table(rows=1 + len(rows_data), cols=len(headers))
                table.style = "Table Grid"

                # Header row
                hdr_cells = table.rows[0].cells
                for i, h in enumerate(headers):
                    hdr_cells[i].text = h
                    hdr_cells[i].paragraphs[0].runs[0].font.bold = True

                # Data rows
                for r_idx, row in enumerate(rows_data):
                    row_cells = table.rows[r_idx + 1].cells
                    for c_idx, cell_value in enumerate(row):
                        if c_idx < len(row_cells):
                            row_cells[c_idx].text = str(cell_value)
                doc.add_paragraph()  # Spacing

        doc.save(str(target_path))

    def _generate_pdf(self, target_path: Path, title: str, sections: list[dict[str, Any]], raw_content: str | None) -> None:
        """Construct PDF document using reportlab."""
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

        doc = SimpleDocTemplate(
            str(target_path),
            pagesize=letter,
            rightMargin=54,
            leftMargin=54,
            topMargin=54,
            bottomMargin=54,
        )

        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            "DocTitle",
            parent=styles["Heading1"],
            fontSize=22,
            leading=26,
            textColor=colors.HexColor("#1A365D"),
            spaceAfter=15,
        )
        h1_style = ParagraphStyle(
            "SectionHeading",
            parent=styles["Heading2"],
            fontSize=15,
            leading=18,
            textColor=colors.HexColor("#2B6CB0"),
            spaceBefore=12,
            spaceAfter=6,
        )
        body_style = ParagraphStyle(
            "Body",
            parent=styles["Normal"],
            fontSize=10,
            leading=14,
            textColor=colors.HexColor("#2D3748"),
            spaceAfter=8,
        )
        bullet_style = ParagraphStyle(
            "Bullet",
            parent=body_style,
            leftIndent=15,
            spaceAfter=4,
        )

        story = [Paragraph(title, title_style), Spacer(1, 10)]

        if raw_content:
            for p_text in raw_content.split("\n\n"):
                if p_text.strip():
                    story.append(Paragraph(p_text.strip().replace("\n", "<br/>"), body_style))
            doc.build(story)
            return

        for sec in sections:
            heading = sec.get("heading")
            if heading:
                story.append(Paragraph(heading, h1_style))

            content = sec.get("content")
            if content:
                for p_text in content.split("\n\n"):
                    if p_text.strip():
                        story.append(Paragraph(p_text.strip().replace("\n", "<br/>"), body_style))

            bullets = sec.get("bullet_points") or []
            for b in bullets:
                story.append(Paragraph(f"&bull; {b}", bullet_style))
            if bullets:
                story.append(Spacer(1, 6))

            tbl = sec.get("table")
            if tbl and tbl.get("headers") and tbl.get("rows"):
                headers = [str(h) for h in tbl["headers"]]
                rows = tbl["rows"]
                table_data = [[Paragraph(f"<b>{h}</b>", body_style) for h in headers]]
                for r in rows:
                    table_data.append([Paragraph(str(c), body_style) for c in r])

                t = Table(table_data)
                t.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EDF2F7")),
                    ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 1), (-1, -1), 4),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E0")),
                ]))
                story.append(t)
                story.append(Spacer(1, 10))

        doc.build(story)

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        fmt = kwargs.get("format", "markdown").lower()
        title = kwargs.get("title")
        if not title:
            raise ValueError("The 'title' parameter is required.")

        raw_filename = kwargs.get("filename")
        sections = kwargs.get("sections") or []
        raw_content = kwargs.get("raw_content")
        user_id = kwargs.get("user_id")

        target_dir = self._get_target_dir(user_id)

        if fmt in ("markdown", "md"):
            ext = ".md"
            filename = _sanitize_filename(raw_filename, "document", ext)
            target_path = target_dir / filename
            text = self._generate_markdown(title, sections, raw_content)
            target_path.write_text(text, encoding="utf-8")
            mime_type = "text/markdown"

        elif fmt in ("text", "txt"):
            ext = ".txt"
            filename = _sanitize_filename(raw_filename, "document", ext)
            target_path = target_dir / filename
            text = self._generate_text(title, sections, raw_content)
            target_path.write_text(text, encoding="utf-8")
            mime_type = "text/plain"

        elif fmt == "docx":
            ext = ".docx"
            filename = _sanitize_filename(raw_filename, "report", ext)
            target_path = target_dir / filename
            self._generate_docx(target_path, title, sections, raw_content)
            mime_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

        elif fmt == "pdf":
            ext = ".pdf"
            filename = _sanitize_filename(raw_filename, "report", ext)
            target_path = target_dir / filename
            self._generate_pdf(target_path, title, sections, raw_content)
            mime_type = "application/pdf"

        else:
            raise ValueError(f"Unsupported format '{fmt}'. Supported formats: 'markdown', 'text', 'docx', 'pdf'.")

        if not target_path.exists() or target_path.stat().st_size == 0:
            raise RuntimeError(f"Document generation failed: File '{filename}' was not created.")

        size = target_path.stat().st_size
        rel_path = str(target_path.relative_to(self.output_dir.parent))

        return {
            "action": "generate_document",
            "format": fmt,
            "filename": filename,
            "relative_path": rel_path,
            "size_bytes": size,
            "mime_type": mime_type,
            "title": title,
            "sections_count": len(sections),
            "status": "Document successfully generated and ready for delivery.",
        }

