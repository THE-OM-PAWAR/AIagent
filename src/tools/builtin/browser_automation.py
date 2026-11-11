"""Production-grade secure browser automation tool powered by isolated ephemeral Playwright contexts."""

import asyncio
import logging
from pathlib import Path
import re
import time
from typing import Any

from src.tools.base import BaseTool
from src.tools.builtin.web_reader import validate_url_security
from src.tools.permissions import ToolPermission

logger = logging.getLogger(__name__)

DEFAULT_SCREENSHOT_DIR = Path("data/generated").resolve()
BROWSER_TIMEOUT_MS = 15000  # 15 seconds timeout
MAX_EXTRACT_CHARS = 20000

# Known anti-bot / challenge markers to detect and halt immediately per guidelines
_CHALLENGE_PATTERNS = [
    re.compile(r"cf-turnstile|cf-challenge|challenge-platform", re.IGNORECASE),
    re.compile(r"g-recaptcha|hcaptcha|arkoselabs|perimeterx", re.IGNORECASE),
    re.compile(r"just a moment\.\.\.|attention required! \| cloudflare", re.IGNORECASE),
    re.compile(r"verify you are human|press & hold to confirm", re.IGNORECASE),
]


class BrowserAutomationTool(BaseTool):
    """Secure, isolated browser automation tool for public web page navigation, inspection, form interaction, and screenshots."""

    def __init__(self, output_dir: Path | str | None = None) -> None:
        self.output_dir = Path(output_dir).resolve() if output_dir else DEFAULT_SCREENSHOT_DIR

    @property
    def name(self) -> str:
        return "browser_automation"

    @property
    def description(self) -> str:
        return (
            "Automate public web page navigation, visual rendering, screenshots, and form interactions "
            "using ephemeral isolated browser contexts. Protected against SSRF and private network access. "
            "Requires user approval for state-mutating actions (clicking buttons, submitting forms). "
            "Never stores credentials or bypasses CAPTCHAs."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Browser action to execute.",
                    "enum": [
                        "navigate",
                        "extract_content",
                        "screenshot",
                        "click_element",
                        "fill_form",
                    ],
                },
                "url": {
                    "type": "string",
                    "description": "Publicly accessible HTTP or HTTPS URL to visit.",
                },
                "selector": {
                    "type": "string",
                    "description": "CSS selector for click_element or form interaction (e.g. '#submit-btn', 'input[name=q]').",
                },
                "form_fields": {
                    "type": "object",
                    "description": "Key-value mapping of CSS selector -> string text to fill into inputs for action='fill_form'.",
                },
                "wait_for_selector": {
                    "type": "string",
                    "description": "Optional CSS selector to wait for before extracting content or capturing screenshot.",
                },
                "user_id": {
                    "type": "integer",
                    "description": "Telegram user ID for organizing sandboxed screenshot files.",
                },
            },
            "required": ["action", "url"],
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.SENSITIVE

    @property
    def requires_approval(self) -> bool:
        return True

    def _get_screenshot_dir(self, user_id: int | None) -> Path:
        target = self.output_dir / (str(user_id) if user_id else "general") / "screenshots"
        target.mkdir(parents=True, exist_ok=True)
        return target

    def _check_challenges(self, page_content: str) -> str | None:
        """Check if target page is gated by anti-bot verification or CAPTCHA."""
        for pattern in _CHALLENGE_PATTERNS:
            if pattern.search(page_content):
                return "Anti-bot challenge or verification gate detected ('verify you are human' / CAPTCHA). Automation halted."
        return None

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        action = kwargs.get("action", "navigate")
        raw_url = kwargs.get("url")
        if not raw_url:
            raise ValueError("The 'url' parameter is required.")

        # 1. Enforce SSRF security
        validated_url = validate_url_security(raw_url)

        user_id = kwargs.get("user_id")
        selector = kwargs.get("selector")
        form_fields = kwargs.get("form_fields") or {}
        wait_selector = kwargs.get("wait_for_selector")

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError(
                "Playwright package is not available in the current environment. "
                "Install playwright and run 'playwright install chromium'."
            )

        start_time = time.perf_counter()

        try:
            async with async_playwright() as p:
                try:
                    browser = await p.chromium.launch(
                        headless=True,
                        args=[
                            "--no-sandbox",
                            "--disable-setuid-sandbox",
                            "--disable-dev-shm-usage",
                            "--disable-gpu",
                        ],
                    )
                except Exception as exc:
                    return {
                        "action": action,
                        "success": False,
                        "url": validated_url,
                        "error": (
                            f"Failed to launch headless Chromium: {exc}. "
                            "Ensure Playwright browser binaries are installed via 'playwright install chromium'."
                        ),
                    }

                # Create ephemeral isolated context without saved credentials or profiles
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                    ignore_https_errors=False,
                )
                page = await context.new_page()

                try:
                    # Navigate with strict timeout
                    await page.goto(validated_url, timeout=BROWSER_TIMEOUT_MS, wait_until="domcontentloaded")

                    # Verify final URL post-redirect for SSRF protection
                    final_url = page.url
                    validate_url_security(final_url)

                    # Check for bot challenge
                    html_content = await page.content()
                    challenge = self._check_challenges(html_content)
                    if challenge:
                        await context.close()
                        await browser.close()
                        return {
                            "action": action,
                            "success": False,
                            "url": final_url,
                            "blocked": True,
                            "reason": challenge,
                        }

                    if wait_selector:
                        try:
                            await page.wait_for_selector(wait_selector, timeout=5000)
                        except Exception:
                            logger.debug("wait_for_selector timed out for '%s'", wait_selector)

                    title = await page.title()

                    if action in ("navigate", "extract_content"):
                        body_text = await page.inner_text("body")
                        clean_text = re.sub(r"\n{3,}", "\n\n", body_text.strip())[:MAX_EXTRACT_CHARS]
                        elapsed = round(time.perf_counter() - start_time, 2)
                        await context.close()
                        await browser.close()
                        return {
                            "action": action,
                            "success": True,
                            "url": final_url,
                            "title": title,
                            "text_preview": clean_text[:2000],
                            "characters_extracted": len(clean_text),
                            "execution_time_seconds": elapsed,
                        }

                    elif action == "screenshot":
                        target_dir = self._get_screenshot_dir(user_id)
                        filename = f"screenshot_{int(time.time())}.png"
                        file_path = target_dir / filename
                        await page.screenshot(path=str(file_path), full_page=False)
                        elapsed = round(time.perf_counter() - start_time, 2)
                        await context.close()
                        await browser.close()
                        return {
                            "action": "screenshot",
                            "success": True,
                            "url": final_url,
                            "title": title,
                            "filename": filename,
                            "relative_path": str(file_path.relative_to(self.output_dir.parent)),
                            "size_bytes": file_path.stat().st_size,
                            "execution_time_seconds": elapsed,
                        }

                    elif action == "click_element":
                        if not selector:
                            raise ValueError("The 'selector' parameter is required for action='click_element'.")
                        await page.click(selector, timeout=5000)
                        await page.wait_for_timeout(1000)
                        new_url = page.url
                        validate_url_security(new_url)
                        new_title = await page.title()
                        elapsed = round(time.perf_counter() - start_time, 2)
                        await context.close()
                        await browser.close()
                        return {
                            "action": "click_element",
                            "success": True,
                            "selector_clicked": selector,
                            "current_url": new_url,
                            "current_title": new_title,
                            "execution_time_seconds": elapsed,
                        }

                    elif action == "fill_form":
                        if not form_fields:
                            raise ValueError("The 'form_fields' mapping is required for action='fill_form'.")
                        for sel, text_val in form_fields.items():
                            await page.fill(sel, str(text_val), timeout=5000)
                        if selector:
                            # If a submit button selector is supplied, submit form
                            await page.click(selector, timeout=5000)
                            await page.wait_for_timeout(1500)
                        new_url = page.url
                        validate_url_security(new_url)
                        new_title = await page.title()
                        elapsed = round(time.perf_counter() - start_time, 2)
                        await context.close()
                        await browser.close()
                        return {
                            "action": "fill_form",
                            "success": True,
                            "fields_filled": list(form_fields.keys()),
                            "current_url": new_url,
                            "current_title": new_title,
                            "execution_time_seconds": elapsed,
                        }

                    else:
                        raise ValueError(f"Unknown action '{action}' for browser_automation.")

                finally:
                    try:
                        await context.close()
                    except Exception:
                        pass
                    try:
                        await browser.close()
                    except Exception:
                        pass

        except Exception as exc:
            return {
                "action": action,
                "success": False,
                "url": validated_url,
                "error": str(exc),
            }

