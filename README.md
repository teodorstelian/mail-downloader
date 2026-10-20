# Yahoo Mail backup

Python 3.10+; no packages to install. Yahoo app passwords are the only sign-in method.

## Setup and run

Edit `.env` beside `main.py`:

```dotenv
YAHOO_EMAIL=you@yahoo.com
YAHOO_APP_PASSWORD=your-new-yahoo-app-password
BACKUP_OUTPUT=D:\MailBackup
BACKUP_LIMIT=
BACKUP_FOLDERS=all
```

A local `.env` has been prepared for this test with the email and destination filled in and the password blank. Enter a newly generated app password yourself. For a fresh checkout, copy `.env.example` to `.env`. `.env` and its variants are ignored by Git; `.env.example` contains placeholders and can be committed. The local password is stored as plain text, so keep this file private. Use a Yahoo app password, not your usual account password.

Set `BACKUP_LIMIT=10` to select up to 10 messages per folder, or `BACKUP_LIMIT=` for no limit. `--limit` overrides this setting for one run.

Run from the project folder:

```powershell
py main.py
```

Test only 10 Inbox messages first:

```powershell
py main.py --inbox-only --limit 10
```

The `.env` is always loaded from beside the script. Windows paths use ordinary single backslashes; optional matching quotes are supported. Values are literal: no variable expansion, escapes, or inline comments. Process environment variables override `.env`, and `--email` / `--output` / `--limit` override both. Relative backup paths resolve from the current working directory. A blank or missing password triggers a hidden terminal prompt.

`--limit 10` alone selects up to 10 per folder. Messages are selected by highest IMAP UID (most recently added to the folder, not necessarily newest Date). Rerunning selects the same sample rather than 10 additional messages. Clear `BACKUP_LIMIT`, set `BACKUP_FOLDERS=all`, and omit selection options for all selectable folders, including Sent, custom folders, Spam and Trash when exposed by Yahoo. A limited run prints “Selected backup complete”; it is not a complete account backup.

## Open and preserve backups

In File Explorer, search the destination for `*.eml`, then right-click a file and choose **Open with > Thunderbird** (or an EML-capable Outlook installation). Thunderbird also has **File > Open > Saved Message**. Each EML includes the original message, headers, and embedded attachments and can be read offline.

The script preserves Yahoo mail: it uses read-only folders and BODY.PEEK and does not delete, move or mark messages read. **Downloading does not free Yahoo storage.** Verify messages and attachments and keep a second backup before separately deciding which originals to delete. Mail that never reached Yahoo while storage was full cannot be recovered here.

Keep the whole backup directory together. Folders use readable names such as `Inbox` and `Sent`; `folder.json` records the original IMAP name. Unsafe characters are replaced, nested Yahoo names are flattened, and conflicting names get a unique suffix. EML names include UIDVALIDITY and UID, for example `Inbox/123-7.eml`. `manifest.sqlite3` tracks message UIDs and SHA-256 checksums. Rerunning verifies saved files, repairs missing or changed files, and downloads new messages. A Yahoo UID reset creates a separate generation, which can duplicate messages while preserving the old backup. Scans are not a simultaneous snapshot; contacts, calendars, account settings and message flags are not backed up.

Allow enough disk space, run one backup process per destination, and rerun after network failures or throttling. Backups are not encrypted by this script. Git ignores EML and manifest files as well as the default `yahoo-backup` directory; choose a destination outside the repository for your backups.

## Checks

```powershell
py -m unittest discover -s tests -v
```

## Date period and total counts

Each folder prints its total message count before filtering, then the number matching the period and the number selected after the limit. For Inbox only, run `py main.py --inbox-only`.

Set optional inclusive dates in `.env`:

```dotenv
BACKUP_START_DATE=2025-09-01
BACKUP_END_DATE=2025-09-30
```

Leave either blank for an open-ended period, or both blank for all dates. Dates use the server's IMAP internal received date, not the sender's Date header, with day-level precision. Imported or moved messages may have a different internal date. The limit is applied after date filtering, per folder; clear `BACKUP_LIMIT` to include all messages in the period. Already backed-up messages outside the period remain on disk.

For a one-run override: `py main.py --start-date 2025-09-01 --end-date 2025-09-30`. Invalid dates and reversed ranges are rejected before sign-in.


## Folder selection and Yahoo counts

Set `BACKUP_FOLDERS=inbox` in `.env` for Inbox only, or `BACKUP_FOLDERS=all` for every selectable folder. Local/example settings default to all folders without date or message limits. Without this setting the program preserves the earlier all-folders behavior. `--folders inbox` or `--folders all` overrides the setting for a single run; `--inbox-only` remains available.

The app uses `export.imap.mail.yahoo.com`, Yahoo's [documented download server](https://help.yahoo.com/kb/SLN28681.html). The earlier regular IMAP endpoint could expose a limited mailbox view. Counts show messages visible through the server before date or download limits; an exact 10,000 count now displays a possible-incompleteness notice. Compare with Yahoo webmail after rerunning. Do not treat a completed run as proof that Yahoo exposed every message. Yahoo also documents possible download issues for folders with 100,000 or more messages. Existing backups are retained and reused where folder UIDs and UIDVALIDITY match.

Existing hash-based backups are reused on the next run: selected, verified messages are copied into the readable folder without downloading again. Old files remain in place, so this can temporarily use additional disk space. Only messages selected by the current folder/date/limit settings are copied.

## Network interruptions

`IMAP_TIMEOUT=300` in `.env` controls the wait for each network operation in seconds. `BACKUP_RETRIES=5` allows five reconnect attempts after a timeout, connection reset, or aborted IMAP session, waiting 5, 10, 20, 30, then 30 seconds. Each attempt logs in again and resumes through the manifest; verified messages are skipped. Authentication and local disk errors stop immediately. If retries are exhausted, rerun `py main.py`. Increase the timeout for a slow connection or large attachments. Interrupted messages may need to download again, but completed and verified files are retained.

The default run backs up all folders and all dates without a message limit, with a 300-second network timeout and up to five reconnect attempts.

Message-specific FETCH NO responses and missing message bodies are logged and skipped so remaining messages and folders can continue. The summary separately counts downloaded messages, already verified backups, and failed/skipped messages. If anything failed, it says “finished with missing emails” instead of claiming a complete backup. Failed emails are not recorded as successfully backed up, so rerunning tries them again. Details, including available subject/date, folder and UID, are saved to `error logs.txt`. Network failures still use reconnect attempts, while protocol errors (FETCH BAD), authentication problems and local disk errors stop the run.

## Log files

Every backup run appends timestamped progress, folder counts, retries and completion messages to `logs.txt` beside `main.py`. `error logs.txt` receives warnings and errors only, including available email subject/date and folder on failed downloads. Output is also shown in the terminal. Both files are ignored by Git. Password input and `.env` contents are not logged. Logs can contain private email subjects; keep them private. Files append across runs, so previous history is preserved. Timestamps use the computer's local time.
