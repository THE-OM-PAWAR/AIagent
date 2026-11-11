"""Comprehensive test suite for Phase 3 Advanced Built-in Tools and Workflow Control.

Covers:
- ConversionTool (length, weight, temperature, volume, area, speed, time, digital storage, error validation)
- DataProcessorTool (parse/validate, filter, select_columns, transform, sort, extract_nested, convert, summarize)
- DocumentGeneratorTool (Markdown, TXT, DOCX, PDF reports, sandboxed file generation)
- SpreadsheetAnalyzerTool (CSV & XLSX inspect, statistics, nulls, duplicates, group aggregates, compare, report)
- BrowserAutomationTool (SSRF security rejection, challenge detection, approval requirements)
- WorkflowEngine & WorkflowControlTool (PostgreSQL/SQLite state, step checkpoints, pause, resume, cancel, restart recovery)
- Full ToolRegistry integration covering all 11 tools
"""

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.workflow_engine import WorkflowEngine
from src.database.models import Base
from src.database.models.workflow import WorkflowModel
from src.database.repositories.workflow_repo import WorkflowRepository
from src.tools.builtin.browser_automation import BrowserAutomationTool
from src.tools.builtin.conversion import ConversionTool
from src.tools.builtin.data_processor import DataProcessorTool
from src.tools.builtin.document_generator import DocumentGeneratorTool
from src.tools.builtin.spreadsheet_analyzer import SpreadsheetAnalyzerTool
from src.tools.builtin.workflow_tools import WorkflowControlTool
from src.tools.executor import ToolExecutor
from src.tools.permissions import ToolPermission
from src.tools.registry import ToolRegistry


class TestConversionTool(unittest.IsolatedAsyncioTestCase):
    """Test suite for ConversionTool."""

    async def asyncSetUp(self) -> None:
        self.tool = ConversionTool()

    async def test_01_length_conversions(self) -> None:
        """Verify length conversions."""
        # 10 km to miles
        res = await self.tool.execute(value=10, from_unit="km", to_unit="miles")
        self.assertAlmostEqual(res["converted_value"], 6.2137, places=3)
        self.assertEqual(res["category"], "length")

        # 100 meters to feet
        res2 = await self.tool.execute(value=100, from_unit="meters", to_unit="feet")
        self.assertAlmostEqual(res2["converted_value"], 328.084, places=2)

    async def test_02_weight_conversions(self) -> None:
        """Verify weight/mass conversions."""
        # 100 kg to lbs
        res = await self.tool.execute(value=100, from_unit="kg", to_unit="lbs")
        self.assertAlmostEqual(res["converted_value"], 220.4623, places=3)

        # 16 oz to pounds
        res2 = await self.tool.execute(value=16, from_unit="oz", to_unit="pounds")
        self.assertEqual(res2["converted_value"], 1.0)

    async def test_03_temperature_conversions(self) -> None:
        """Verify temperature conversions including offset math."""
        # 0 C to Fahrenheit = 32 F
        res_f = await self.tool.execute(value=0, from_unit="celsius", to_unit="fahrenheit")
        self.assertEqual(res_f["converted_value"], 32.0)

        # 212 F to Celsius = 100 C
        res_c = await self.tool.execute(value=212, from_unit="fahrenheit", to_unit="celsius")
        self.assertEqual(res_c["converted_value"], 100.0)

        # 0 C to Kelvin = 273.15 K
        res_k = await self.tool.execute(value=0, from_unit="celsius", to_unit="kelvin")
        self.assertEqual(res_k["converted_value"], 273.15)

    async def test_04_volume_and_area(self) -> None:
        """Verify volume and area conversions."""
        # 1 US gallon to liters
        res_vol = await self.tool.execute(value=1, from_unit="gal", to_unit="liters")
        self.assertAlmostEqual(res_vol["converted_value"], 3.7854, places=3)

        # 1 hectare to acres
        res_area = await self.tool.execute(value=1, from_unit="hectare", to_unit="acres")
        self.assertAlmostEqual(res_area["converted_value"], 2.4711, places=3)

    async def test_05_speed_time_and_digital_storage(self) -> None:
        """Verify speed, time, and digital storage conversions."""
        # 100 km/h to mph
        res_spd = await self.tool.execute(value=100, from_unit="km/h", to_unit="mph")
        self.assertAlmostEqual(res_spd["converted_value"], 62.1371, places=3)

        # 2 hours to minutes
        res_time = await self.tool.execute(value=2, from_unit="hours", to_unit="minutes")
        self.assertEqual(res_time["converted_value"], 120.0)

        # 1024 Megabytes to Gigabytes
        res_dig = await self.tool.execute(value=1000, from_unit="MB", to_unit="GB")
        self.assertEqual(res_dig["converted_value"], 1.0)

        # 1 MiB to bytes (binary)
        res_bin = await self.tool.execute(value=1, from_unit="MiB", to_unit="bytes")
        self.assertEqual(res_bin["converted_value"], 1048576.0)

    async def test_06_incompatible_and_invalid_units(self) -> None:
        """Verify cross-category conversion and invalid units raise ValueError."""
        # Incompatible categories: kg to meters
        with self.assertRaises(ValueError):
            await self.tool.execute(value=10, from_unit="kg", to_unit="meters")

        # Unknown source unit
        with self.assertRaises(ValueError):
            await self.tool.execute(value=10, from_unit="lightyears", to_unit="meters")

        # Unknown target unit
        with self.assertRaises(ValueError):
            await self.tool.execute(value=10, from_unit="meters", to_unit="furlongs")


