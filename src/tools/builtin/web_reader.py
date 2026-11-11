"""Production-grade secure web page reader with comprehensive SSRF protection and HTML extraction."""

import ipaddress
import logging
import socket
from typing import Any
import urllib.parse

from bs4 import BeautifulSoup
import httpx

from src.tools.base import BaseTool
from src.tools.permissions import ToolPermission

logger = logging.getLogger(__name__)

# Maximum response size allowed (5 MB)
MAX_RESPONSE_BYTES = 5 * 1024 * 1024

# SSRF blocked private, loopback, and link-local CIDRs
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # Link-local & cloud metadata
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("224.0.0.0/4"),    # Multicast
    ipaddress.ip_network("240.0.0.0/4"),    # Reserved
    ipaddress.ip_network("::1/128"),        # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),       # IPv6 Unique Local Address
    ipaddress.ip_network("fe80::/10"),      # IPv6 Link-local
]


def validate_url_security(url_str: str) -> str:
    """Validate URL scheme and resolve DNS to block SSRF attacks against private/internal networks."""
    clean_url = url_str.strip()
    parsed = urllib.parse.urlsplit(clean_url)

    if parsed.scheme.lower() not in ("http", "https"):
        raise ValueError(f"Scheme '{parsed.scheme}' is not allowed. Only HTTP and HTTPS URLs are permitted.")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL must contain a valid hostname.")

    hostname_lower = hostname.lower()

    # Block common local hostnames
    if hostname_lower in ("localhost", "127.0.0.1", "::1", "0.0.0.0", "metadata.google.internal"):
        raise ValueError(f"Access to private/local host '{hostname}' is blocked for security.")

    # Resolve hostname to IP addresses and check against blocked networks
    try:
        addr_info = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise ValueError(f"DNS resolution failed for hostname '{hostname}': {exc}") from exc

    for entry in addr_info:
        ip_str = entry[4][0]
        ip_obj = ipaddress.ip_address(ip_str)

        # Check if IP is private, loopback, or link-local
        if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local or ip_obj.is_multicast or ip_obj.is_reserved:
            raise ValueError(f"Host '{hostname}' resolved to blocked private/internal IP {ip_str}.")

        for net in _BLOCKED_NETWORKS:
            if ip_obj in net:
                raise ValueError(f"Host '{hostname}' resolved to blocked IP range {net}.")

    return clean_url


class WebReaderTool(BaseTool):
    """Tool for securely fetching and extracting readable text, metadata, headings, and links from URLs."""

    @property
    def name(self) -> str:
        return "web_page_reader"

    @property
    def description(self) -> str:
        return (
            "Securely fetch and extract readable content, title, metadata, headings, and links from "
            "a publicly accessible web page. Protected against SSRF and private network access."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Publicly accessible HTTP or HTTPS URL to read.",
                },
                "extract_mode": {
                    "type": "string",
                    "description": "Content extraction focus.",
                    "enum": ["main_content", "metadata", "headings", "links", "full_text"],
                },
                "max_characters": {
                    "type": "integer",
                    "description": "Maximum characters of text to return (default 15,000).",
                },
            },
            "required": ["url"],
        }

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.READ_ONLY

    @property
    def timeout_seconds(self) -> float:
        return 20.0

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        raw_url = kwargs.get("url")
        if not raw_url:
            raise ValueError("The 'url' parameter is required.")

        validated_url = validate_url_security(raw_url)
        extract_mode = kwargs.get("extract_mode", "main_content")
        max_chars = min(kwargs.get("max_characters", 15000), 30000)

        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; AIagent-Reader/2.0; +https://github.com/aiagent)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

        # Follow redirects securely with manual validation on each hop
        current_url = validated_url
        response = None
        max_redirects = 5

        timeout = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)

        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            for _ in range(max_redirects + 1):
                validate_url_security(current_url)
                try:
                    response = await client.get(current_url, headers=headers)
                except httpx.TimeoutException as exc:
                    raise TimeoutError(f"HTTP request to '{current_url}' timed out.") from exc
                except httpx.RequestError as exc:
                    raise ValueError(f"Failed connecting to '{current_url}': {exc}") from exc

                # Check redirect
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        break
                    current_url = urllib.parse.urljoin(current_url, location)
                    continue
                else:
                    break

        if not response or response.status_code >= 400:
            status = response.status_code if response else "Unknown"
            raise ValueError(f"HTTP error {status} when fetching URL '{validated_url}'.")

        # Verify Content-Type
        content_type = response.headers.get("content-type", "").lower()
        if not any(t in content_type for t in ("text/html", "text/plain", "application/xhtml")):
            raise ValueError(f"Unsupported content type '{content_type}'. Only HTML and text pages can be read.")

        # Limit bytes read
        raw_html = response.text[:MAX_RESPONSE_BYTES]
        soup = BeautifulSoup(raw_html, "html.parser")

        # Extract metadata
        title = soup.title.string.strip() if soup.title and soup.title.string else ""
        meta_desc = ""
        desc_tag = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
        if desc_tag and desc_tag.get("content"):
            meta_desc = desc_tag["content"].strip()

        if extract_mode == "metadata":
            return {
                "action": "web_page_reader",
                "url": current_url,
                "status_code": response.status_code,
                "title": title,
                "description": meta_desc,
                "content_type": content_type,
            }

        # Extract headings
        headings = []
        for tag in soup.find_all(["h1", "h2", "h3"]):
            text_h = tag.get_text(strip=True)
            if text_h:
                headings.append({"level": tag.name.upper(), "text": text_h})
        if extract_mode == "headings":
            return {
                "action": "web_page_reader",
                "url": current_url,
                "title": title,
                "headings_count": len(headings),
                "headings": headings[:50],
            }

        # Extract outgoing links
        links = []
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"].strip()
            anchor = a_tag.get_text(strip=True)
            if href and not href.startswith(("#", "javascript:", "mailto:")):
                abs_url = urllib.parse.urljoin(current_url, href)
                links.append({"text": anchor[:50], "url": abs_url})
        if extract_mode == "links":
            return {
                "action": "web_page_reader",
                "url": current_url,
                "title": title,
                "links_count": len(links),
                "links": links[:60],
            }

        # Main content extraction: strip non-content elements
        for element in soup(["script", "style", "nav", "footer", "header", "aside", "noscript", "svg", "form"]):
            element.decompose()

        # Find main article or body
        main_container = soup.find("article") or soup.find("main") or soup.find("div", class_=lambda c: c and "content" in c) or soup.body

        if main_container:
            clean_text = main_container.get_text(separator="\n", strip=True)
        else:
            clean_text = soup.get_text(separator="\n", strip=True)

        # Collapse excessive newlines
        import re
        clean_text = re.sub(r"\n{3,}", "\n\n", clean_text)
        is_truncated = len(clean_text) > max_chars
        content_out = clean_text[:max_chars]

        return {
            "action": "web_page_reader",
            "url": current_url,
            "title": title,
            "description": meta_desc,
            "characters_extracted": len(content_out),
            "is_truncated": is_truncated,
            "content": content_out,
        }

