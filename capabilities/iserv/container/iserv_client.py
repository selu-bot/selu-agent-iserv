from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from email.message import Message
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://mags-greven.de"
SESSION_MAX_AGE_SECONDS = 15 * 60
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 8
MAX_LIST_PAGES = 20
DOWNLOAD_CHUNK_SIZE = 64 * 1024
PDF_TIMEOUT_SECONDS = 15
LETTER_INDEX = "/iserv/parentletter/parent/index"
LETTER_PREFIX = "/iserv/parentletter/parent/show/"
ATTACHMENT_PREFIXES = (
    "/iserv/parentletter/attachment/", "/iserv/file/download/",
    "/iserv/download/", "/iserv/attachment/",
)
CONFIRM_SELECTOR = 'form[name=form] button[name="form[submit]"][confirmation-type]'


class IServError(RuntimeError):
    """An error whose message is safe to return to the caller."""


class AuthenticationError(IServError):
    pass


@dataclass(frozen=True)
class LoginCredentials:
    username: str = field(repr=False)
    password: str = field(repr=False)


def validate_integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise IServError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


class IServClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "Selu IServ Capability/1.0"
        self._credentials: LoginCredentials | None = None
        self._authenticated_at: float | None = None
        self._base_url = DEFAULT_BASE_URL
        self._deadline: float | None = None
        self._context = None

    def set_request_context(self, context=None) -> None:
        self._context = context
        remaining = context.time_remaining() if context else None
        self._deadline = time.monotonic() + min(remaining if remaining is not None else 120, 120)

    def clear_request_context(self) -> None:
        self._context, self._deadline = None, None

    def _remaining_time(self) -> float:
        if self._context is not None and not self._context.is_active():
            raise IServError("Request was cancelled before the next operation")
        remaining = self._deadline - time.monotonic() if self._deadline is not None else 120
        if remaining <= 0:
            raise IServError("IServ request deadline exceeded")
        return remaining

    def _clear_auth(self) -> None:
        self.session.cookies.clear()
        self._authenticated_at = None

    def set_credentials(self, username: str, password: str) -> None:
        if not all(isinstance(v, str) and v.strip() for v in (username, password)):
            raise AuthenticationError("Non-empty USERNAME and PASSWORD are required")
        creds = LoginCredentials(username=username, password=password)
        if self._credentials != creds:
            self._clear_auth()
            self._credentials = creds

    def set_base_url(self, base_url: str | None) -> None:
        normalized_url = self._normalize_base_url(base_url)
        if normalized_url != self._base_url:
            self._clear_auth()
            self._base_url = normalized_url

    @staticmethod
    def _normalize_base_url(base_url: str | None) -> str:
        if base_url is not None and not isinstance(base_url, str):
            raise IServError("Invalid ISERV_BASE_URL: expected an HTTPS origin")
        raw = (base_url or DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL
        if "://" not in raw:
            raw = f"https://{raw}"
        try:
            parsed = urlsplit(raw)
            if (parsed.scheme != "https" or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                    or any(c.isspace() or c == "\\" for c in raw)):
                raise ValueError
            port = parsed.port
        except ValueError:
            raise IServError("Invalid ISERV_BASE_URL: use an HTTPS origin without a path or credentials") from None
        host = parsed.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        return f"https://{host}" + (f":{port}" if port and port != 443 else "")

    def _safe_url(self, href: str, *, base: str | None = None) -> str:
        """Validate BEFORE sending a request, including each redirect hop."""
        if (not isinstance(href, str) or not href or len(href) > 4096
                or any(ord(c) < 32 or ord(c) == 127 or c == "\\" for c in href)):
            raise IServError("Invalid IServ URL")
        try:
            url = urljoin(base or f"{self._base_url}/", href)
            parsed, origin = urlsplit(url), urlsplit(self._base_url)
            if (parsed.scheme != "https" or parsed.hostname != origin.hostname
                    or (parsed.port or 443) != (origin.port or 443)
                    or parsed.username is not None or parsed.password is not None):
                raise ValueError
        except ValueError:
            raise IServError("URL must use the configured IServ HTTPS host") from None
        # Reject encoded path traversal/separators, which servers can normalize
        # differently. Query parameters may legitimately contain encoded URLs.
        decoded = unquote(parsed.path)
        if ("%" in decoded or "\\" in decoded or decoded != parsed.path
                and any(c in decoded for c in ("/../", "/./"))
                or any(segment in {".", ".."} for segment in decoded.split("/"))
                or re.search(r"%2f|%5c", parsed.path, re.I)):
            raise IServError("Invalid IServ URL path")
        return urlunsplit(parsed._replace(fragment=""))

    def _resource_url(self, href: str, prefixes: tuple[str, ...]) -> str:
        url = self._safe_url(href)
        if not urlsplit(url).path.startswith(prefixes):
            raise IServError("URL does not identify a supported IServ resource")
        return url

    def is_authenticated(self) -> bool:
        if self._authenticated_at is None:
            return False
        if time.monotonic() - self._authenticated_at >= SESSION_MAX_AGE_SECONDS:
            self._clear_auth()
            return False
        return True

    def _read_body(self, response: requests.Response, maximum: int) -> bytes:
        try:
            length = int(response.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length > maximum:
            raise IServError(f"IServ response is too large (maximum {maximum} bytes)")
        chunks, size = [], 0
        started = time.monotonic()
        for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
            self._remaining_time()
            size += len(chunk)
            if size > maximum:
                raise IServError(f"IServ response is too large (maximum {maximum} bytes)")
            if time.monotonic() - started > 30:
                raise IServError("IServ response took too long to download")
            chunks.append(chunk)
        return b"".join(chunks)

    def _request(self, method: str, href: str, *, data: dict | None = None,
                 maximum: int = MAX_PAGE_BYTES) -> requests.Response:
        url = self._safe_url(href)
        try:
            for _ in range(MAX_REDIRECTS + 1):
                remaining = self._remaining_time()
                response = self.session.request(
                    method, url, data=data, timeout=(min(5, remaining), min(20, remaining)),
                    allow_redirects=False, stream=True,
                )
                with response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location:
                            raise IServError("IServ returned a redirect without a destination")
                        url = self._safe_url(location, base=url)
                        if response.status_code == 303 or (
                                method == "POST" and response.status_code in {301, 302}):
                            method, data = "GET", None
                        elif method == "POST":
                            # Replaying acknowledgements or credentials is unsafe.
                            raise IServError("IServ requested a POST replay; submission was not repeated")
                        continue
                    if response.status_code == 401:
                        raise AuthenticationError("IServ session expired")
                    if response.status_code == 403:
                        raise IServError("IServ denied access to this resource")
                    if not 200 <= response.status_code < 300:
                        raise IServError(f"IServ returned HTTP {response.status_code}")
                    # A successful request can still be the IServ HTML auth
                    # bridge; use the response's effective URL, not only the
                    # URL we requested.
                    effective_url = self._safe_url(response.url, base=url)
                    is_bridge = urlsplit(effective_url).path == "/iserv/auth/auth"
                    response._content = self._read_body(
                        response, MAX_PAGE_BYTES if is_bridge else maximum,
                    )
                    response._content_consumed = True
                    if is_bridge:
                        soup = BeautifulSoup(response.content, "lxml")
                        refresh = soup.select_one('meta[http-equiv="refresh" i]')
                        match = re.search(r"(?:^|;)\s*url\s*=\s*(.+)\s*$",
                                          refresh.get("content", "") if refresh else "", re.I)
                        if not match:
                            raise AuthenticationError("IServ authentication redirect has no destination")
                        url = self._safe_url(
                            unescape(match.group(1).strip().strip("\"'")), base=effective_url
                        )
                        method, data = "GET", None
                        continue
                    return response
        except requests.RequestException:
            # requests exceptions can contain URLs with auth tokens or letter IDs.
            raise IServError("IServ network request failed or timed out") from None
        raise IServError("Too many IServ redirects")

    @staticmethod
    def _login_response(response: requests.Response) -> bool:
        if urlsplit(response.url).path.rstrip("/") == "/iserv/auth/login":
            return True
        if "html" in response.headers.get("Content-Type", "").lower():
            return BeautifulSoup(response.content, "lxml").select_one('input[name="_password"]') is not None
        return False

    def login(self) -> None:
        if not self._credentials:
            raise AuthenticationError("Missing IServ credentials")
        self._clear_auth()
        response = self._request("GET", "/iserv/auth/login")
        soup = BeautifulSoup(response.content, "lxml")
        password = soup.select_one('input[name="_password"]')
        form = password.find_parent("form") if password else None
        if form is None:
            raise AuthenticationError("IServ login form was not recognized")
        data = {i["name"]: i.get("value", "") for i in form.select("input[type=hidden][name]")}
        data.update(_username=self._credentials.username, _password=self._credentials.password)
        action = self._safe_url(form.get("action") or response.url, base=response.url)
        if urlsplit(action).path != "/iserv/auth/login":
            raise AuthenticationError("IServ login form destination was not recognized")
        response = self._request("POST", action, data=data)
        if (self._login_response(response)
                or not urlsplit(response.url).path.startswith("/iserv/")
                or not any(cookie.name == "IServSession" for cookie in self.session.cookies)):
            self._clear_auth()
            raise AuthenticationError("Login failed; check credentials or additional login requirements")
        self._authenticated_at = time.monotonic()

    def _ensure_auth(self) -> None:
        if not self.is_authenticated():
            self.login()

    def _get_response(self, href: str, maximum: int = MAX_PAGE_BYTES) -> requests.Response:
        # Only safe GETs may be retried, once, after an expired session.
        for attempt in range(2):
            self._ensure_auth()
            try:
                response = self._request("GET", href, maximum=maximum)
                if self._login_response(response):
                    raise AuthenticationError("IServ session expired")
                return response
            except AuthenticationError:
                self._clear_auth()
                if attempt:
                    raise
        raise AuthenticationError("IServ session expired")

    def _get_page(self, href: str) -> BeautifulSoup:
        response = self._get_response(href)
        return BeautifulSoup(response.content, "lxml")

    def _post_page(self, href: str, data: dict) -> BeautifulSoup:
        self._ensure_auth()
        response = self._request("POST", href, data=data)
        if self._login_response(response):
            self._clear_auth()
            raise AuthenticationError("IServ session expired during submission")
        return BeautifulSoup(response.content, "lxml")

    def _list_pages(self, path: str):
        """Follow server pagination as well as exposing our local offset/limit."""
        url, visited = self._safe_url(path), set()
        for _ in range(MAX_LIST_PAGES):
            if url in visited:
                raise IServError("IServ pagination repeated a page; listing is incomplete")
            visited.add(url)
            soup = self._get_page(url)
            yield soup
            next_link = soup.select_one(
                'a[rel="next"], .pagination li.next:not(.disabled) a, '
                '.pagination .page-item:not(.disabled) a[aria-label="Next"]'
            )
            if next_link is None:
                return
            url = self._safe_url(next_link.get("href", ""), base=url)
            if urlsplit(url).path != urlsplit(path).path:
                raise IServError("IServ pagination left the expected listing")
        raise IServError("IServ listing exceeds the page limit; listing is incomplete")

    def get_parent_letters(self, limit: int = 20, offset: int = 0,
                           unread_only: bool = False) -> dict[str, Any]:
        validate_integer(limit, "limit", 1, 100)
        validate_integer(offset, "offset", 0, 100_000)
        if type(unread_only) is not bool:
            raise IServError("unread_only must be a boolean")
        letters, seen = [], set()
        for soup in self._list_pages(LETTER_INDEX):
            if soup.select_one("table tbody") is None:
                raise IServError("IServ parent-letter list was not recognized; no empty result was assumed")
            for row in soup.select("tbody tr"):
                letter = self._parse_parent_letter_row(row)
                if letter is None:
                    # An explicit colspan placeholder is valid for an empty table.
                    if row.select_one("td[colspan]") is not None:
                        continue
                    raise IServError("IServ parent-letter row was not recognized")
                self._resource_url(letter["href"], (LETTER_PREFIX,))
                if letter["href"] not in seen:
                    seen.add(letter["href"])
                    letters.append(letter)
        unread_total = sum(not letter["read"] for letter in letters)
        if unread_only:
            letters = [letter for letter in letters if not letter["read"]]
        letters.sort(key=lambda item: item["date_sort"], reverse=True)
        page = letters[offset:offset + limit]
        return {"letters": page, "total": len(letters), "unread_total": unread_total,
                "offset": offset, "limit": limit, "returned": len(page),
                "has_more": offset + len(page) < len(letters)}

    @staticmethod
    def _parse_parent_letter_row(row) -> dict[str, Any] | None:
        cells = row.select("td.iserv-admin-list-field")
        if not cells:
            return None
        link = cells[0].select_one("a")
        if len(cells) < 6 or link is None or not link.get_text(strip=True) or not link.get("href"):
            raise IServError("IServ parent-letter row is incomplete")
        try:
            date_sort = int(cells[5]["data-sort"].strip())
        except (KeyError, ValueError, AttributeError):
            raise IServError("IServ parent-letter date was not recognized") from None
        return {"title": link.get_text(strip=True), "href": link["href"],
                "date": cells[5].get_text(strip=True), "date_sort": date_sort,
                "read": not bool(set(row.get("class", [])) & {
                    "unread", "not-read", "iserv-admin-list-row-unread", "iserv-list-row-unread"
                }),
                "child": cells[1].get_text(strip=True), "sender": cells[2].get_text(strip=True),
                # The list does not establish acknowledgement requirements.
                "needs_confirmation": None}

    @staticmethod
    def _letter_body(soup: BeautifulSoup):
        body = soup.select_one("div.parent-letter-body")
        if body is None:
            raise IServError("IServ parent-letter content was not recognized")
        return body

    @staticmethod
    def _response_buttons(soup: BeautifulSoup):
        return [button for button in soup.select(CONFIRM_SELECTOR) if not button.has_attr("disabled")]

    def get_parent_letter_content(self, href: str) -> dict[str, Any]:
        self._resource_url(href, (LETTER_PREFIX,))
        soup = self._get_page(href)
        body = self._letter_body(soup)
        attachments, seen = [], set()
        for link in soup.select("a[href]"):
            link_href = link["href"]
            if not urlsplit(urljoin(self._base_url, link_href)).path.startswith(ATTACHMENT_PREFIXES):
                continue
            url = self._resource_url(link_href, ATTACHMENT_PREFIXES)
            if url not in seen:
                seen.add(url)
                attachments.append({"filename": link.get_text(strip=True) or "attachment",
                                    "href": link_href})
        response_types = sorted({b["confirmation-type"] for b in self._response_buttons(soup)})
        return {"body_text": body.get_text(separator="\n", strip=True),
                "attachments": attachments, "needs_confirmation": "SEEN" in response_types,
                "needs_response": bool(response_types), "response_types": response_types}

    def confirm_parent_letter(self, href: str) -> dict[str, Any]:
        url = self._resource_url(href, (LETTER_PREFIX,))
        soup = self._get_page(href)
        body = self._letter_body(soup).get_text(" ", strip=True)
        buttons = self._response_buttons(soup)
        if not buttons:
            raise IServError("This letter does not require confirmation (no confirmation button found)")
        if len(buttons) != 1 or buttons[0]["confirmation-type"] != "SEEN":
            raise IServError("This letter requires a different response; only a read acknowledgement is supported")
        button = buttons[0]
        form = button.find_parent("form")
        if form.get("method", "get").lower() != "post":
            raise IServError("IServ confirmation form method was not recognized")
        data = {i["name"]: i.get("value", "") for i in form.select("input[type=hidden][name]")}
        if not data.get("form[_token]"):
            raise IServError("IServ confirmation form is missing its CSRF token")
        data[button["name"]] = button.get("value", "")
        action = self._safe_url(form.get("action") or url, base=url)
        if urlsplit(action).path != urlsplit(url).path:
            raise IServError("IServ confirmation form targets a different letter")
        try:
            posted = self._post_page(action, data=data)
            if posted.select_one(".alert-danger, .invalid-feedback, .form-error-message"):
                raise IServError("IServ rejected the confirmation")
            # Verify persisted state with a fresh GET. Never replay an ambiguous POST.
            verified = self._get_page(href)
            if (self._letter_body(verified).get_text(" ", strip=True) != body
                    or self._response_buttons(verified)):
                raise IServError("Confirmation state did not change as expected")
        except IServError:
            raise IServError(
                "Could not verify the read acknowledgement. It may have been recorded; "
                "check the letter in IServ before trying again. No submission was repeated."
            ) from None
        return {"confirmed": True, "href": href}

    def download_attachment(self, attachment_href: str) -> dict[str, Any]:
        url = self._resource_url(attachment_href, ATTACHMENT_PREFIXES)
        response = self._get_response(url, maximum=MAX_ATTACHMENT_BYTES)
        mime_type = response.headers.get("Content-Type", "application/octet-stream").split(";")[0].strip()
        if mime_type.lower() in {"text/html", "application/xhtml+xml"}:
            raise IServError("IServ returned an HTML page instead of an attachment")
        header = Message()
        header["Content-Disposition"] = response.headers.get("Content-Disposition", "")
        filename = header.get_filename() or unquote(urlsplit(url).path.rsplit("/", 1)[-1])
        filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
        filename = "".join(c for c in filename if c.isprintable()).strip(" .")[:200] or "attachment"
        return {"data": response.content, "filename": filename, "mime_type": mime_type,
                "size_bytes": len(response.content)}

    def parse_pdf_attachment(self, attachment_href: str) -> dict[str, Any]:
        attachment = self.download_attachment(attachment_href)
        data = attachment.pop("data")
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise IServError("PDF is too large")
        if not data.startswith(b"%PDF-"):
            raise IServError("Attachment is not a PDF")
        # A small compressed PDF can consume large amounts of RAM/CPU. Run the
        # parser in a killable process; output/page limits alone are insufficient.
        try:
            result = subprocess.run(
                [sys.executable, str(Path(__file__).with_name("pdf_worker.py"))],
                input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=min(PDF_TIMEOUT_SECONDS, self._remaining_time()), check=False,
            )
        except subprocess.TimeoutExpired:
            raise IServError("PDF parsing exceeded its time limit") from None
        if result.returncode:
            raise IServError("PDF could not be parsed within the resource limits")
        try:
            parsed = json.loads(result.stdout)
        except (ValueError, UnicodeDecodeError):
            raise IServError("PDF parser returned an invalid result") from None
        if "error" in parsed:
            raise IServError(parsed["error"])
        return {**attachment, **parsed}

    def get_notifications(self, limit: int = 20) -> dict[str, Any]:
        validate_integer(limit, "limit", 1, 100)
        notifications, seen = [], set()
        for soup in self._list_pages("/iserv/notification/all"):
            items = soup.select("li.notification-item[data-id]")
            if not items:
                # An empty inbox is valid, but only accept a page carrying the
                # module's heading. A login/error/redesigned page must fail loudly.
                heading = " ".join(h.get_text(" ", strip=True).lower()
                                    for h in soup.select("title, h1, h2, .page-title"))
                if any(word in heading for word in ("benachrichtigung", "notification")):
                    return {"notifications": [], "total": 0, "limit": limit,
                            "returned": 0, "has_more": False}
                raise IServError("IServ notification markup was not recognized")
            for item in items:
                title_el = item.select_one(".notification-title a")
                date_el = item.select_one("time[data-date]")
                if title_el is None or not title_el.get_text(strip=True) or date_el is None:
                    raise IServError("IServ notification markup was not recognized")
                date_iso = date_el["data-date"]
                try:
                    parsed_date = datetime.fromisoformat(date_iso.replace("Z", "+00:00"))
                    if parsed_date.tzinfo is None:
                        raise ValueError
                except ValueError:
                    raise IServError("IServ notification date was not recognized") from None
                message_el = item.select_one(".notification-message")
                href = title_el.get("href", "")
                self._safe_url(href)
                if item["data-id"] in seen:
                    continue
                seen.add(item["data-id"])
                notifications.append({"id": item["data-id"], "title": title_el.get_text(strip=True),
                                      "message": message_el.get_text(strip=True) if message_el else "",
                                      "date": date_el.get_text(strip=True), "date_iso": date_iso,
                                      "href": href, "read": not item.has_attr("data-unread"),
                                      "type": item.get("data-type"), "_sort": parsed_date.timestamp()})
        notifications.sort(key=lambda item: item.pop("_sort"), reverse=True)
        return {"notifications": notifications[:limit], "total": len(notifications), "limit": limit,
                "returned": min(limit, len(notifications)), "has_more": len(notifications) > limit}