class TestDataProcessorTool(unittest.IsolatedAsyncioTestCase):
    """Test suite for DataProcessorTool."""

    async def asyncSetUp(self) -> None:
        self.tool = DataProcessorTool()

    async def test_01_parse_validate(self) -> None:
        """Verify parsing and validation of JSON and CSV."""
        json_sample = json.dumps([{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}])
        res = await self.tool.execute(action="parse_validate", data_string=json_sample)
        self.assertTrue(res["valid"])
        self.assertEqual(res["detected_format"], "json")
        self.assertEqual(res["element_count"], 2)

        csv_sample = "id,name,role\n1,Alice,Dev\n2,Bob,Ops\n"
        res_csv = await self.tool.execute(action="parse_validate", data_string=csv_sample)
        self.assertTrue(res_csv["valid"])
        self.assertEqual(res_csv["detected_format"], "csv")

    async def test_02_filter_and_select_columns(self) -> None:
        """Verify filtering records by conditions and column projection."""
        records = [
            {"id": 1, "name": "Alice", "age": 25, "active": "true"},
            {"id": 2, "name": "Bob", "age": 17, "active": "false"},
            {"id": 3, "name": "Charlie", "age": 30, "active": "true"},
        ]
        json_str = json.dumps(records)

        # Filter age >= 21
        res_filter = await self.tool.execute(
            action="filter",
            data_string=json_str,
            filter_conditions=[{"field": "age", "operator": ">=", "value": 21}],
        )
        self.assertEqual(res_filter["matched_count"], 2)
        matched_names = [r["name"] for r in res_filter["records"]]
        self.assertEqual(matched_names, ["Alice", "Charlie"])

        # Select columns
        res_cols = await self.tool.execute(
            action="select_columns",
            data_string=json_str,
            columns=["id", "name"],
        )
        self.assertEqual(list(res_cols["records"][0].keys()), ["id", "name"])

    async def test_03_sort_and_transform(self) -> None:
        """Verify sorting and column renaming."""
        records = [
            {"id": 2, "name": "Bob", "score": 85},
            {"id": 1, "name": "Alice", "score": 95},
            {"id": 3, "name": "Charlie", "score": 70},
        ]
        json_str = json.dumps(records)

        # Sort descending by score
        res_sort = await self.tool.execute(
            action="sort",
            data_string=json_str,
            sort_key="score",
            sort_descending=True,
        )
        self.assertEqual(res_sort["records"][0]["name"], "Alice")
        self.assertEqual(res_sort["records"][-1]["name"], "Charlie")

        # Transform / rename columns
        res_trans = await self.tool.execute(
            action="transform",
            data_string=json_str,
            rename_map={"name": "full_name", "score": "grade"},
        )
        self.assertIn("full_name", res_trans["records"][0])
        self.assertIn("grade", res_trans["records"][0])

    async def test_04_extract_nested(self) -> None:
        """Verify nested JSON path extraction."""
        nested = {
            "company": "Antigravity",
            "departments": [
                {"name": "Engineering", "lead": {"name": "Grace", "city": "London"}},
                {"name": "Research", "lead": {"name": "Alan", "city": "Cambridge"}},
            ],
        }
        res = await self.tool.execute(
            action="extract_nested",
            data_string=json.dumps(nested),
            json_path="departments[0].lead.name",
        )
        self.assertTrue(res["found"])
        self.assertEqual(res["value"], "Grace")

    async def test_05_convert_and_summarize(self) -> None:
        """Verify CSV to JSON conversion and summary statistics."""
        csv_str = "id,city,temp\n1,Tokyo,22.5\n2,Paris,\n3,New York,18.0\n"
        res_conv = await self.tool.execute(action="convert", data_string=csv_str, convert_target="json")
        self.assertEqual(res_conv["to_format"], "json")
        parsed_out = json.loads(res_conv["output"])
        self.assertEqual(len(parsed_out), 3)

        res_sum = await self.tool.execute(action="summarize", data_string=csv_str)
        self.assertEqual(res_sum["total_records"], 3)
        self.assertEqual(res_sum["column_count"], 3)
        temp_col = next(c for c in res_sum["columns"] if c["column"] == "temp")
        self.assertEqual(temp_col["null_count"], 1)


