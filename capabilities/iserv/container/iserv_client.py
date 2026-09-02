from __future__ import annotations

import logging
import re
import time
from html import unescape
from io import BytesIO
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse
from typing import Any

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader
from pypdf.errors import PdfReadError

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://mags-greven.de"
SESSION_MAX_AGE_SECONDS = 15 * 60
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
MAX_PDF_PAGES = 50
MAX_PDF_TEXT_CHARS = 100_000
DOWNLOAD_CHUNK_SIZE = 64 * 1024


class IServError(RuntimeError):
    pass


class AuthenticationError(IServError):
    pass


@dataclass
class LoginCredentials:
    username: str
    password: str


class IServClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "Selu IServ Capability/1.0"
        self._credentials: LoginCredentials | None = None
        self._authenticated_at: float | None = None
        self._base_url = DEFAULT_BASE_URL

    def set_credentials(self, username: str, password: str) -> None:
        creds = LoginCredentials(username=username, password=password)
        if self._credentials != creds:
            self.session.cookies.clear()
            self._authenticated_at = None
            self._credentials = creds

    def set_base_url(self, base_url: str | None) -> None:
        normalized_url = self._normalize_base_url(base_url)
        if normalized_url != self._base_url:
            self.session.cookies.clear()
            self._authenticated_at = None
            self._base_url = normalized_url

    @staticmethod
    def _normalize_base_url(base_url: str | None) -> str:
        raw = (base_url or DEFAULT_BASE_URL).strip()
        if not raw:
            raw = DEFAULT_BASE_URL
        if "://" not in raw:
            raw = f"https://{raw}"
        parsed = urlparse(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise IServError(
                "Invalid ISERV_BASE_URL. Use a full host like https://schule.example.de"
            )
        return raw.rstrip("/")

    def is_authenticated(self) -> bool:
        if self._authenticated_at is None:
            return False
        age = time.time() - self._authenticated_at
        if age >= SESSION_MAX_AGE_SECONDS:
            logger.info("Session age %.0fs exceeds max %ds, clearing", age, SESSION_MAX_AGE_SECONDS)
            self.session.cookies.clear()
            self._authenticated_at = None
            return False
        return True

    def login(self) -> None:
        if not self._credentials:
            raise AuthenticationError("Missing IServ credentials")

        self.session.cookies.clear()
        logger.info("Authenticating to configured IServ account")

        # GET the login page to pick up session cookies and check for CSRF token
        resp = self.session.get(f"{self._base_url}/iserv/auth/login", timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        form_data: dict[str, str] = {
            "_username": self._credentials.username,
            "_password": self._credentials.password,
        }

        # Include CSRF token if present (some IServ versions use it, others don't)
        csrf_input = soup.select_one("input[name=_csrf_token]")
        if csrf_input:
            form_data["_csrf_token"] = csrf_input["value"]

        # Include any other hidden fields the form may carry
        login_form = soup.select_one("form")
        if login_form:
            for hidden in login_form.select("input[type=hidden]"):
                name = hidden.get("name")
                if name and name not in form_data:
                    form_data[name] = hidden.get("value", "")

        login_resp = self.session.post(
            f"{self._base_url}/iserv/auth/login",
            data=form_data,
            timeout=20,
            allow_redirects=True,
        )
        login_resp.raise_for_status()
        login_resp = self._follow_iserv_auth_redirect(login_resp)

        if "/iserv/auth/login" in login_resp.url:
            raise AuthenticationError("Login failed — check username and password")

        self._authenticated_at = time.time()
        logger.info("Authentication successful")

    def _follow_iserv_auth_redirect(
        self,
        response: requests.Response,
        *,
        stream: bool = False,
    ) -> requests.Response:
        """Follow IServ's HTML meta-refresh authentication bridge.

        The IServ app gateway responds with HTTP 200 at /iserv/auth/auth and a
        meta refresh. Browsers follow it automatically; requests does not.
        """
        for _ in range(3):
            if urlparse(response.url).path != "/iserv/auth/auth":
                return response

            soup = BeautifulSoup(response.text, "lxml")
            refresh = soup.select_one('meta[http-equiv="refresh" i]')
            content = refresh.get("content", "") if refresh else ""
            match = re.search(r"(?:^|;)\s*url\s*=\s*(.+)\s*$", content, re.I)
            if not match:
                raise AuthenticationError(
                    "IServ authentication redirect did not contain a target URL"
                )

            target = unescape(match.group(1).strip().strip("\"'"))
            target_url = urljoin(response.url, target)
            parsed_target = urlparse(target_url)
            parsed_base = urlparse(self._base_url)
            if (
                parsed_target.scheme != parsed_base.scheme
                or parsed_target.netloc != parsed_base.netloc
            ):
                raise AuthenticationError(
                    "IServ authentication redirect targeted a different host"
                )

            response.close()
            response = self.session.get(target_url, timeout=20, stream=stream)
            response.raise_for_status()

        raise AuthenticationError("Too many IServ authentication redirects")

    def _ensure_auth(self) -> None:
        if not self.is_authenticated():
            self.login()

    def _get_page(self, path: str, retry: bool = True) -> BeautifulSoup:
        self._ensure_auth()
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        try:
            resp = self.session.get(url, timeout=20)
            resp.raise_for_status()
            resp = self._follow_iserv_auth_redirect(resp)
            if "/iserv/auth/login" in resp.url:
                raise AuthenticationError("Session expired")
            return BeautifulSoup(resp.text, "lxml")
        except AuthenticationError:
            if retry:
                logger.info("Session expired, re-authenticating")
                self._authenticated_at = None
                self.login()
                return self._get_page(path, retry=False)
            raise

    def _post_page(self, path: str, data: dict, retry: bool = True) -> BeautifulSoup:
        self._ensure_auth()
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        try:
            resp = self.session.post(url, data=data, timeout=20, allow_redirects=True)
            resp.raise_for_status()
            resp = self._follow_iserv_auth_redirect(resp)
            if "/iserv/auth/login" in resp.url:
                raise AuthenticationError("Session expired")
            return BeautifulSoup(resp.text, "lxml")
        except AuthenticationError:
            if retry:
                logger.info("Session expired, re-authenticating")
                self._authenticated_at = None
                self.login()
                return self._post_page(path, data, retry=False)
            raise

    def get_parent_letters(
        self,
        limit: int = 20,
        offset: int = 0,
        unread_only: bool = False,
    ) -> dict[str, Any]:
        soup = self._get_page("/iserv/parentletter/parent/index")
        rows = soup.select("tbody tr")

        letters: list[dict[str, Any]] = []
        for row in rows:
            letter = self._parse_parent_letter_row(row)
            if letter is None:
                continue
            if unread_only and letter["read"]:
                continue
            letters.append(letter)

        letters.sort(key=lambda x: x.get("date_sort", 0), reverse=True)
        end = min(offset + limit, len(letters))
        page = letters[offset:end] if offset < len(letters) else []

        return {
            "letters": page,
            "total": len(letters),
            "offset": offset,
            "limit": limit,
            "returned": len(page),
        }

    @staticmethod
    def _parse_parent_letter_row(row) -> dict[str, Any] | None:
        # IServ table layout (as of 2026):
        #   0: Title (link)   1: Child   2: Sender   3: Additional senders
        #   4: Recipients     5: Created (date with data-sort)
        cells = row.select("td.iserv-admin-list-field")
        if not cells:
            return None

        link = cells[0].select_one("a") if cells else None
        if link is None:
            return None

        title = link.get_text(strip=True)
        href = link.get("href", "")

        # Child name from cell 1
        child = cells[1].get_text(strip=True) if len(cells) > 1 else ""
        # Sender from cell 2
        sender = cells[2].get_text(strip=True) if len(cells) > 2 else ""

        # Date from cell 5 (the "Erstellt" column)
        date_str = ""
        date_sort = 0
        if len(cells) >= 6:
            date_cell = cells[5]
            date_str = date_cell.get_text(strip=True)
            sort_val = date_cell.get("data-sort", "0")
            try:
                date_sort = int(sort_val.strip())
            except (ValueError, AttributeError):
                date_sort = 0

        is_read = not row.has_attr("class") or "unread" not in row.get("class", [])

        return {
            "title": title,
            "href": href,
            "date": date_str,
            "date_sort": date_sort,
            "read": is_read,
            "child": child,
            "sender": sender,
        }

    def get_parent_letter_content(self, href: str) -> dict[str, Any]:
        soup = self._get_page(href)

        body_div = soup.select_one("div.parent-letter-body")
        body_html = body_div.decode_contents() if body_div else ""
        body_text = body_div.get_text(separator="\n", strip=True) if body_div else ""

        attachments: list[dict[str, Any]] = []
        for link in soup.select("a[href*='/iserv/']"):
            link_href = link.get("href", "")
            if any(
                kw in link_href
                for kw in (
                    "/parentletter/attachment/",
                    "/file/",
                    "/download/",
                    "/attachment/",
                )
            ):
                filename = link.get_text(strip=True) or link_href.split("/")[-1]
                attachments.append({
                    "filename": filename,
                    "href": link_href,
                })

        # Confirmation detection: look for submit button with confirmation-type
        # attribute inside the form[name=form]. If present, letter needs confirmation.
        needs_confirmation = False
        confirm_btn = soup.select_one(
            'form[name=form] button[name="form[submit]"][confirmation-type]'
        )
        if confirm_btn:
            needs_confirmation = True

        return {
            "body_html": body_html,
            "body_text": body_text,
            "attachments": attachments,
            "needs_confirmation": needs_confirmation,
        }

    def confirm_parent_letter(self, href: str) -> dict[str, Any]:
        soup = self._get_page(href)

        # IServ confirmation pattern: form[name=form] contains a submit button
        # with name="form[submit]" and confirmation-type="SEEN", plus a hidden
        # form[_token] field. Submitting the form with both fields confirms.
        form = soup.select_one("form[name=form]")
        if not form:
            raise IServError("No form found on the parent letter page")

        confirm_btn = form.select_one(
            'button[name="form[submit]"][confirmation-type]'
        )
        if not confirm_btn:
            raise IServError(
                "This letter does not require confirmation "
                "(no confirmation button found)"
            )

        form_data: dict[str, str] = {}
        # Include all hidden fields (notably form[_token])
        for inp in form.select("input[type=hidden]"):
            name = inp.get("name")
            if name:
                form_data[name] = inp.get("value", "")

        # Include the submit button value
        btn_name = confirm_btn.get("name", "form[submit]")
        btn_value = confirm_btn.get("value", "")
        form_data[btn_name] = btn_value

        # POST to the form action (empty string = same URL)
        action = form.get("action") or href
        if not action.startswith("http") and not action.startswith("/"):
            action = href

        self._post_page(action, data=form_data)
        return {"confirmed": True, "href": href}

    def download_attachment(self, attachment_href: str) -> dict[str, Any]:
        self._ensure_auth()
        url = urljoin(f"{self._base_url}/", attachment_href)
        if urlparse(url).netloc != urlparse(self._base_url).netloc:
            raise IServError("Attachment URL must use the configured IServ host")

        resp = self.session.get(url, timeout=30, stream=True)
        resp.raise_for_status()
        resp = self._follow_iserv_auth_redirect(resp, stream=True)
        if "/iserv/auth/login" in resp.url:
            raise AuthenticationError("Session expired")

        content_length = resp.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > MAX_ATTACHMENT_BYTES:
                    raise IServError(
                        f"Attachment is too large ({content_length} bytes, max {MAX_ATTACHMENT_BYTES})"
                    )
            except ValueError:
                pass

        content_disp = resp.headers.get("Content-Disposition", "")
        filename_match = re.search(r'filename[*]?=["\']?([^"\';\n]+)', content_disp)
        filename = filename_match.group(1).strip() if filename_match else url.split("/")[-1]

        mime_type = resp.headers.get("Content-Type", "application/octet-stream").split(";")[0].strip()
        chunks: list[bytes] = []
        size_bytes = 0
        for chunk in resp.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
            if not chunk:
                continue
            size_bytes += len(chunk)
            if size_bytes > MAX_ATTACHMENT_BYTES:
                raise IServError(
                    f"Attachment is too large (max {MAX_ATTACHMENT_BYTES} bytes)"
                )
            chunks.append(chunk)
        data = b"".join(chunks)

        return {
            "data": data,
            "filename": filename,
            "mime_type": mime_type,
            "size_bytes": size_bytes,
        }

    def parse_pdf_attachment(self, attachment_href: str) -> dict[str, Any]:
        """Download and extract bounded text without relying on an agent skill."""
        attachment = self.download_attachment(attachment_href)
        data = attachment["data"]
        mime_type = attachment["mime_type"].lower()
        filename = attachment["filename"]

        if len(data) > MAX_ATTACHMENT_BYTES:
            raise IServError(
                f"PDF is too large ({len(data)} bytes, max {MAX_ATTACHMENT_BYTES})"
            )
        if not data.startswith(b"%PDF-"):
            raise IServError(
                f"Attachment is not a PDF (filename={filename!r}, mime_type={mime_type!r})"
            )

        try:
            reader = PdfReader(BytesIO(data), strict=False)
            if reader.is_encrypted:
                try:
                    unlocked = reader.decrypt("")
                except Exception as exc:
                    raise IServError("PDF is encrypted and cannot be parsed") from exc
                if not unlocked:
                    raise IServError("PDF is encrypted and cannot be parsed")

            total_pages = len(reader.pages)
            parsed_pages = min(total_pages, MAX_PDF_PAGES)
            page_texts: list[dict[str, Any]] = []
            remaining_chars = MAX_PDF_TEXT_CHARS

            for page_number, page in enumerate(reader.pages[:parsed_pages], start=1):
                if remaining_chars <= 0:
                    break
                text = (page.extract_text() or "").strip()
                if len(text) > remaining_chars:
                    text = text[:remaining_chars]
                remaining_chars -= len(text)
                page_texts.append({"page": page_number, "text": text})
        except IServError:
            raise
        except (PdfReadError, ValueError, TypeError, KeyError) as exc:
            raise IServError(f"Could not parse PDF: {exc}") from exc

        combined_text = "\n\n".join(
            f"[Page {page['page']}]\n{page['text']}"
            for page in page_texts
            if page["text"]
        )
        text_truncated = total_pages > len(page_texts) or remaining_chars <= 0

        return {
            "filename": filename,
            "mime_type": mime_type,
            "size_bytes": len(data),
            "page_count": total_pages,
            "parsed_pages": len(page_texts),
            "pages": page_texts,
            "text": combined_text,
            "text_truncated": text_truncated,
            "has_extractable_text": any(page["text"] for page in page_texts),
        }

    def get_notifications(self, limit: int = 20) -> dict[str, Any]:
        # Fetch all notifications (not just unread)
        soup = self._get_page("/iserv/notification/all")
        notifications: list[dict[str, Any]] = []

        for item in soup.select("li.notification-item[data-id]"):
            # Title from .notification-title link
            title_el = item.select_one(".notification-title a")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            if not title:
                continue

            # Message context (e.g., "Neuer Elternbrief betreffend: Mia")
            message_el = item.select_one(".notification-message")
            message = message_el.get_text(strip=True) if message_el else ""

            # Date from <time> element
            date_el = item.select_one("time")
            date_str = date_el.get_text(strip=True) if date_el else ""
            # Also extract ISO date from data-date attribute
            date_iso = date_el.get("data-date", "") if date_el else ""

            # Link to the actual item
            href = title_el.get("href", "")

            # Unread if data-unread attribute is present (empty string = unread)
            is_read = not item.has_attr("data-unread")

            notifications.append({
                "title": title,
                "message": message,
                "date": date_str,
                "date_iso": date_iso,
                "href": href,
                "read": is_read,
            })

            if len(notifications) >= limit:
                break

        return {
            "notifications": notifications,
            "total": len(notifications),
            "limit": limit,
        }
