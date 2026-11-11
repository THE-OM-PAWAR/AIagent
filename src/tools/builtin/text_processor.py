"""Production-grade deterministic and semantic text processing engine."""

from collections import Counter
import difflib
import logging
import re
from typing import Any

from src.tools.base import BaseTool
from src.tools.permissions import ToolPermission

logger = logging.getLogger(__name__)

# Regular expressions for structured entity extraction
_EMAIL_REGEX = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,7}\b")
_URL_REGEX = re.compile(r"https?://(?:www\.)?[-a-zA-Z0-9@:%._+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b(?:[-a-zA-Z0-9()@:%_+.~#?&/=]*)")
_PHONE_REGEX = re.compile(r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{2,4}\)?[-.\s]?\d{3,4}[-.\s]?\d{3,4}\b")
_MD_HEADING_REGEX = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_MD_LINK_REGEX = re.compile(r"\[([^\]]+)\]\((https?://[^\)]+)\)")
_CODE_BLOCK_REGEX = re.compile(r"```([a-zA-Z0-9_-]*)\n(.*?)```", re.DOTALL)


class TextProcessorTool(BaseTool):
    """Tool for deterministic and semantic text analysis, extraction, formatting, and comparison."""

    def __init__(self, llm_manager: Any = None) -> None:
        self.llm_manager = llm_manager

    @property
    def name(self) -> str:
        return "text_processor"

    @property
    def description(self) -> str:
        return (
            "Analyze and process text: word/character counts, reading time, pattern matching (regex), "
            "structured extraction (emails, URLs, phones, markdown links, code blocks), text comparison "
            "(diff & similarity), formatting, and semantic summarization/rewriting."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Text operation to perform.",
                    "enum": [
                        "count_metrics",
                        "pattern_match",
                        "extract_structured",
                        "compare",
                        "format_text",
                        "summarize",
                        "rewrite",
                        "classify",
                    ],
                },
                "text": {"type": "string", "description": "Primary text content to analyze or process."},
                "comparison_text": {
                    "type": "string",
                    "description": "Secondary text for comparison or diff analysis.",
                },
                "pattern": {
                    "type": "string",
                    "description": "Regex pattern or search term for 'pattern_match'.",
                },
                "extraction_type": {
                    "type": "string",
                    "description": "Entity type to extract for 'extract_structured'.",
                    "enum": ["email", "url", "phone", "markdown_headings", "markdown_links", "code_blocks", "all"],
                },
                "format_style": {
                    "type": "string",
                    "description": "Formatting style for 'format_text'.",
                    "enum": ["clean_whitespace", "uppercase", "lowercase", "titlecase", "slugify", "remove_empty_lines"],
                },
                "summary_style": {
                    "type": "string",
                    "description": "Style for semantic summarization ('bullet_points', 'executive', 'concise').",
                },
                "target_tone": {
                    "type": "string",
                    "description": "Target tone for rewriting (e.g. 'professional', 'concise', 'casual', 'academic').",
                },
                "categories": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Candidate category labels for classification.",
                },
            },
            "required": ["action", "text"],
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        action = kwargs.get("action", "count_metrics")
        text = kwargs.get("text", "")

        if action == "count_metrics":
            words = text.split()
            chars_with_spaces = len(text)
            chars_no_spaces = len("".join(words))
            lines = text.splitlines()
            paragraphs = [p for p in text.split("\n\n") if p.strip()]
            sentences = [s for s in re.split(r"[.!?]+", text) if s.strip()]

            # Estimated reading time at average 200 words per minute
            reading_time_mins = round(len(words) / 200.0, 2)

            return {
                "action": "count_metrics",
                "words_count": len(words),
                "characters_with_spaces": chars_with_spaces,
                "characters_no_spaces": chars_no_spaces,
                "lines_count": len(lines),
                "paragraphs_count": len(paragraphs),
                "sentences_count": len(sentences),
                "estimated_reading_time_minutes": reading_time_mins,
            }

        elif action == "pattern_match":
            pattern_str = kwargs.get("pattern")
            if not pattern_str:
                raise ValueError("The 'pattern' parameter is required for action='pattern_match'.")

            try:
                regex = re.compile(pattern_str)
            except re.error as exc:
                raise ValueError(f"Invalid regular expression '{pattern_str}': {exc}") from exc

            matches = []
            for match in regex.finditer(text):
                start, end = match.span()
                matches.append({
                    "match": match.group(),
                    "start": start,
                    "end": end,
                })
                if len(matches) >= 100:  # Bound match results
                    break

            return {
                "action": "pattern_match",
                "pattern": pattern_str,
                "total_matches": len(matches),
                "matches": matches,
            }

        elif action == "extract_structured":
            etype = kwargs.get("extraction_type", "all")
            results: dict[str, Any] = {}

            if etype in ("email", "all"):
                results["emails"] = list(dict.fromkeys(_EMAIL_REGEX.findall(text)))
            if etype in ("url", "all"):
                results["urls"] = list(dict.fromkeys(_URL_REGEX.findall(text)))
            if etype in ("phone", "all"):
                results["phones"] = list(dict.fromkeys(_PHONE_REGEX.findall(text)))
            if etype in ("markdown_headings", "all"):
                headings = []
                for level, title in _MD_HEADING_REGEX.findall(text):
                    headings.append({"level": len(level), "title": title.strip()})
                results["headings"] = headings
            if etype in ("markdown_links", "all"):
                links = []
                for label, url in _MD_LINK_REGEX.findall(text):
                    links.append({"label": label, "url": url})
                results["markdown_links"] = links
            if etype in ("code_blocks", "all"):
                blocks = []
                for lang, code in _CODE_BLOCK_REGEX.findall(text):
                    blocks.append({"language": lang.strip() or "text", "lines": len(code.splitlines())})
                results["code_blocks"] = blocks

            return {
                "action": "extract_structured",
                "extraction_type": etype,
                "extracted": results,
            }

        elif action == "compare":
            other = kwargs.get("comparison_text")
            if other is None:
                raise ValueError("The 'comparison_text' parameter is required for action='compare'.")

            matcher = difflib.SequenceMatcher(None, text, other)
            similarity = round(matcher.ratio(), 4)

            # Generate unified diff
            lines_a = text.splitlines(keepends=True)
            lines_b = other.splitlines(keepends=True)
            diff_lines = list(difflib.unified_diff(lines_a, lines_b, fromfile="text_a", tofile="text_b"))

            additions = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
            deletions = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))

            return {
                "action": "compare",
                "similarity_ratio": similarity,
                "identical": similarity == 1.0,
                "additions_count": additions,
                "deletions_count": deletions,
                "diff_preview": "".join(diff_lines[:50]),
            }

        elif action == "format_text":
            style = kwargs.get("format_style", "clean_whitespace")
            if style == "clean_whitespace":
                # Normalize multiple spaces and linebreaks
                cleaned = re.sub(r"[ \t]+", " ", text)
                cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
            elif style == "uppercase":
                cleaned = text.upper()
            elif style == "lowercase":
                cleaned = text.lower()
            elif style == "titlecase":
                cleaned = text.title()
            elif style == "remove_empty_lines":
                cleaned = "\n".join(line for line in text.splitlines() if line.strip())
            elif style == "slugify":
                s = text.lower().strip()
                s = re.sub(r"[^\w\s-]", "", s)
                cleaned = re.sub(r"[-\s]+", "-", s)
            else:
                cleaned = text

            return {
                "action": "format_text",
                "style": style,
                "formatted_text": cleaned,
            }

        elif action in ("summarize", "rewrite", "classify"):
            # If LLMManager is available, invoke it; otherwise use clean extractive fallback
            if self.llm_manager and hasattr(self.llm_manager, "generate"):
                try:
                    if action == "summarize":
                        style = kwargs.get("summary_style", "concise bullet points")
                        prompt = f"Summarize the following text in {style}:\n\n{text[:4000]}"
                    elif action == "rewrite":
                        tone = kwargs.get("target_tone", "professional")
                        prompt = f"Rewrite the following text in a {tone} tone:\n\n{text[:4000]}"
                    else:
                        cats = kwargs.get("categories") or ["General", "Technical", "Administrative"]
                        prompt = f"Classify the following text into one of these categories {cats}. Return only the chosen category name and a 1-sentence justification:\n\n{text[:2000]}"

                    resp = await self.llm_manager.generate(
                        messages=[{"role": "user", "content": prompt}],
                        timeout=30.0,
                    )
                    return {
                        "action": action,
                        "result": resp.content.strip(),
                        "model": resp.model_name,
                    }
                except Exception as exc:
                    logger.warning("TextProcessor: LLM invocation for %s failed, falling back to extractive: %s", action, exc)

            # Fallback extractive logic when LLMManager is not passed
            if action == "summarize":
                sentences = [s.strip() for s in re.split(r"[.!?]+", text) if len(s.strip()) > 15]
                top_sentences = sentences[:3]
                return {
                    "action": "summarize",
                    "result": " • " + "\n • ".join(top_sentences) if top_sentences else text[:200],
                    "method": "extractive_fallback",
                }
            elif action == "rewrite":
                return {
                    "action": "rewrite",
                    "result": text.strip(),
                    "method": "direct_fallback",
                }
            else:
                return {
                    "action": "classify",
                    "result": "Unclassified (requires active LLMManager)",
                    "method": "fallback",
                }

        raise ValueError(f"Unknown text processing action '{action}'.")

