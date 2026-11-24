# CLAUDE.md - project memory for 3CX Bulk Tool

> This repository is **PUBLIC**. Never write real credentials, PBX host names, customer/company names,
> people's names, KeePass exports or personal data into any file here, into commits, or into release notes.
> Keep such details in the chat only.

## How to talk to the maintainer
- The maintainer is a junior network engineer. **Answer in Polish**, explain step by step as to a complete
  beginner, think ahead so they do not have to come back for missing pieces. Mistakes are fine, just fix them.
- **Everything in the product (GUI, CLI, messages, README) must be in English** (the company works in Ireland).
- Ask before building when the request is vague (they explicitly asked for that); otherwise act on sensible defaults.
- Report honestly what was tested and what was not.

## What the project is
A tool for **bulk changes in 3CX V20 phone systems through the Configuration API (XAPI)**, driven by CSV files.
About **150 PBX systems** (self-hosted) must be managed. Credentials of the systems come from a **KeePass CSV
export** that the app imports itself (name=Title, Client ID=Username, API key=Password, address=URL).

Delivered so far (all in `src/tcx_bulk/`, 56 tests, `pytest -q`; GUI tests need tkinter + `xvfb-run -a`):
- `client.py` XAPI client (OAuth2 `/connect/token` client_credentials, retries, `$top/$skip` paging, `$metadata`
  entity discovery, PATCH/POST/DELETE on `/xapi/v1/<Entity>(<Id>)`).
- `bulk.py` CSV plan (dry-run first, type coercion, unknown-column protection, create/delete behind flags),
  backup before every change, rollback.
- `systems.py` KeePass/KeePassXC CSV import (secrets never printed/saved). `multi.py` many systems: plan all in
  parallel, **pilot system first**, failures isolated per system, per-system backups, whole-run undo.
- `gui.py` Tk window with the sv-ttk theme (light/dark): tabs "KeePass systems" / "Single PBX", coloured preview
  table, Copy log, **Check for updates** button. `cli.py` = same features in the terminal (`tcx-bulk ...`).
- `updater.py` self-update from GitHub Releases (SHA-256 verified; Windows: rename running exe to `.old`).
- Tests use a simulated PBX (`tests/fakepbx.py`). Screenshots were taken with Xvfb + Pillow ImageGrab.

## Releasing (automatic)
Bump `__version__` in `src/tcx_bulk/__init__.py` **and** `version` in `pyproject.toml`, commit, push to a branch
listed in `RELEASE_BRANCHES` of `.github/workflows/build.yml`. CI runs tests, builds Windows+Linux with
PyInstaller (`--collect-data sv_ttk` for the GUI), creates the GitHub Release with `SHA256SUMS.txt` and commit
messages as notes. Do not bump the version for trivial changes (every release reaches all users' update button).
Verified on a real Windows machine: v0.2.0 -> v0.2.1 self-update works. Current version: see `__version__`.
Session limits seen: from the cloud session only the dev branch can be pushed; pushing **tags returns 403**.

## THE BLOCKER (current state, important)
The only test PBX available is **3CX V20 Update 10 (Build 1801), PRO Edition, 4 simultaneous calls**.
In its admin console, **Integrations > API ("Service Principals") says "This feature is available in AI Edition
only" and the list is empty**, so **no API client (Client ID / API key) can exist** there. Login attempts end with
`HTTP 400 invalid_client` (the address is right, the PBX answers; there is simply no client). Public docs/forum
disagree on the needed licence (older: Enterprise; current docs: "8SC AI licence or higher"; forum: AI Edition =
renamed Enterprise). **Consequence: this tool has still never talked to a real 3CX API.** Everything is verified
only against the simulator (request format follows the official spec: https://www.3cx.com/docs/configuration-rest-api-endpoints/).
Other known gotchas from the forum: token role can come back as `user` (then `/Users` gives 403); the Users list
may omit System Owner accounts; some fields (e.g. `CurrentProfileName`) cannot be PATCHed.
Licensing matters per PBX: each of the ~150 systems would need the edition that includes Service Principals.

## Open question being investigated (next session, with access to the maintainer's computer)
The company has an **internal tool written by a colleague that traces calls on 3CX systems** (it is running on
the maintainer's PC). The maintainer asked: **find out how it works and whether the same method can be used for
our bulk-change tool.** Plan for that session:
1. Request the program's folder via `list_computer_folders` + `request_computer_folder` (the maintainer adds the
   folder in the Claude desktop app: Settings > Claude Code > Remote Control). **Read only. Do NOT run the
   program, do not copy or print any credentials/keys found in config files, do not change anything.**
2. Work out the mechanism: language, config files, which interface it uses (XAPI / Call Control API with a service
   principal, 3CX CDR output, logs over SSH, direct PostgreSQL, SIP/websocket, ...). Hints: remote port 443/5001 =
   HTTPS API, 22 = SSH, 5432 = PostgreSQL, 5060/5061 = SIP. The maintainer can also read it in Resource Monitor
   (resmon > Network > TCP connections) and can ask the author.
3. Judge transferability: a **read-only** method (logs/CDR) does not allow configuration changes; an API-based
   one needs the same licence; SSH/DB changes are **unsupported by 3CX and risky** - analysis and a written
   proposal first, no changes on production without explicit approval.
4. **Do not help circumvent 3CX licensing** (cracks, patching, enabling the API on editions that do not include it).
5. Treat the program as untrusted until read; never feed it real PBX credentials.

## Other options discussed, not decided
- Check a bigger **production** PBX: Integrations > API there - if "Add" works without the AI Edition warning,
  the tool can get its first real test (use a test user, change only `FirstName`, preview first, undo after).
- Ask 3CX/partner which edition gives the API and its cost for ~150 systems (maybe a temporary test licence).
- Built-in 3CX CSV import/export of extensions for simple bulk edits (no API needed).
- SSH-based approach: only analysis for now (see above).
- Possible later features: profiles per customer, more entity-specific helpers, a "Copy log" is already there.

## First real-world test checklist (once a PBX with API access exists)
1. Create the API client (Integrations > API > Add; enable Configuration API/XAPI, admin role), copy the key once.
2. GUI > "Single PBX" > address (host only, e.g. `pbx.example.ie`, optional `:port`), Client ID, key > Connect.
3. Export the current state, keep the real field names, make a 1-row CSV (`Number`,`FirstName`), preview, apply,
   verify in the admin console, then Undo. Report any HTTP errors with the Copy log button.
