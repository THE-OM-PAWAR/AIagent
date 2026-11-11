"""Comprehensive test suite for Phase 2 Essential Built-in Tools.

Covers:
- CalculatorTool (AST math, percentages, P&L, margin/markup, interest, EMI, ratio, avg)
- DateTimeTool (now, timezone conversion, diff, duration, business days, deadline, format)
- TextProcessorTool (metrics, pattern match, structured extraction, text compare, semantic fallback)
- FileReaderTool (TXT, MD, JSON, CSV, XLSX, DOCX, PDF, path traversal security, metadata, tables)
- WebReaderTool (SSRF blocking, private IP rejection, scheme validation, HTML parsing)
- MemoryOperationsTool (search, save, update, archive via MemoryManager)
- ToolExecutor execution integration and error wrapping
- ToolRegistry built-in registration and Gemini declarations
"""

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from src.tools.builtin.calculator import CalculatorTool
from src.tools.builtin.datetime_tool import DateTimeTool
from src.tools.builtin.file_reader import FileReaderTool
from src.tools.builtin.memory_tools import MemoryOperationsTool
from src.tools.builtin.text_processor import TextProcessorTool
from src.tools.builtin.web_reader import WebReaderTool
from src.tools.executor import ToolExecutor
from src.tools.permissions import ToolPermission
from src.tools.registry import ToolRegistry


class TestCalculatorTool(unittest.IsolatedAsyncioTestCase):
    """Test suite for CalculatorTool."""

    async def asyncSetUp(self) -> None:
        self.tool = CalculatorTool()

    async def test_01_basic_arithmetic(self) -> None:
        """Verify safe deterministic AST evaluation."""
        result = await self.tool.execute(action="evaluate", expression="2 + 3 * 4")
        self.assertEqual(result["result"], 14)

        # Parentheses & exponentiation
        result2 = await self.tool.execute(action="evaluate", expression="(10 - 2) ** 2 / 4")
        self.assertEqual(result2["result"], 16.0)

    async def test_02_division_by_zero(self) -> None:
        """Verify division by zero raises ZeroDivisionError."""
        with self.assertRaises(ZeroDivisionError):
            await self.tool.execute(action="evaluate", expression="100 / 0")

    async def test_03_disallowed_syntax_and_eval_injection(self) -> None:
        """Verify arbitrary code execution is strictly rejected by AST validator."""
        disallowed = [
            "__import__('os').system('ls')",
            "open('/etc/passwd').read()",
            "eval('2 + 2')",
            "lambda x: x",
        ]
        for expr in disallowed:
            with self.assertRaises(ValueError):
                await self.tool.execute(action="evaluate", expression=expr)

    async def test_04_percentages(self) -> None:
        """Verify percentage operations."""
        # 15% of 200 = 30
        res1 = await self.tool.execute(action="percentage", value=200, percentage=15)
        self.assertEqual(res1["percentage_amount"], 30.0)
        self.assertEqual(res1["value_with_percentage_added"], 230.0)

        # Percentage change from 100 to 150 = +50%
        res2 = await self.tool.execute(action="percentage", value=150, comparison_value=100)
        self.assertEqual(res2["percentage_change"], 50.0)
        self.assertTrue(res2["is_increase"])

    async def test_05_profit_and_loss(self) -> None:
        """Verify profit and loss calculation."""
        # Profit case
        res_profit = await self.tool.execute(action="profit_loss", cost_price=100, selling_price=125)
        self.assertEqual(res_profit["status"], "Profit")
        self.assertEqual(res_profit["amount"], 25.0)
        self.assertEqual(res_profit["percentage"], 25.0)

        # Loss case
        res_loss = await self.tool.execute(action="profit_loss", cost_price=200, selling_price=150)
        self.assertEqual(res_loss["status"], "Loss")
        self.assertEqual(res_loss["amount"], 50.0)
        self.assertEqual(res_loss["percentage"], 25.0)

    async def test_06_margin_and_markup(self) -> None:
        """Verify gross margin and markup percentages."""
        # Cost 80, Selling 100 -> Profit 20, Margin 20%, Markup 25%
        res = await self.tool.execute(action="margin_markup", cost_price=80, selling_price=100)
        self.assertEqual(res["profit_margin_percentage"], 20.0)
        self.assertEqual(res["markup_percentage"], 25.0)

    async def test_07_interest_calculations(self) -> None:
        """Verify simple and compound interest."""
        # Simple interest: P=1000, R=5%, T=2 -> I = 100, Total = 1100
        res_int = await self.tool.execute(action="interest", principal=1000, annual_rate=5, time_years=2)
        self.assertEqual(res_int["simple_interest"]["interest_amount"], 100.0)
        self.assertEqual(res_int["simple_interest"]["total_amount"], 1100.0)
        self.assertGreater(res_int["compound_interest"]["total_amount"], 1000)

    async def test_08_loan_emi(self) -> None:
        """Verify standard amortized loan EMI calculation."""
        res_emi = await self.tool.execute(action="loan_emi", principal=100000, annual_rate=10, tenure_months=12)
        self.assertGreater(res_emi["monthly_emi"], 8700)
        self.assertLess(res_emi["monthly_emi"], 8900)
        self.assertGreater(res_emi["total_payment_payable"], 100000)

    async def test_09_average_and_ratio(self) -> None:
        """Verify average and ratio operations."""
        avg_res = await self.tool.execute(action="average", numbers=[10, 20, 30, 40])
        self.assertEqual(avg_res["mean"], 25.0)

        ratio_res = await self.tool.execute(action="ratio", numbers=[12, 16])
        self.assertEqual(ratio_res["simplified_ratio"], "3:4")