class TestDocumentGeneratorTool(unittest.IsolatedAsyncioTestCase):
    """Test suite for DocumentGeneratorTool."""

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tool = DocumentGeneratorTool(output_dir=self.temp_dir.name)

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_01_generate_markdown(self) -> None:
        """Verify generating structured Markdown documents."""
        sections = [
            {"heading": "Executive Summary", "content": "This is a summary of Q3 results.", "bullet_points": ["Growth 25%", "Zero incidents"]},
            {"heading": "Metrics", "table": {"headers": ["Dept", "Budget"], "rows": [["Eng", "$500k"], ["Sales", "$300k"]]}},
        ]
        res = await self.tool.execute(
            format="markdown",
            title="Q3 Performance Report",
            sections=sections,
            filename="q3_report.md",
        )
        self.assertEqual(res["format"], "markdown")
        self.assertEqual(res["filename"], "q3_report.md")

        file_on_disk = Path(self.temp_dir.name) / "general" / "q3_report.md"
        self.assertTrue(file_on_disk.exists())
        content = file_on_disk.read_text(encoding="utf-8")
        self.assertIn("# Q3 Performance Report", content)
        self.assertIn("## Executive Summary", content)
        self.assertIn("| Dept | Budget |", content)

    async def test_02_generate_text(self) -> None:
        """Verify generating plain text documents."""
        res = await self.tool.execute(
            format="text",
            title="Deployment Checklist",
            raw_content="1. Verify database\n2. Run migrations\n3. Start bot",
            filename="checklist.txt",
        )
        self.assertEqual(res["format"], "text")
        file_on_disk = Path(self.temp_dir.name) / "general" / "checklist.txt"
        self.assertTrue(file_on_disk.exists())
        self.assertIn("DEPLOYMENT CHECKLIST", file_on_disk.read_text(encoding="utf-8"))

    async def test_03_generate_docx(self) -> None:
        """Verify generating Microsoft Word (.docx) documents."""
        sections = [
            {"heading": "Project Overview", "content": "The architecture is modular and async.", "bullet_points": ["High resilience", "Full RAG memory"]},
            {"heading": "Milestones", "table": {"headers": ["Phase", "Status"], "rows": [["Phase 1", "Done"], ["Phase 2", "Done"], ["Phase 3", "Active"]]}},
        ]
        res = await self.tool.execute(
            format="docx",
            title="Technical Specification",
            sections=sections,
            filename="spec.docx",
        )
        self.assertEqual(res["format"], "docx")
        file_on_disk = Path(self.temp_dir.name) / "general" / "spec.docx"
        self.assertTrue(file_on_disk.exists())
        self.assertGreater(file_on_disk.stat().st_size, 1000)

    async def test_04_generate_pdf(self) -> None:
        """Verify generating PDF documents with reportlab."""
        sections = [
            {"heading": "Financial Audit", "content": "Summary of accounts for the fiscal year."},
            {"heading": "Ledger", "table": {"headers": ["Item", "Cost"], "rows": [["Compute", "$1200"], ["Storage", "$300"]]}},
        ]
        res = await self.tool.execute(
            format="pdf",
            title="Audit Report",
            sections=sections,
            filename="audit.pdf",
        )
        self.assertEqual(res["format"], "pdf")
        file_on_disk = Path(self.temp_dir.name) / "general" / "audit.pdf"
        self.assertTrue(file_on_disk.exists())
        # Check PDF header
        with open(file_on_disk, "rb") as f:
            header = f.read(5)
            self.assertEqual(header, b"%PDF-")


