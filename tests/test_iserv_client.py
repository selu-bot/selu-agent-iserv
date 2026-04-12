"""Unit tests for IServ client — HTML parsing, session logic, no live calls."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "capabilities", "iserv", "container"))

from iserv_client import (
    AuthenticationError,
    IServClient,
    IServError,
    SESSION_MAX_AGE_SECONDS,
)


# ---------------------------------------------------------------------------
# HTML fixtures — matching real IServ HTML structure (2026)
# ---------------------------------------------------------------------------

PARENT_LETTER_LIST_HTML = """
<html><body>
<table><tbody>
<tr class="unread">
  <td class="iserv-admin-list-field"><a href="/iserv/parentletter/parent/show/uuid-42/user-1">Schulausflug 5a</a></td>
  <td class="iserv-admin-list-field"><span class="badge text-bg-default text-normal p-1">Mia Thewes</span></td>
  <td class="iserv-admin-list-field">Frau Mueller</td>
  <td class="iserv-admin-list-field"><div class="parentletter-additional-authors"></div></td>
  <td class="iserv-admin-list-field"><span class="custom-badge">Klasse 02d</span></td>
  <td class="iserv-admin-list-field" data-sort="  1712900000">12.04.2026 10:00</td>
</tr>
<tr>
  <td class="iserv-admin-list-field"><a href="/iserv/parentletter/parent/show/uuid-41/user-1">Elternsprechtag</a></td>
  <td class="iserv-admin-list-field"><span class="badge text-bg-default text-normal p-1">Mia Thewes</span></td>
  <td class="iserv-admin-list-field">Herr Schmidt</td>
  <td class="iserv-admin-list-field"><div class="parentletter-additional-authors"></div></td>
  <td class="iserv-admin-list-field"><span class="custom-badge">Klasse 02d</span></td>
  <td class="iserv-admin-list-field" data-sort="  1712800000">11.04.2026 09:00</td>
</tr>
<tr class="unread">
  <td class="iserv-admin-list-field"><a href="/iserv/parentletter/parent/show/uuid-40/user-1">Sportfest</a></td>
  <td class="iserv-admin-list-field"><span class="badge text-bg-default text-normal p-1">Mia Thewes</span></td>
  <td class="iserv-admin-list-field">Frau Weber</td>
  <td class="iserv-admin-list-field"><div class="parentletter-additional-authors"></div></td>
  <td class="iserv-admin-list-field"><span class="custom-badge">Jahrgang 02</span></td>
  <td class="iserv-admin-list-field" data-sort="  1712700000">10.04.2026 08:00</td>
</tr>
</tbody></table>
</body></html>
"""

PARENT_LETTER_DETAIL_WITH_CONFIRM_HTML = """
<html><body>
<div class="parent-letter-body top-border translatable-content">
  <p>Liebe Eltern,</p>
  <p>am 20.04. findet der Schulausflug statt.</p>
</div>
<a href="/iserv/parentletter/attachment/abc-123">Elternbrief.pdf</a>
<a href="/iserv/file/download/def-456/info.docx">info.docx</a>
<form action="" class="form-horizontal" method="post" name="form" role="form">
<div class="top-border">
<button class="btn-success btn btn-primary" confirmation-type="SEEN" id="form_submit" name="form[submit]" type="submit"><span class="glyphicon glyphicon-check"></span> Gelesen</button>
<a class="btn btn-default" href="/iserv/parentletter/parent/index"><span class="fal fa-xmark"></span>Abbrechen</a>
</div>
<input class="form-control" id="form__token" name="form[_token]" type="hidden" value="test-token-789"/>
</form>
</body></html>
"""

PARENT_LETTER_DETAIL_NO_CONFIRM_HTML = """
<html><body>
<div class="parent-letter-body top-border translatable-content">
  <p>Keine Bestaetigung noetig.</p>
