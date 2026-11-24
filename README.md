# 3CX Bulk Tool

A tool for **bulk changes in a 3CX (V20) phone system through its API**, driven by a CSV file (e.g. from Excel).
It comes in two flavours:

| Version | Who it is for | File |
|---|---|---|
| **Desktop window (GUI)** | everyone in the company, no terminal needed | `tcx-bulk-gui.exe` (Windows) / `tcx-bulk-gui` (Linux) |
| **Terminal (CLI)** | administrators, scripts, automation | `tcx-bulk.exe` / `tcx-bulk` |

The downloadable files are built automatically on GitHub: **Actions tab → latest "Build executables" run → Artifacts** (separate downloads for Windows and Linux).

---

## Safety rules (read once)

1. **Always preview before changing anything.** Loading a CSV changes nothing. It only shows a "was → will be" list. Changes happen only after you click **APPLY CHANGES** (CLI: the `--execute` flag).
2. **A backup is saved before every change.** The old values are written to `backup.json` (GUI: the `tcx-bulk-runs` folder in your home directory; CLI: the `tcx-runs` folder).
3. **Undo.** Changes can be undone from the backup (the *Undo changes from a backup* button, or `tcx-bulk rollback`). This covers "update" changes. Deleted records are not recreated automatically, but their data stays in `backup.json`.
4. **Creating and deleting are blocked by default.** You have to unlock them explicitly (checkboxes in the GUI, `--allow-create` / `--allow-delete` flags).
5. **Test on one person first.** Make a CSV with a single row, apply it, and check in the 3CX admin console that the result is what you wanted. Only then run the full file. In the CLI, `--limit 1` does this for you.
6. **Personal data (GDPR).** An export from the PBX contains names, email addresses and phone numbers. Do not email it, do not commit it to git, do not leave it in a shared folder. `export_*` files are in `.gitignore`.

---

## Step 0: create API access in 3CX (one time)

The tool logs in to the PBX with an "API key", not with your admin password.

1. Log in to the 3CX admin console.
2. Go to **Integrations → API** (the menu name can differ slightly between versions).
3. Click **Add** and create an API client. Set:
   - **Client ID**: any name/number (this is the login),
   - **Role / permissions**: as much as you need, ideally administrative (see "Known issues" below),
   - save, and **copy the API Key (Client Secret)**. It is usually shown only once.
4. The PBX address (FQDN) is what you type in the browser, e.g. `pbx.yourcompany.ie`.

> Do not enter your admin console password or SSH password into this tool. They are not needed.

---

## Desktop window (GUI): step by step

1. Start `tcx-bulk-gui`. Enter the **PBX address**, **Client ID** and **Client Secret**, then click **Connect and test**.
   The address and Client ID are remembered for next time. **The secret is never written to disk.**
2. In section 2, pick the **Entity**. The default is `Users` (extensions / users). After connecting, the list contains **every entity your PBX exposes**.
3. Click **Export current state to CSV…**. You get a file with the current data. It is your template and an extra backup.
4. Open the CSV in Excel. **Keep the key column** (for `Users` it is `Number`) and **only the columns you want to change**. Delete the rest. Edit the values and save as CSV.
5. **Load CSV (preview only)…** shows what will change and what is an error. Nothing is changed yet.
6. If the preview looks right, click **APPLY CHANGES** and confirm. Rows with errors are skipped.
7. Something went wrong? **Undo changes from a backup…** and pick the `backup.json` from the `tcx-bulk-runs` folder.

## Terminal (CLI)

Copy `.env.example` to `.env` and fill it in (or set the environment variables `TCX_FQDN`, `TCX_CLIENT_ID`, `TCX_CLIENT_SECRET`).

