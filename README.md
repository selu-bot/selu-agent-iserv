# IServ School Assistant for selu

This repository contains a specialized [selu](https://selu.bot/) agent for working with IServ school accounts. Selu provides the agent runtime, credential handling, and user-facing conversation; this project supplies the agent instructions and the IServ capability that performs the website operations. See the [Selu documentation](https://docs.selu.bot/en/) for the runtime and deployment model.

The integration targets IServ **parent letters**. IServ exposes these in its parent-letter module rather than as ordinary email messages, so the capability keeps the user-facing term “parent letter” while using the IServ parent-letter endpoints and HTML forms internally.

## What the agent does

- Lists recent or unread parent letters, including the child, sender, date, subject, and a short preview.
- Opens one parent letter and returns clean text plus attachment metadata.
- Confirms that a parent letter was seen only after the user explicitly asks for that action. The supported IServ acknowledgement is `SEEN`.
- Downloads an attachment into a short-lived Selu artifact that can be opened or passed to another step.
- Extracts text from PDF attachments in a bounded worker process. This is text extraction, not OCR.
- Lists IServ notifications and normalizes their dates and types where the page provides them.

The agent does not silently acknowledge letters, expose raw HTML to the model, or treat a failed confirmation request as proof that the letter was acknowledged.

## How the integration works

The runtime and capability are separated so that Selu can apply its normal isolation and credential rules:

```text
Selu conversation
      |
      v
agent.yaml + agent.md + prompt.md
      |
      v
gRPC capability container
      |
      +--> IServClient --> IServ parent-letter and notification pages
      +--> PDF worker  --> bounded text extraction
      +--> ArtifactStore --> expiring attachment handles
```

### Login and requests

`IServClient` owns a session for one invocation. It first loads the IServ login page, preserves hidden form fields and CSRF values, submits the credentials, and requires an `IServSession` cookie before continuing. Expired sessions are re-authenticated once and the original request is retried only when it is safe to do so.

IServ is a web application, so the client consumes HTML pages and forms as well as HTTP responses. It follows only same-origin HTTPS redirects, rejects cross-host redirects and unexpected authentication bridges, validates the expected page structure, and applies response-size and timeout limits. A malformed page is an error; it is never converted into an empty inbox.

### Parent letters

Parent-letter list pages are paginated and fetched until the requested local page is complete or the server reports no more results. The capability returns structured fields such as `id`, `subject`, `sender`, `child`, `date`, `preview`, `body_text`, `attachments`, and `needs_response`. It deliberately omits the original `body_html` so markup cannot become an instruction channel for the agent.

The confirmation flow fetches the letter, extracts its CSRF token, submits the same-origin acknowledgement form, and then fetches the letter again to verify the resulting state. If the response is ambiguous, the operation fails safely and the user is told that the status must be checked in IServ.

### Attachments and PDFs

Attachment URLs must remain on the configured IServ origin and within the expected parent-letter file paths. Downloads are capped at 5 MiB, reject HTML masquerading as a file, and receive a sanitized filename. The resulting artifact expires automatically and is released when the caller is done.

PDF parsing runs in a separate one-shot process with page, output, memory, CPU, and timeout limits. The main capability process does not invoke an unrestricted PDF parser on untrusted input.

## Configuration

Selu injects the declared credentials when it starts the capability. No username or password is stored in this repository.

| Variable | Required | Description |
| --- | --- | --- |
| `USERNAME` | Yes | IServ account username. |
| `PASSWORD` | Yes | IServ account password. |
| `ISERV_BASE_URL` | No | IServ base URL; defaults to `https://mags-greven.de`. The host must also be present in the manifest network allowlist. |

The current manifest allowlists `mags-greven.de:443`. If the agent is used with another IServ installation, update the allowlist and the base URL together, and provide credentials for that installation.

The capability listens on gRPC port `50051` inside its container. Selu supplies the request lifecycle and invokes the six declared tools; the container is not intended to be an unauthenticated public HTTP service.

## Project layout

- [`agent.yaml`](agent.yaml) — Selu agent identity, routing, session, and capability declaration.
- [`agent.md`](agent.md) / [`agent.de.md`](agent.de.md) — agent behavior and confirmation policy.
- [`capabilities/iserv/manifest.yaml`](capabilities/iserv/manifest.yaml) — tool, credential, resource, and network declarations.
- [`capabilities/iserv/prompt.md`](capabilities/iserv/prompt.md) — tool contracts and safety guidance for the model.
- [`capabilities/iserv/container/iserv_client.py`](capabilities/iserv/container/iserv_client.py) — authenticated IServ HTML client.
- [`capabilities/iserv/container/server.py`](capabilities/iserv/container/server.py) — gRPC service, input validation, limits, and error handling.
- [`capabilities/iserv/container/pdf_worker.py`](capabilities/iserv/container/pdf_worker.py) — isolated PDF text extraction worker.
- [`capabilities/iserv/container/artifacts.py`](capabilities/iserv/container/artifacts.py) — bounded, expiring attachment storage.

For IServ background, see the official [IServ parent-letter documentation](https://doku.iserv.de/modules/parentletter/). For Selu installation and runtime behavior, use the [Selu documentation](https://docs.selu.bot/en/).

## Development and verification

Install the pinned development dependencies and run the checks from the repository root:

```bash
python -m pip install --require-hashes -r requirements-dev.txt
python -m pytest -q
ruff check .
```

The GitHub Actions workflow runs these checks and builds the capability image. To build the image locally when Docker is available:

```bash
docker build -t selu-cap-iserv:local capabilities/iserv/container
```

IServ is an external web application and may change its markup or authentication flow. The client is intentionally strict about those changes so an upstream change produces an actionable error instead of silently returning incorrect data.