class TestDateTimeTool(unittest.IsolatedAsyncioTestCase):
    """Test suite for DateTimeTool."""

    async def asyncSetUp(self) -> None:
        self.tool = DateTimeTool()

    async def test_01_now_utc_and_timezone(self) -> None:
        """Verify current datetime retrieval in UTC and specified timezone."""
        res_utc = await self.tool.execute(action="now")
        self.assertEqual(res_utc["timezone"], "UTC")
        self.assertIn("iso", res_utc)

        res_ny = await self.tool.execute(action="now", timezone="America/New_York")
        self.assertEqual(res_ny["timezone"], "America/New_York")

    async def test_02_convert_timezone(self) -> None:
        """Verify timezone conversion and invalid timezone handling."""
        res = await self.tool.execute(
            action="convert_timezone",
            date_string="2026-06-01T12:00:00Z",
            source_timezone="UTC",
            timezone="Asia/Kolkata",
        )
        self.assertIn("17:30", res["converted_iso"])

        # Invalid timezone
        with self.assertRaises(ValueError):
            await self.tool.execute(action="convert_timezone", date_string="2026-06-01T12:00:00Z", timezone="Invalid/TZ")

    async def test_03_date_difference(self) -> None:
        """Verify difference between two dates."""
        res = await self.tool.execute(
            action="date_difference",
            date_string="2026-01-01T00:00:00Z",
            target_date_string="2026-01-11T00:00:00Z",
        )
        self.assertEqual(res["total_days"], 10.0)
        self.assertEqual(res["breakdown"]["days"], 10)

    async def test_04_add_duration(self) -> None:
        """Verify adding/subtracting durations."""
        res = await self.tool.execute(
            action="add_duration",
            date_string="2026-01-01T00:00:00Z",
            days=5,
            hours=3,
        )
        self.assertIn("2026-01-06T03:00:00", res["result_date"])

    async def test_05_business_days(self) -> None:
        """Verify business day calculations skipping Saturday and Sunday."""
        # Friday 2026-01-02 + 1 business day -> Monday 2026-01-05
        res = await self.tool.execute(
            action="add_duration",
            date_string="2026-01-02T10:00:00Z",
            business_days_count=1,
        )
        self.assertIn("2026-01-05", res["result_date"])

    async def test_06_calculate_deadline_and_format(self) -> None:
        """Verify deadline calculation and custom date formatting."""
        res_dl = await self.tool.execute(
            action="calculate_deadline",
            date_string="2030-01-01T00:00:00Z",
        )
        self.assertFalse(res_dl["is_passed"])
        self.assertGreater(res_dl["calendar_days_remaining"], 300)

        res_fmt = await self.tool.execute(
            action="format_date",
            date_string="2026-01-01T15:30:00Z",
            format_pattern="date_only",
        )
        self.assertEqual(res_fmt["formatted"], "2026-01-01")