class TestSpreadsheetAnalyzerTool(unittest.IsolatedAsyncioTestCase):
    """Test suite for SpreadsheetAnalyzerTool."""

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage_root = Path(self.temp_dir.name)
        self.tool = SpreadsheetAnalyzerTool(storage_root=self.storage_root)

        # Create test CSV
        csv_file = self.storage_root / "sales.csv"
        csv_file.write_text(
            "Region,Category,Revenue,Units\n"
            "North,Electronics,1200,10\n"
            "North,Furniture,800,4\n"
            "South,Electronics,1500,12\n"
            "South,Furniture,600,3\n"
            "North,Electronics,1200,10\n"  # Duplicate row
            "East,Electronics,,5\n",        # Missing Revenue
            encoding="utf-8",
        )

        # Create test XLSX
        import openpyxl
        xlsx_file = self.storage_root / "inventory.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Products"
        ws.append(["SKU", "Item", "Quantity", "Price"])
        ws.append(["A1", "Widget", 50, 19.99])
        ws.append(["A2", "Gadget", 30, 29.99])
        ws.append(["A3", "Doohickey", 100, 4.99])
        wb.save(str(xlsx_file))

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_01_inspect(self) -> None:
        """Verify worksheet and column type inspection."""
        res_csv = await self.tool.execute(file_path="sales.csv", action="inspect")
        self.assertEqual(res_csv["total_columns"], 4)
        col_names = [c["name"] for c in res_csv["columns"]]
        self.assertEqual(col_names, ["Region", "Category", "Revenue", "Units"])

        res_xlsx = await self.tool.execute(file_path="inventory.xlsx", action="inspect")
        self.assertEqual(res_xlsx["active_sheet"], "Products")
        self.assertEqual(res_xlsx["total_rows_inspected"], 3)

    async def test_02_summary_statistics(self) -> None:
        """Verify numeric statistical calculation (mean, median, min, max, quartiles)."""
        res = await self.tool.execute(file_path="inventory.xlsx", action="summary_statistics")
        qty_stats = res["column_statistics"]["Quantity"]
        self.assertEqual(qty_stats["type"], "numeric")
        self.assertEqual(qty_stats["min"], 30)
        self.assertEqual(qty_stats["max"], 100)
        self.assertEqual(qty_stats["median"], 50)

    async def test_03_missing_values_and_duplicates(self) -> None:
        """Verify detection of null values and duplicate rows."""
        res_nulls = await self.tool.execute(file_path="sales.csv", action="missing_values")
        rev_col = next(c for c in res_nulls["columns"] if c["column"] == "Revenue")
        self.assertEqual(rev_col["missing_count"], 1)

        res_dups = await self.tool.execute(file_path="sales.csv", action="detect_duplicates")
        self.assertEqual(res_dups["duplicate_count"], 1)

    async def test_04_group_aggregates(self) -> None:
        """Verify grouping by categorical column and summing/averaging numeric column."""
        res = await self.tool.execute(
            file_path="sales.csv",
            action="group_aggregates",
            group_by_column="Region",
            aggregate_column="Units",
        )
        self.assertGreater(len(res["groups"]), 0)
        north_grp = next(g for g in res["groups"] if g["group"] == "North")
        self.assertEqual(north_grp["sum"], 24.0)

    async def test_05_generate_report(self) -> None:
        """Verify analytical markdown report generation."""
        res = await self.tool.execute(file_path="sales.csv", action="generate_report")
        self.assertIn("# Spreadsheet Analysis Report: sales.csv", res["report_markdown"])
        self.assertIn("Column Overview", res["report_markdown"])


