# Yahoo Mail backup

Requires Python 3.10 or newer. No packages to install.

1. Sign into your dad's Yahoo account with his permission. In Yahoo Account Security, generate an **app password** for this backup. Use that instead of his usual password. See [Yahoo's IMAP instructions](https://help.yahoo.com/kb/imap-server-settings-yahoo-mail-sln4075.html).
2. Choose a local drive with enough free space for the entire mailbox and attachments.
3. Open PowerShell in this project and run:

   ```powershell
   py main.py --email "dad@yahoo.com" --output "D:\MailBackup"
   ```

   Replace the address and destination. Paste the app password when prompted; it is hidden and never saved by this script. If `py` is unavailable, try `python`.

The script downloads every selectable folder, including Sent, custom folders, Spam and Trash when Yahoo exposes them. Each `.eml` file contains one original message with its headers and embedded attachments. Open these files with an email client that supports EML. Folder directories use safe hashes; `folder.json` records each original IMAP folder name (encoded as supplied by Yahoo). `manifest.sqlite3` tracks downloads and SHA-256 checksums.

Keep the entire backup directory together. Run the same command to resume or capture new mail. Previously saved files are checked against their recorded checksums; missing or changed files are downloaded again. If Yahoo resets folder UIDs, a separate generation is saved, which can duplicate messages but preserves old backups. Run only one backup process per destination. Large mailboxes can take hours or days; rerun after network failures or throttling. The backup reflects messages present as each folder is scanned, not a simultaneous snapshot.

## Check before clearing storage

Wait for “Backup complete”, inspect messages and attachments from several folders, and copy the backup to a second drive. The script opens folders read-only and uses BODY.PEEK: it does not delete, move, or mark mail as read. It does not back up contacts, calendars, or account settings. Local email is not encrypted by this script; store it on a protected drive.

Downloading alone **does not free Yahoo storage**. After verifying the backup, you can separately decide which originals to delete in Yahoo. Mail that never reached the account while storage was full cannot be recovered by this downloader.

If login fails, check the full email address and Yahoo-generated app password. If Yahoo will not generate one, this script cannot bypass that restriction; an email client supporting Yahoo's browser sign-in is an alternative.

## Development checks

```powershell
py -m unittest discover -s tests -v
```

## Try a small backup first

```powershell
py main.py --email "dad@yahoo.com" --output "D:\MailBackup" --inbox-only --limit 10
```

This selects the 10 highest IMAP UIDs in Inbox (most recently added to that folder, not necessarily the newest Date headers). `--limit 10` without `--inbox-only` selects up to 10 per folder. These are selected messages, not 10 additional downloads on each run. Remove both options to back up everything into the same destination; verified files are reused. A limited run prints “Selected backup complete”, which does not mean the entire account was backed up.

## Open downloaded messages

In Windows File Explorer, search the backup directory for `*.eml`. Right-click a result, choose **Open with**, and select Thunderbird. Each file opens as a message with its body and attachments. You can also open a saved message from Thunderbird's **File > Open > Saved Message** menu. Reading local EML files does not require signing into Yahoo.

## Sign-in without an app password

Yahoo supports OAuth2: you sign in on Yahoo's authorization page and the client receives a token rather than your account password. Thunderbird supports this already: https://support.mozilla.org/en-US/kb/thunderbird-and-yahoo

This script currently supports app passwords only. Implementing our own Yahoo OAuth application requires developer registration and Yahoo Mail access; it is not enabled merely by changing an IMAP option. Yahoo's developer documentation: https://senders.yahooinc.com/developer/documentation/