class TestTextProcessorTool(unittest.IsolatedAsyncioTestCase):
    """Test suite for TextProcessorTool."""

    async def asyncSetUp(self) -> None:
        self.tool = TextProcessorTool()

    async def test_01_count_metrics(self) -> None:
        """Verify word, character, and sentence counting."""
        sample = "The quick brown fox jumps over the lazy dog. It was a sunny afternoon!"
        res = await self.tool.execute(action="count_metrics", text=sample)
        self.assertEqual(res["words_count"], 14)
        self.assertEqual(res["sentences_count"], 2)
        self.assertGreater(res["estimated_reading_time_minutes"], 0)

    async def test_02_pattern_matching(self) -> None:
        """Verify regex pattern matching."""
        sample = "Error codes: ERR-101, ERR-202, and ERR-303."
        res = await self.tool.execute(action="pattern_match", text=sample, pattern=r"ERR-\d+")
        self.assertEqual(res["total_matches"], 3)
        matched_texts = [m["match"] for m in res["matches"]]
        self.assertEqual(matched_texts, ["ERR-101", "ERR-202", "ERR-303"])

    async def test_03_structured_extraction(self) -> None:
        """Verify extraction of emails, urls, phones, and markdown links/headings."""
        sample = (
            "# Main Title\n\n"
            "Contact us at support@example.com or sales@example.org.\n"
            "Visit https://example.com/docs or call +1-555-123-4567.\n"
            "Check [Google](https://google.com) for details."
        )
        res = await self.tool.execute(action="extract_structured", text=sample, extraction_type="all")
        self.assertIn("support@example.com", res["extracted"]["emails"])
        self.assertIn("https://example.com/docs", res["extracted"]["urls"])
        self.assertEqual(res["extracted"]["headings"][0]["title"], "Main Title")
        self.assertEqual(len(res["extracted"]["markdown_links"]), 1)
        self.assertEqual(res["extracted"]["markdown_links"][0]["label"], "Google")

    async def test_04_compare_text(self) -> None:
        """Verify unified diff calculation."""
        text_a = "Line 1\nLine 2\nLine 3"
        text_b = "Line 1\nLine 2 modified\nLine 3"
        res = await self.tool.execute(action="compare", text=text_a, comparison_text=text_b)
        self.assertFalse(res["identical"])
        self.assertIn("-Line 2", res["diff_preview"])
        self.assertIn("+Line 2 modified", res["diff_preview"])

    async def test_05_extractive_fallback_for_summarize_and_classify(self) -> None:
        """Verify extractive heuristics work when no LLMManager is attached."""
        sample = "First sentence here. Second sentence with important data. Third concluding sentence."
        res_sum = await self.tool.execute(action="summarize", text=sample)
        self.assertIn("First sentence here", res_sum["result"])

        res_cls = await self.tool.execute(action="classify", text="This is a query about price", categories=["Billing", "Technical"])
        self.assertEqual(res_cls["action"], "classify")
        self.assertIn("result", res_cls)


