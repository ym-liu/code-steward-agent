# File indexers

How real tools keep track of millions of files, and what Code Kamaji borrows from them. Based on public docs and engineering blogs; closed-source details may differ.

## The tools

| Tool            | Notices changes with                                  | Understands files with                        | Keeps impact low by                  |
| --------------- | ----------------------------------------------------- | --------------------------------------------- | ------------------------------------ |
| Windows Search  | The NTFS change journal (needs admin)                 | Plugins per file type                         | Low priority, pausing while you work |
| macOS Spotlight | FSEvents, which keeps a history per drive             | Plugins per file type, in worker processes    | Low priority                         |
| Dropbox         | A file watcher + periodic rescans                     | Hashes of file blocks                         | Only syncing what changed            |
| VS Code         | A file watcher with excluded folders (`node_modules`) | Language plugins per file type                | Skipping excluded folders            |
| JetBrains       | A file watcher + a saved snapshot of the disk         | Indexes that rebuild when their logic changes | Skipping excluded folders            |

## Patterns they share

- **Watcher + snapshot + rescan**: watch for changes, keep a saved snapshot of every file, and rescan now and then to catch what the watcher missed.
- **Diff, don't redo**: compare the snapshot with the disk, and only process what changed. Dropbox's sync engine is built around this.
- **Versioned processing**: when the logic that understands files changes, only results from the old logic are redone.
- **Plugins per file type**: each file type gets its own small extractor, so new types don't change the core.
- **Low impact**: low CPU and disk priority, and backing off while the user is busy.

## What Code Kamaji does

| Pattern                     | Code Kamaji                                            |
| --------------------------- | ------------------------------------------------------ |
| Watcher + snapshot + rescan | ✅ Watcher, the DB, and full scans at startup and daily |
| Diff, don't redo            | ✅ "Compare with DB" step                               |
| Excluded folders            | ✅ `exclude_folders` in config                          |
| Low process priority        | ✅ In the design (context, data flow)                   |
| Versioned processing        | ✅ `classifier_version` per artifact                    |
| Plugins per file type       | 🔜 Later: fits "recognize any file type", and AI later  |
| Back off while user is busy | 🔜 Later: needs idle detection                          |
| Worker processes            | 🔜 Much later: crash isolation                          |
