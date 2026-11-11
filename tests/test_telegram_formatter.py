"""Unit tests for Telegram HTML formatter and message delivery."""

import unittest
from unittest.mock import AsyncMock, MagicMock

from telegram.constants import ParseMode

from src.interfaces.telegram.formatter import (
    balance_html_tags,
    markdown_to_telegram_html,
    send_formatted_reply,
    strip_html,
)


class TestTelegramFormatter(unittest.TestCase):
    """Test markdown to Telegram HTML conversion and safety utilities."""

    def test_headers_conversion(self) -> None:
        md = "### 📄 Executive Summary\n#### 1. Professional Experience"
        html_out = markdown_to_telegram_html(md)
        self.assertIn("<b>📄 Executive Summary</b>", html_out)
        self.assertIn("<b>1. Professional Experience</b>", html_out)
        self.assertNotIn("###", html_out)
        self.assertNotIn("####", html_out)

    def test_horizontal_rules_removed(self) -> None:
        md = "Header\n\n---\n\nBody text\n\n***\n\nEnd"
        html_out = markdown_to_telegram_html(md)
        self.assertNotIn("---", html_out)
        self.assertNotIn("***", html_out)
        self.assertIn("Header", html_out)
        self.assertIn("Body text", html_out)

    def test_bold_and_bullet_conversion(self) -> None:
        md = "* **Software Engineer:** Mentors students.\n* **Founder:** Built platform."
        html_out = markdown_to_telegram_html(md)
        self.assertIn("• <b>Software Engineer:</b> Mentors students.", html_out)
        self.assertIn("• <b>Founder:</b> Built platform.", html_out)
        self.assertNotIn("**", html_out)

    def test_code_blocks_escaped_and_protected(self) -> None:
        md = "Check this code:\n```python\ndef add(a, b):\n    return a < b and b > 0\n```\nDone."
        html_out = markdown_to_telegram_html(md)
        self.assertIn('<pre><code class="language-python">def add(a, b):\n    return a &lt; b and b &gt; 0</code></pre>', html_out)
        self.assertNotIn("< b", html_out)

    def test_inline_code_escaped(self) -> None:
        md = "Run `curl -X POST <api_url>&key=123` to test."
        html_out = markdown_to_telegram_html(md)
        self.assertIn("<code>curl -X POST &lt;api_url&gt;&amp;key=123</code>", html_out)

    def test_snake_case_not_mangled_by_italic(self) -> None:
        md = "The parameter is user_id_101 or account_balance_total."
        html_out = markdown_to_telegram_html(md)
        self.assertIn("user_id_101", html_out)
        self.assertIn("account_balance_total", html_out)
        self.assertNotIn("<i>", html_out)

    def test_links_and_blockquotes(self) -> None:
        md = "> Important notice\n[Google](https://google.com)"
        html_out = markdown_to_telegram_html(md)
        self.assertIn("<blockquote>Important notice</blockquote>", html_out)
        self.assertIn('<a href="https://google.com">Google</a>', html_out)

    def test_balance_html_tags(self) -> None:
        unclosed = "<b>Bold text <i>italic"
        balanced = balance_html_tags(unclosed)
        self.assertEqual(balanced, "<b>Bold text <i>italic</i></b>")

    def test_strip_html(self) -> None:
        html_text = "<b>Hello</b> &amp; <code>world</code>!"
        plain = strip_html(html_text)
        self.assertEqual(plain, "Hello & world!")

    def test_user_resume_screenshot_example(self) -> None:
        sample = (
            "Here is a comprehensive summary and breakdown of the uploaded resume for **Omprakash Pawar (Om Pawar)**:\n\n"
            "---\n\n"
            "### 📄 Executive Summary\n\n"
            "**Omprakash Pawar** is an **AI Backend Engineer & Product Builder**.\n\n"
            "---\n\n"
            "### 🔑 Key Points\n\n"
            "#### 1. Professional Experience\n"
            "* **Software Engineer & Teaching Assistant @ Sheryians AI School:** Mentors students.\n"
            "* **Founder & Product Engineer @ GetStay.in:** Built and operates a hostel marketplace serving **50+ hostels**.\n"
        )
        res = markdown_to_telegram_html(sample)
        # Verify all markdown artifacts from screenshot are formatted
        self.assertNotIn("**", res)
        self.assertNotIn("###", res)
        self.assertNotIn("####", res)
        self.assertNotIn("---", res)
        self.assertIn("<b>Omprakash Pawar (Om Pawar)</b>", res)
        self.assertIn("<b>📄 Executive Summary</b>", res)
        self.assertIn("• <b>Software Engineer &amp; Teaching Assistant @ Sheryians AI School:</b>", res)
        self.assertIn("• <b>Founder &amp; Product Engineer @ GetStay.in:</b>", res)


class TestSendFormattedReply(unittest.IsolatedAsyncioTestCase):
    """Test message chunking and dispatch with HTML parse mode and fallback."""

    async def test_send_formatted_reply_success(self) -> None:
        mock_msg = AsyncMock()
        mock_sent = MagicMock()
        mock_msg.reply_text.return_value = mock_sent

        text = "Hello **world**! Check `code`."
        sent = await send_formatted_reply(mock_msg, text)

        self.assertEqual(len(sent), 1)
        mock_msg.reply_text.assert_called_once()
        args, kwargs = mock_msg.reply_text.call_args
        self.assertIn("<b>world</b>", args[0])
        self.assertIn("<code>code</code>", args[0])
        self.assertEqual(kwargs.get("parse_mode"), ParseMode.HTML)

    async def test_send_formatted_reply_fallback_on_parse_error(self) -> None:
        from telegram.error import BadRequest

        mock_msg = AsyncMock()
        # First call fails with BadRequest (simulate invalid entity), second call succeeds (plain text)
        mock_msg.reply_text.side_effect = [
            BadRequest("Can't parse entities"),
            MagicMock(),
        ]

        text = "Hello **world**!"
        sent = await send_formatted_reply(mock_msg, text)

        self.assertEqual(len(sent), 1)
        self.assertEqual(mock_msg.reply_text.call_count, 2)
        # Second call should be plain text without parse_mode
        fallback_call = mock_msg.reply_text.call_args_list[1]
        args, kwargs = fallback_call
        self.assertEqual(args[0], "Hello world!")
        self.assertNotIn("parse_mode", kwargs)


if __name__ == "__main__":
    unittest.main()