class TestFileReaderTool(unittest.IsolatedAsyncioTestCase):
    """Test suite for FileReaderTool with sandboxed file types and security checks."""

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage_root = Path(self.temp_dir.name)
        self.tool = FileReaderTool(storage_root=str(self.storage_root))

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_01_read_txt_and_markdown(self) -> None:
        """Verify reading plain text and markdown documents."""
        # Create test txt file
        txt_path = self.storage_root / "sample.txt"
        txt_path.write_text("Hello, AI Assistant!\nSecond line of content.", encoding="utf-8")

        res = await self.tool.execute(action="read_text", file_path="sample.txt")
        self.assertIn("Hello, AI Assistant!", res["content"])
        self.assertEqual(res["lines_read"], 2)

        # Create test markdown file
        md_path = self.storage_root / "notes.md"
        md_path.write_text("# Title\n- Item 1\n- Item 2", encoding="utf-8")

        res_md = await self.tool.execute(action="read_text", file_path="notes.md")
        self.assertIn("# Title", res_md["content"])

    async def test_02_read_json(self) -> None:
        """Verify reading and formatting JSON files."""
        json_path = self.storage_root / "data.json"
        data = {"agent": "Gemini", "version": 2.0, "tools": ["calculator", "file_reader"]}
        json_path.write_text(json.dumps(data), encoding="utf-8")

        res = await self.tool.execute(action="read_text", file_path="data.json")
        self.assertTrue(res["is_dict"])
        self.assertIn("Gemini", res["content_preview"])

    async def test_03_read_csv_and_table(self) -> None:
        """Verify reading CSV as text and as structured table."""
        csv_path = self.storage_root / "users.csv"
        csv_path.write_text("id,name,role\n1,Alice,Engineer\n2,Bob,Architect\n", encoding="utf-8")

        res_txt = await self.tool.execute(action="read_text", file_path="users.csv")
        self.assertIn("Alice", str(res_txt["rows"]))

        res_tbl = await self.tool.execute(action="read_table", file_path="users.csv")
        self.assertEqual(res_tbl["fieldnames"], ["id", "name", "role"])
        self.assertEqual(len(res_tbl["data"]), 2)
        self.assertEqual(res_tbl["data"][0]["name"], "Alice")

    async def test_04_read_docx(self) -> None:
        """Verify reading DOCX documents."""
        try:
            import docx
            docx_path = self.storage_root / "test.docx"
            doc = docx.Document()
            doc.add_heading("Project Overview", level=1)
            doc.add_paragraph("This is a paragraph inside Word document.")
            table = doc.add_table(rows=2, cols=2)
            table.cell(0, 0).text = "Col1"
            table.cell(0, 1).text = "Col2"
            table.cell(1, 0).text = "Val1"
            table.cell(1, 1).text = "Val2"
            doc.save(str(docx_path))

            res = await self.tool.execute(action="read_text", file_path="test.docx")
            self.assertIn("Project Overview", res["content"])
            self.assertIn("This is a paragraph", res["content"])

            res_tbl = await self.tool.execute(action="read_table", file_path="test.docx")
            self.assertEqual(res_tbl["tables_found"], 1)
            self.assertEqual(len(res_tbl["tables"]), 1)
        except ImportError:
            self.skipTest("python-docx not installed")

    async def test_05_read_xlsx(self) -> None:
        """Verify reading Excel spreadsheets."""
        try:
            import openpyxl
            xlsx_path = self.storage_root / "sheet.xlsx"
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "Sales"
            ws.append(["Product", "Revenue", "Units"])
            ws.append(["Widget A", 1200, 30])
            ws.append(["Widget B", 2400, 60])
            wb.save(str(xlsx_path))

            res = await self.tool.execute(action="read_text", file_path="sheet.xlsx")
            self.assertGreaterEqual(res["rows_read"], 2)

            res_tbl = await self.tool.execute(action="read_table", file_path="sheet.xlsx")
            self.assertEqual(res_tbl["headers"], ["Product", "Revenue", "Units"])
            self.assertEqual(len(res_tbl["data"]), 2)
        except ImportError:
            self.skipTest("openpyxl not installed")

    async def test_06_read_pdf(self) -> None:
        """Verify reading PDF files."""
        try:
            import pypdf
            pdf_path = self.storage_root / "doc.pdf"
            writer = pypdf.PdfWriter()
            writer.add_blank_page(width=72, height=72)
            with open(pdf_path, "wb") as f:
                writer.write(f)

            res = await self.tool.execute(action="read_text", file_path="doc.pdf")
            self.assertEqual(res["total_pages"], 1)
        except ImportError:
            self.skipTest("pypdf not installed")

    async def test_07_security_path_traversal_blocked(self) -> None:
        """Verify path traversal attacks outside sandbox are strictly rejected."""
        traversal_paths = [
            "../../etc/passwd",
            "/etc/passwd",
            "../secret.txt",
            "subdir/../../../../../../etc/shadow",
        ]
        for bad_path in traversal_paths:
            with self.assertRaises(PermissionError):
                await self.tool.execute(action="read_text", file_path=bad_path)

    async def test_08_inspect_metadata_and_list_files(self) -> None:
        """Verify metadata inspection and file listing."""
        f = self.storage_root / "meta_test.txt"
        f.write_text("Sample metadata file", encoding="utf-8")

        res_meta = await self.tool.execute(action="inspect_metadata", file_path="meta_test.txt")
        self.assertEqual(res_meta["metadata"]["extension"], ".txt")
        self.assertGreater(res_meta["metadata"]["size_bytes"], 0)

        res_list = await self.tool.execute(action="list_files")
        filenames = [f["filename"] for f in res_list["files"]]
        self.assertIn("meta_test.txt", filenames)