</div>
<form action="" class="form-horizontal" method="post" name="form" role="form">
<h3 class="mt-0 top-border">Antwort</h3>
<input class="form-control" id="form__token" name="form[_token]" type="hidden" value="token-xyz"/>
</form>
</body></html>
"""

NOTIFICATION_HTML = """
<html><body>
<ul>
<li class="list-group-item notification-item" data-id="100" data-unread="">
  <div class="media"><div class="media-body">
    <span class="notification-message">Neuer Elternbrief betreffend: Mia</span>
    <p class="notification-title"><a href="/iserv/notification/goto/100">Ostergruesse</a></p>
  </div></div>
  <time data-date="2026-04-12T08:30:00+02:00">12.04.2026 08:30</time>
</li>
<li class="list-group-item notification-item" data-id="99">
  <div class="media"><div class="media-body">
    <span class="notification-message">Vertretungsplan</span>
    <p class="notification-title"><a href="/iserv/notification/goto/99">Plan aktualisiert</a></p>
  </div></div>
  <time data-date="2026-04-11T14:00:00+02:00">11.04.2026 14:00</time>
</li>
</ul>
</body></html>
"""


# ---------------------------------------------------------------------------
# Parsing tests
# ---------------------------------------------------------------------------

class TestParseParentLetterRow:
    def test_parse_letter_list(self):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(PARENT_LETTER_LIST_HTML, "lxml")
        rows = soup.select("tbody tr")

        letters = []
        for row in rows:
            letter = IServClient._parse_parent_letter_row(row)
            if letter:
                letters.append(letter)

        assert len(letters) == 3

        assert letters[0]["title"] == "Schulausflug 5a"
        assert letters[0]["href"] == "/iserv/parentletter/parent/show/uuid-42/user-1"
        assert letters[0]["date"] == "12.04.2026 10:00"
        assert letters[0]["date_sort"] == 1712900000
        assert letters[0]["read"] is False
        assert letters[0]["child"] == "Mia Thewes"
        assert letters[0]["sender"] == "Frau Mueller"

        assert letters[1]["title"] == "Elternsprechtag"
        assert letters[1]["read"] is True

        assert letters[2]["title"] == "Sportfest"
        assert letters[2]["read"] is False

    def test_parse_empty_row(self):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup("<tr><td>no class</td></tr>", "lxml")
        row = soup.select_one("tr")
        assert IServClient._parse_parent_letter_row(row) is None


class TestParseParentLetterDetail:
    def test_parse_detail_with_attachments_and_confirmation(self):
        client = IServClient()
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(PARENT_LETTER_DETAIL_WITH_CONFIRM_HTML, "lxml")
        client._get_page = MagicMock(return_value=soup)

        result = client.get_parent_letter_content("/iserv/parentletter/parent/show/42")

        assert "Schulausflug" in result["body_text"]
        assert result["needs_confirmation"] is True
        assert len(result["attachments"]) == 2
        assert result["attachments"][0]["filename"] == "Elternbrief.pdf"
        assert "/parentletter/attachment/" in result["attachments"][0]["href"]
        assert result["attachments"][1]["filename"] == "info.docx"

    def test_parse_detail_no_confirmation(self):
        client = IServClient()
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(PARENT_LETTER_DETAIL_NO_CONFIRM_HTML, "lxml")
        client._get_page = MagicMock(return_value=soup)

        result = client.get_parent_letter_content("/iserv/parentletter/parent/show/99")

        assert result["needs_confirmation"] is False
        assert result["attachments"] == []
        assert "Keine Bestaetigung" in result["body_text"]


class TestParseNotifications:
    def test_parse_notifications(self):
        client = IServClient()
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(NOTIFICATION_HTML, "lxml")
        client._get_page = MagicMock(return_value=soup)

        result = client.get_notifications(limit=10)

        assert len(result["notifications"]) == 2
        n0 = result["notifications"][0]
        assert n0["title"] == "Ostergruesse"
        assert n0["read"] is False
        assert n0["message"] == "Neuer Elternbrief betreffend: Mia"
        assert n0["date_iso"] == "2026-04-12T08:30:00+02:00"

        n1 = result["notifications"][1]
        assert n1["title"] == "Plan aktualisiert"
        assert n1["read"] is True


# ---------------------------------------------------------------------------
# Session / auth tests
# ---------------------------------------------------------------------------

class TestSessionManagement:
    def test_not_authenticated_initially(self):
        client = IServClient()
        assert client.is_authenticated() is False

    def test_authenticated_after_login_timestamp(self):
        client = IServClient()
        client._authenticated_at = time.time()
        assert client.is_authenticated() is True

    def test_session_expires_after_max_age(self):
        client = IServClient()
        client._authenticated_at = time.time() - SESSION_MAX_AGE_SECONDS - 1
        assert client.is_authenticated() is False

    def test_credential_change_clears_session(self):
        client = IServClient()
        client.set_credentials("user1", "pass1")
        client._authenticated_at = time.time()
        client.set_credentials("user2", "pass2")
        assert client._authenticated_at is None

    def test_same_credentials_keep_session(self):
        client = IServClient()
        client.set_credentials("user1", "pass1")
        client._authenticated_at = time.time()
        client.set_credentials("user1", "pass1")
        assert client._authenticated_at is not None

    def test_login_requires_credentials(self):
        client = IServClient()
        with pytest.raises(AuthenticationError, match="Missing"):
            client.login()


class TestConfirmParentLetter:
    def test_confirm_via_form(self):
        client = IServClient()
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(PARENT_LETTER_DETAIL_WITH_CONFIRM_HTML, "lxml")
        post_soup = BeautifulSoup("<html><body>OK</body></html>", "lxml")

        client._get_page = MagicMock(return_value=soup)
        client._post_page = MagicMock(return_value=post_soup)

        result = client.confirm_parent_letter("/iserv/parentletter/parent/show/42")

        assert result["confirmed"] is True
        client._post_page.assert_called_once()
        call_args = client._post_page.call_args
        # Form data should include token and submit button
        form_data = call_args[1]["data"] if "data" in call_args[1] else call_args[0][1]
        assert "form[_token]" in form_data
        assert "form[submit]" in form_data

    def test_confirm_raises_when_no_button(self):
        client = IServClient()
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(PARENT_LETTER_DETAIL_NO_CONFIRM_HTML, "lxml")
        client._get_page = MagicMock(return_value=soup)

        with pytest.raises(IServError, match="does not require confirmation"):
            client.confirm_parent_letter("/iserv/parentletter/parent/show/99")


class TestGetParentLettersFiltering:
    def test_unread_only_filter(self):
        client = IServClient()
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(PARENT_LETTER_LIST_HTML, "lxml")
        client._get_page = MagicMock(return_value=soup)

        result = client.get_parent_letters(unread_only=True)

        assert result["total"] == 2
        for letter in result["letters"]:
            assert letter["read"] is False

    def test_pagination(self):
        client = IServClient()
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(PARENT_LETTER_LIST_HTML, "lxml")
        client._get_page = MagicMock(return_value=soup)

        result = client.get_parent_letters(limit=1, offset=0)
        assert result["returned"] == 1
        assert result["total"] == 3
        assert result["letters"][0]["title"] == "Schulausflug 5a"

        result2 = client.get_parent_letters(limit=1, offset=1)
        assert result2["returned"] == 1
        assert result2["letters"][0]["title"] == "Elternsprechtag"

    def test_offset_beyond_total(self):
        client = IServClient()
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(PARENT_LETTER_LIST_HTML, "lxml")
        client._get_page = MagicMock(return_value=soup)

        result = client.get_parent_letters(offset=100)
        assert result["returned"] == 0
        assert result["letters"] == []
