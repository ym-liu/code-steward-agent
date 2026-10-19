# TODO

Small things to come back to later.

## Scanning

- **Renames split across batches**: a rename shows up as "file deleted" + "file created". If they land in different batches, it looks like a new file. Fix: wait a bit before marking a file deleted.
- **Dropped events**: if too many changes happen at once, the OS drops some events. Fix: rescan the folders involved.
- **Network and USB drives**: they often don't report changes. Fix: scan those folders on a timer instead.
- **Back off while the user is busy**: pause heavy work while the computer is in use, like Windows Search does.
- **OS change log**: Windows (USN journal) and macOS (FSEvents) keep a log of file changes. Reading it would skip the full scan at startup. Windows needs admin, Linux has none.

## Identity and classifying

- **File ID**: the OS gives every file an ID (inode on Linux/macOS) that survives renames. Store it to detect renames without reading the file.
- **Sensitive files**: flag files with secrets, e.g. `.env`, `.pem`, `id_rsa`, so they're never copied to the library or sent to AI.
- **Plugins per file type**: one small extractor per file type, so supporting a new type doesn't change the core.

## Decisions to keep

- **AI**: ask the prof how much AI is expected, e.g. writing each script's purpose and usage.
- **`/logs`**: add a `since` filter instead of a clear endpoint. Flag it in the status when the log file can't be written.

## Tooling

- **Protect `main`**: in the GitHub repo settings, require CI to pass before a PR can merge.
- **`.gitignore`**: add `*.sqlite3`, `*.log` and `.env` as soon as those files exist.
