from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL = "https://mags-greven.de"
SESSION_MAX_AGE_SECONDS = 15 * 60


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

    def set_credentials(self, username: str, password: str) -> None:
        creds = LoginCredentials(username=username, password=password)
        if self._credentials != creds:
            self.session.cookies.clear()
            self._authenticated_at = None
            self._credentials = creds

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
        logger.info("Authenticating to IServ as %s", self._credentials.username)

        # GET the login page to pick up session cookies and check for CSRF token
        resp = self.session.get(f"{BASE_URL}/iserv/auth/login", timeout=20)
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
            f"{BASE_URL}/iserv/auth/login",
            data=form_data,
            timeout=20,
            allow_redirects=True,
        )
        login_resp.raise_for_status()

        if "/iserv/auth/login" in login_resp.url:
            raise AuthenticationError("Login failed — check username and password")

        self._authenticated_at = time.time()
        logger.info("Authentication successful")

    def _ensure_auth(self) -> None:
        if not self.is_authenticated():
            self.login()

    def _get_page(self, path: str, retry: bool = True) -> BeautifulSoup:
        self._ensure_auth()
        url = path if path.startswith("http") else f"{BASE_URL}{path}"
        try:
            resp = self.session.get(url, timeout=20)
            resp.raise_for_status()
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
        url = path if path.startswith("http") else f"{BASE_URL}{path}"
        try:
            resp = self.session.post(url, data=data, timeout=20, allow_redirects=True)
            resp.raise_for_status()
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
        url = (
            attachment_href
            if attachment_href.startswith("http")
            else f"{BASE_URL}{attachment_href}"
        )
        resp = self.session.get(url, timeout=30, stream=True)
        resp.raise_for_status()

        content_disp = resp.headers.get("Content-Disposition", "")
        filename_match = re.search(r'filename[*]?=["\']?([^"\';\n]+)', content_disp)
        filename = filename_match.group(1).strip() if filename_match else url.split("/")[-1]

        mime_type = resp.headers.get("Content-Type", "application/octet-stream").split(";")[0].strip()
        data = resp.content

        return {
            "data": data,
            "filename": filename,
            "mime_type": mime_type,
            "size_bytes": len(data),
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