```bash
tcx-bulk check                              # does the login work?
tcx-bulk entities                           # list every entity on the PBX
tcx-bulk export -o export_users.csv         # current state -> CSV
tcx-bulk apply changes.csv                  # PREVIEW (dry run), changes nothing
tcx-bulk apply changes.csv --execute --limit 1   # test with 1 change
tcx-bulk apply changes.csv --execute        # everything (asks for confirmation)
tcx-bulk rollback tcx-runs/20261124-103000/backup.json --execute
```

Other entities: `--entity Groups`, `--entity Queues`, etc. You can force the key column with `--key Name`. By default the tool tries `Number`, then `Name`, then `Id`. Useful flags: `--skip-errors` (apply the valid rows even if some rows are bad), `--stop-on-error`, `--delay 0.5` (pause between changes if the PBX is busy), `-v` (also show rows with no changes).

---

## How to prepare the CSV file

Example (`examples/users_update.csv`):

```csv
Number,FirstName,LastName,EmailAddress,MobileNumber
100,John,Murphy,john.murphy@company.ie,0851234567
101,Aoife,Byrne,aoife.byrne@company.ie,
102,,,new.address@company.ie,
```

| Rule | What it means |
|---|---|
| Key column | Used to find the record on the PBX (for `Users`: `Number`). It must be in the file. |
| **Empty cell** | **Leave this field unchanged.** (Row 102 changes only the email.) |
| `__EMPTY__` | Set the field to an empty value (clearing it). |
| Column names | Must match the PBX field names exactly (see the headers of an export). **A typo in a header aborts everything before any change is made**, so nothing breaks silently. |
| Separator | Comma, semicolon or tab, detected automatically (Excel in many European locales saves semicolons). |
| True/false | `true/false`, `yes/no`, `1/0`. |
| Phone numbers | Kept as text, so a leading zero is not lost. |
| Nested fields | Put JSON in the cell, e.g. `[{"GroupId":1}]`. For advanced users. |
| `action` column | Optional: `update` (default), `create`, `delete`. Needs the unlock flags (see above). Template: `examples/users_create_delete.csv`. |

The tool is **idempotent**: if the value on the PBX already equals the one in the file, the row is marked "no changes". Running the same file twice is safe.

---

## Known limitations and issues (honest list)

- **This tool has not yet been tested against a real 3CX PBX.** It was written from the [official Configuration API specification](https://www.3cx.com/docs/configuration-rest-api-endpoints/) and tested against a simulated PBX (22 automated tests). So **do your first test on a single test user**, e.g. changing only `FirstName`.
- **403 on `/Users`.** Some people on the 3CX forum report that an API client gets a token with the `user` role instead of admin, and then `/Users` returns 403. Check the API client's role in the admin console. If the problem stays, run `tcx-bulk check` and look at the message.
- **Not everything can be changed through the API.** Some fields are only references or are locked by 3CX (for example, the current user status `CurrentProfileName` cannot be changed with PATCH). You will see such an error against the specific row in the results (`result.csv`).
- **The `Users` list may not include every account** (for example accounts with the System Owner role).
- **All entities = CRUD operations.** The tool reads, updates, creates and deletes records of any entity listed in the PBX `$metadata`. It does not support special API actions (e.g. restarting a service, OData function calls).
- The TLS certificate is **verified** by default. If you use your own CA, point to the file in the *CA file* field (or `TCX_CA_BUNDLE`). "Do not verify certificate" is a last resort.

---

## For developers

```bash
pip install -e ".[dev]"
pytest -q                     # tests (GUI tests need tkinter + DISPLAY; in CI: xvfb-run)
python -m tcx_bulk.gui        # run the window from source
```

Building the executables locally (on a given OS, for that OS only):

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name tcx-bulk-gui --paths src packaging/gui_entry.py
pyinstaller --onefile --name tcx-bulk --paths src packaging/cli_entry.py
```

Layout: `src/tcx_bulk/client.py` (HTTP, token, retries), `bulk.py` (CSV, change plan, backup, rollback), `cli.py`, `gui.py`; `tests/fakepbx.py` is the PBX simulator.