class TestBrowserAutomationTool(unittest.IsolatedAsyncioTestCase):
    """Test suite for BrowserAutomationTool security, approval, and SSRF restrictions."""

    async def asyncSetUp(self) -> None:
        self.tool = BrowserAutomationTool()

    async def test_01_ssrf_blocking(self) -> None:
        """Verify browser automation blocks private networks, localhost, loopback, and metadata."""
        blocked_urls = [
            "http://127.0.0.1:8080",
            "http://localhost:3000",
            "http://10.0.0.1/admin",
            "http://192.168.1.1/setup",
            "http://169.254.169.254/latest/meta-data/",
            "file:///etc/passwd",
        ]
        for url in blocked_urls:
            with self.assertRaises(ValueError):
                await self.tool.execute(action="navigate", url=url)

    async def test_02_approval_requirement(self) -> None:
        """Verify tool declares SENSITIVE permission and requires user approval."""
        self.assertEqual(self.tool.permission, ToolPermission.SENSITIVE)
        self.assertTrue(self.tool.requires_approval)

    async def test_03_challenge_detection(self) -> None:
        """Verify anti-bot / verification challenges are detected to halt automation safely."""
        sample_challenge = "<html><body><h1>Attention Required! | Cloudflare</h1><div class='cf-turnstile'></div></body></html>"
        res = self.tool._check_challenges(sample_challenge)
        self.assertIsNotNone(res)
        self.assertIn("challenge", res.lower())

        sample_clean = "<html><body><h1>Welcome to Public Docs</h1><p>Documentation text here.</p></body></html>"
        self.assertIsNone(self.tool._check_challenges(sample_clean))


