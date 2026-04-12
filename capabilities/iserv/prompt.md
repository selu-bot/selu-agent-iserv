# IServ Capability — Tool Reference

## Tools

### `iserv__check_parent_letters`
Fetches the parent letter list. Returns an array of letters with `title`, `date`, `date_sort`, `read`, `needs_confirmation`, and `href`.

- Default: most recent 20 letters, sorted newest first
- Use `unread_only: true` to filter to unread letters only
- Use `offset` for pagination through older letters

### `iserv__get_parent_letter`
Reads the full content of one parent letter.

- Pass the `href` from the letter list result
- Returns `body_text` (cleaned content), `body_html` (raw HTML), `attachments` (list with `filename`, `href`, `size`), and `needs_confirmation` (boolean)
- If the letter has attachments, list them for the user and offer to download
- If `needs_confirmation` is true, inform the user

### `iserv__confirm_parent_letter`
Sends the read confirmation for a parent letter. Only call this after the user has explicitly approved.

- Pass the `href` of the letter
- Returns `confirmed: true` on success

### `iserv__download_attachment`
Downloads a file attachment and makes it available for the user.

- Pass `attachment_href` from the letter's attachments list
- Returns `artifact` with `capability_artifact_id`, `filename`, and `mime_type`
- The orchestrator will handle delivery to the user

### `iserv__check_notifications`
Fetches recent IServ notifications.

- Returns an array with `title`, `date`, `type`, and `read` for each notification
- Present in reverse chronological order

## Guidelines

- Always use `store_get("last_parentletter_check")` before checking letters to provide context on what's new
- After checking, update with `store_set("last_parentletter_check", current_datetime)`
- Never show raw hrefs or HTML to the user — present content in clean, readable format
- Confirm before any irreversible action (read confirmations)
