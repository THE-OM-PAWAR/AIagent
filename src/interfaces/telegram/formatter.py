"""Telegram HTML and Markdown formatting utilities for production AI responses."""

from __future__ import annotations

import html
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telegram import Message

logger = logging.getLogger(__name__)


def balance_html_tags(text: str) -> str:
    """Ensure all opened supported Telegram HTML tags (<pre>, <code>, <b>, <i>, <s>, <u>, <blockquote>) are closed."""
    supported_tags = ["pre", "code", "b", "i", "s", "u", "blockquote"]
    open_stack: list[str] = []

    # Match all tags
    for match in re.finditer(r"</?([a-zA-Z]+)(?:\s+[^>]*)?>", text):
        tag_name = match.group(1).lower()
        if tag_name not in supported_tags:
            continue
        is_closing = match.group(0).startswith("</")
        if not is_closing:
            open_stack.append(tag_name)
        else:
            if tag_name in open_stack:
                # Remove the most recent matching tag
                for idx in range(len(open_stack) - 1, -1, -1):
                    if open_stack[idx] == tag_name:
                        open_stack.pop(idx)
                        break

    # Close any tags still open in reverse order
    for tag in reversed(open_stack):
        text += f"</{tag}>"

    return text


def strip_html(text: str) -> str:
    """Strip HTML tags for safe plain-text fallback."""
    clean = re.sub(r"<[^>]+>", "", text)
    return html.unescape(clean)


def markdown_to_telegram_html(text: str) -> str:
    """Convert standard LLM Markdown into valid, beautifully styled Telegram HTML.

    Transforms:
        - ```lang ... ``` -> <pre><code class="language-lang">...</code></pre>
        - `inline code` -> <code>...</code>
        - ### Headers -> <b>Header</b>
        - --- or *** dividers -> clean spacing (removed)
        - * item or - item -> • item
        - **bold** -> <b>bold</b>
        - _italic_ -> <i>italic</i>
        - ~~strikethrough~~ -> <s>strikethrough</s>
        - > blockquote -> <blockquote>blockquote</blockquote>
        - [text](url) -> <a href="url">text</a>
    """
    if not text or not text.strip():
        return ""

    # 1. Protect and format fenced code blocks
    code_blocks: list[str] = []

    def _save_code_block(match: re.Match[str]) -> str:
        lang = match.group(1).strip() if match.group(1) else ""
        raw_code = match.group(2)
        escaped_code = html.escape(raw_code.rstrip())
        idx = len(code_blocks)
        if lang:
            code_blocks.append(f'<pre><code class="language-{lang}">{escaped_code}</code></pre>')
        else:
            code_blocks.append(f"<pre><code>{escaped_code}</code></pre>")
        return f"\x00CB_{idx}\x00"

    # Match ```optional_lang\n code \n```
    text = re.sub(r"```([a-zA-Z0-9_+-]*)\n?(.*?)\n?```", _save_code_block, text, flags=re.DOTALL)

    # 2. Protect and format inline code
    inline_codes: list[str] = []

    def _save_inline_code(match: re.Match[str]) -> str:
        raw_code = match.group(1)
        escaped_code = html.escape(raw_code)
        idx = len(inline_codes)
        inline_codes.append(f"<code>{escaped_code}</code>")
        return f"\x00IC_{idx}\x00"

    text = re.sub(r"`([^`\n]+)`", _save_inline_code, text)

    # 3. Escape HTML characters in all regular prose
    text = html.escape(text)

    # 4. Remove Markdown horizontal rules (---, ___, ***)
    text = re.sub(r"^[ \t]*[-*_]{3,}[ \t]*$", "", text, flags=re.MULTILINE)

    # 5. Convert Markdown headers (#, ##, ###, ####) to bold lines
    text = re.sub(r"^[ \t]*#{1,6}[ \t]+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)

    # 6. Convert bullet lists (* or -) to clean unicode bullet points
    text = re.sub(r"^[ \t]*[*\-][ \t]+", "• ", text, flags=re.MULTILINE)

    # 7. Convert blockquotes (> quote)
    text = re.sub(r"^[ \t]*&gt;[ \t]+(.+)$", r"<blockquote>\1</blockquote>", text, flags=re.MULTILINE)

    # 8. Convert bold: **text**
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)

    # 9. Convert strikethrough: ~~text~~
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)

    # 10. Convert italic: _text_ (preserving snake_case identifiers like user_id_101)
    text = re.sub(r"(?<!\w)_([^\n_]+?)_(?!\w)", r"<i>\1</i>", text)

    # 11. Convert links: [text](url)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^\s\)]+)\)", r'<a href="\2">\1</a>', text)

    # 12. Normalize multiple empty lines to at most 2 newlines
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    # 13. Restore code blocks and inline code
    for idx, cb in enumerate(code_blocks):
        text = text.replace(f"\x00CB_{idx}\x00", cb)
    for idx, ic in enumerate(inline_codes):
        text = text.replace(f"\x00IC_{idx}\x00", ic)

    # 14. Ensure all HTML tags are balanced
    return balance_html_tags(text)


async def send_formatted_reply(
    message: Message,
    text: str,
    max_length: int = 4000,
) -> list[Message]:
    """Format and send message chunks to Telegram with HTML formatting and plain-text fallback."""
    from telegram.constants import ParseMode
    from src.interfaces.telegram.handlers import split_message

    raw_chunks = split_message(text, max_length=max_length)
    sent_messages: list[Message] = []

    for chunk in raw_chunks:
        formatted = markdown_to_telegram_html(chunk)
        try:
            sent = await message.reply_text(
                formatted,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            sent_messages.append(sent)
        except Exception as exc:
            logger.warning(
                "Telegram HTML parse error: %s. Falling back to plain text for chunk.",
                exc,
            )
            # Safe fallback: plain text without any HTML tags
            plain_chunk = strip_html(formatted) if formatted else chunk
            sent = await message.reply_text(
                plain_chunk,
                disable_web_page_preview=True,
            )
            sent_messages.append(sent)

    return sent_messages