class TestWorkflowEngineAndControl(unittest.IsolatedAsyncioTestCase):
    """Test suite for persistent PostgreSQL/SQLite WorkflowEngine and WorkflowControlTool."""

    async def asyncSetUp(self) -> None:
        # Create in-memory SQLite async engine with Base metadata
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.session_factory = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

        # Setup ToolRegistry with mock tools
        self.registry = ToolRegistry()
        self.registry.register(
            name="add_step",
            description="Adds numbers",
            func=lambda a, b: {"sum": a + b},
            permission=ToolPermission.READ_ONLY,
        )
        self.registry.register(
            name="multiply_step",
            description="Multiplies numbers",
            func=lambda a, b: {"product": a * b},
            permission=ToolPermission.READ_ONLY,
        )
        self.executor = ToolExecutor(self.registry)
        self.wf_engine = WorkflowEngine(session_factory=self.session_factory, tool_executor=self.executor)
        self.tool = WorkflowControlTool(workflow_engine=self.wf_engine)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_01_create_and_execute_workflow(self) -> None:
        """Verify multi-step workflow execution with context interpolation and checkpoints."""
        steps = [
            {"name": "step_add", "tool_name": "add_step", "arguments": {"a": 5, "b": 10}},
            {"name": "step_mult", "tool_name": "multiply_step", "arguments": {"a": "{context.step_add.sum}", "b": 3}},
        ]

        # 1. Create workflow via tool
        res_create = await self.tool.execute(
            action="create_workflow",
            user_id=101,
            name="Math Pipeline",
            steps=steps,
        )
        self.assertTrue(res_create["success"])
        wf_id = res_create["workflow"]["workflow_id"]

        # 2. Start workflow
        res_start = await self.tool.execute(action="start_workflow", workflow_id=wf_id, user_id=101)
        self.assertEqual(res_start["result"]["status"], "completed")
        self.assertEqual(res_start["result"]["total_steps"], 2)

        # 3. Check persistent status in DB
        res_status = await self.tool.execute(action="get_status", workflow_id=wf_id, user_id=101)
        wf_data = res_status["workflow"]
        self.assertEqual(wf_data["status"], "completed")
        self.assertEqual(wf_data["current_step_index"], 2)
        # Context contains outputs of both steps
        self.assertEqual(wf_data["context_data"]["step_add"]["sum"], 15)
        self.assertEqual(wf_data["context_data"]["step_mult"]["product"], 45)
        self.assertEqual(len(wf_data["execution_history"]), 2)

    async def test_02_pause_resume_and_cancel(self) -> None:
        """Verify workflow pause, resume, and cancellation."""
        steps = [
            {"name": "step_1", "tool_name": "add_step", "arguments": {"a": 1, "b": 1}},
            {"name": "step_2", "tool_name": "add_step", "arguments": {"a": 2, "b": 2}},
        ]
        res_create = await self.tool.execute(action="create_workflow", user_id=101, name="Pausable Workflow", steps=steps)
        wf_id = res_create["workflow"]["workflow_id"]

        # Pause workflow
        res_pause = await self.tool.execute(action="pause_workflow", workflow_id=wf_id, user_id=101)
        self.assertTrue(res_pause["paused"])

        res_status = await self.tool.execute(action="get_status", workflow_id=wf_id, user_id=101)
        self.assertEqual(res_status["workflow"]["status"], "paused")

        # Resume workflow
        res_resume = await self.tool.execute(action="resume_workflow", workflow_id=wf_id, user_id=101)
        self.assertEqual(res_resume["result"]["status"], "completed")

        # Create another workflow to cancel
        res_create2 = await self.tool.execute(action="create_workflow", user_id=101, name="Cancelable", steps=steps)
        wf_id2 = res_create2["workflow"]["workflow_id"]
        res_cancel = await self.tool.execute(action="cancel_workflow", workflow_id=wf_id2, user_id=101)
        self.assertTrue(res_cancel["cancelled"])

        res_status2 = await self.tool.execute(action="get_status", workflow_id=wf_id2, user_id=101)
        self.assertEqual(res_status2["workflow"]["status"], "cancelled")

    async def test_03_restart_recovery(self) -> None:
        """Verify recover_on_startup transitions interrupted 'running' workflows to 'paused' without duplicate execution."""
        # Manually create a workflow stuck in 'running' status (simulating mid-execution crash)
        async with self.session_factory() as session:
            repo = WorkflowRepository(session)
            wf = await repo.create_workflow(
                user_id=101,
                name="Crashed Workflow",
                steps=[{"name": "s1", "tool_name": "add_step", "arguments": {"a": 10, "b": 20}}],
            )
            await repo.update_status(wf.id, "running")

        # Run startup recovery
        recovered_count = await self.wf_engine.recover_on_startup()
        self.assertEqual(recovered_count, 1)

        # Verify status is safely paused with informative message
        status = await self.wf_engine.get_status(wf.id, user_id=101)
        self.assertEqual(status["status"], "paused")
        self.assertIn("interrupted", status["error_message"].lower())


class TestToolRegistryPhase3Integration(unittest.TestCase):
    """Test suite verifying ToolRegistry integration with all P0, P1, and P2 tools."""

    def test_01_register_all_tools(self) -> None:
        """Verify register_builtin_tools installs all 11 tools and Gemini declarations work."""
        mock_mm = MagicMock()
        mock_llm = MagicMock()
        mock_wf = MagicMock()
        registry = ToolRegistry()

        registry.register_builtin_tools(
            memory_manager=mock_mm,
            llm_manager=mock_llm,
            workflow_engine=mock_wf,
        )

        tools = registry.list_tools()
        names = {t["name"] for t in tools}

        expected = {
            # P0
            "calculator",
            "datetime_calculator",
            "text_processor",
            "file_reader",
            "web_page_reader",
            "memory_operations",
            # P1
            "conversion_tool",
            "data_processor",
            "document_generator",
            "spreadsheet_analyzer",
            # P2
            "browser_automation",
            "workflow_control",
        }
        self.assertTrue(expected.issubset(names))

        # Check Gemini declarations can be generated for all 12 tools
        declarations = registry.get_gemini_declarations()
        self.assertGreaterEqual(len(declarations), 11)


if __name__ == "__main__":
    unittest.main()