class TestWebReaderTool(unittest.IsolatedAsyncioTestCase):
    """Test suite for WebReaderTool with SSRF protections."""

    async def asyncSetUp(self) -> None:
        self.tool = WebReaderTool()

    async def test_01_ssrf_blocked_ips_and_schemes(self) -> None:
        """Verify SSRF validation blocks private networks, localhost, link-local, and bad schemes."""
        blocked_urls = [
            "http://127.0.0.1",
            "http://localhost/admin",
            "http://127.0.0.1:8080/metrics",
            "http://10.0.0.1/internal",
            "http://192.168.1.1/router",
            "http://172.16.0.5/api",
            "http://169.254.169.254/latest/meta-data/",
            "http://0.0.0.0/",
            "file:///etc/passwd",
            "ftp://files.example.com",
            "gopher://example.com",
        ]
        for url in blocked_urls:
            with self.assertRaises(ValueError):
                await self.tool.execute(url=url)

    async def test_02_html_extraction_with_mock_client(self) -> None:
        """Verify HTML title, headings, links, and content extraction."""
        sample_html = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>Sample Tech Article</title>
            <meta name="description" content="A guide to modern backend engineering">
        </head>
        <body>
            <h1>Engineering Excellence</h1>
            <h2>Core Principles</h2>
            <p>Antigravity agents execute tools safely and asynchronously.</p>
            <a href="https://example.com/learn-more">Learn More</a>
            <script>alert('malicious')</script>
        </body>
        </html>
        """

        mock_resp = httpx.Response(
            status_code=200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=sample_html,
            request=httpx.Request("GET", "https://example.com/article"),
        )

        with patch("src.tools.builtin.web_reader.socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]):
            with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
                mock_get.return_value = mock_resp
                res = await self.tool.execute(url="https://example.com/article")
                self.assertEqual(res["title"], "Sample Tech Article")
                self.assertIn("Engineering Excellence", res["content"])
                self.assertIn("Antigravity agents execute tools safely", res["content"])
                self.assertNotIn("alert('malicious')", res["content"])

                # Test headings extraction mode
                res_h = await self.tool.execute(url="https://example.com/article", extract_mode="headings")
                self.assertEqual(res_h["headings"][0]["text"], "Engineering Excellence")

                # Test links extraction mode
                res_l = await self.tool.execute(url="https://example.com/article", extract_mode="links")
                self.assertEqual(res_l["links"][0]["text"], "Learn More")


class TestMemoryOperationsTool(unittest.IsolatedAsyncioTestCase):
    """Test suite for MemoryOperationsTool connected to MemoryManager."""

    async def asyncSetUp(self) -> None:
        self.mock_memory_manager = MagicMock()
        self.tool = MemoryOperationsTool(memory_manager=self.mock_memory_manager)

    async def test_01_search_memory(self) -> None:
        """Verify search_memory queries MemoryManager."""
        mock_item = MagicMock()
        mock_item.memory_id = "mem-1"
        mock_item.content = "User prefers dark mode"
        mock_item.category = MagicMock(value="semantic")
        mock_item.importance = 0.9
        mock_item.fused_score = 0.95
        mock_item.created_at = datetime.now(timezone.utc)

        self.mock_memory_manager.retrieve = AsyncMock(return_value=[mock_item])

        res = await self.tool.execute(action="search_memory", user_id=101, query="preferences")
        self.assertEqual(res["results_count"], 1)
        self.assertEqual(res["memories"][0]["content"], "User prefers dark mode")

    async def test_02_save_memory(self) -> None:
        """Verify save_memory calls save_memory with conflict check."""
        mock_saved = MagicMock()
        mock_saved.id = "mem-2"
        mock_saved.content = "User lives in Seattle"
        mock_saved.category = MagicMock(value="semantic")

        self.mock_memory_manager.remember = AsyncMock(return_value=mock_saved)

        res = await self.tool.execute(action="save_memory", user_id=101, content="User lives in Seattle")
        self.assertEqual(res["memory_id"], "mem-2")

    async def test_03_update_and_archive(self) -> None:
        """Verify update and archive memory operations."""
        mock_updated = MagicMock()
        mock_updated.id = "mem-2"
        mock_updated.content = "User lives in Portland"

        self.mock_memory_manager.update_memory = AsyncMock(return_value=mock_updated)
        self.mock_memory_manager.archive_memory = AsyncMock(return_value=True)

        res_update = await self.tool.execute(action="update_memory", user_id=101, memory_id="mem-2", content="User lives in Portland")
        self.assertEqual(res_update["memory_id"], "mem-2")

        res_arch = await self.tool.execute(action="archive_memory", user_id=101, memory_id="mem-2")
        self.assertTrue(res_arch["success"])


class TestToolRegistryBuiltinRegistration(unittest.IsolatedAsyncioTestCase):
    """Test suite verifying ToolRegistry integration with all built-in tools."""

    def test_01_register_builtin_tools(self) -> None:
        """Verify register_builtin_tools installs all tools and Gemini declarations work."""
        mock_mm = MagicMock()
        mock_llm = MagicMock()
        registry = ToolRegistry()

        registry.register_builtin_tools(memory_manager=mock_mm, llm_manager=mock_llm)

        tools = registry.list_tools()
        names = {t["name"] for t in tools}

        expected = {
            "calculator",
            "datetime_calculator",
            "text_processor",
            "file_reader",
            "web_page_reader",
            "memory_operations",
        }
        self.assertTrue(expected.issubset(names))

        # Check Gemini declarations can be generated
        declarations = registry.get_gemini_declarations()
        self.assertGreaterEqual(len(declarations), 6)

    async def test_02_executor_runs_builtin_tools_safely(self) -> None:
        """Verify ToolExecutor wraps success and exceptions correctly for built-in tools."""
        registry = ToolRegistry()
        registry.register_builtin_tools()
        executor = ToolExecutor(registry)

        # 1. Successful execution
        res = await executor.execute_tool("calculator", {"action": "evaluate", "expression": "15 * 4"}, user_id=101)
        self.assertTrue(res.success)
        self.assertEqual(res.output["result"], 60)

        # 2. Division by zero wrapped into safe failure result without crashing
        res_err = await executor.execute_tool("calculator", {"action": "evaluate", "expression": "10 / 0"}, user_id=101)
        self.assertFalse(res_err.success)
        self.assertIn("Division by zero", res_err.error)


if __name__ == "__main__":
    unittest.main()
