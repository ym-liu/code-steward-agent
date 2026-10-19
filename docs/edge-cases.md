# Edge cases

Tricky situations to check new code against. Fixes for some of them are in the [TODO](todo.md).

## Identity

| Case          | Watch out for                                                                                       |
| ------------- | --------------------------------------------------------------------------------------------------- |
| Rename / move | Shows up as one file deleted and another created. Must keep the artifact's ID                       |
| Atomic save   | Editors write a temp file, then rename it over the original. Must count as a modify, not a new file |

## Safety

| Case              | Watch out for                                                                                                   |
| ----------------- | --------------------------------------------------------------------------------------------------------------- |
| Protected folders | Watching `C:\` would include `C:\Windows`. System folders and `deny_paths` are never read                       |
| Symlinks          | A symlink (a pointer to another folder) can lead outside the watched folder, or loop forever. Never follow them |

## Volume

| Case                   | Watch out for                                                                            |
| ---------------------- | ---------------------------------------------------------------------------------------- |
| Burst of changes       | `git checkout`, unzip or `npm install` fire thousands of events. Process them in batches |
| Folder moved / deleted | One event for possibly thousands of files. Check every path under the folder             |
| Huge folders           | Millions of files. Full scans must stay cheap: stat every file, hash only changed ones   |

## Concurrency

| Case                    | Watch out for                                                 |
| ----------------------- | ------------------------------------------------------------- |
| Startup scan vs watcher | The startup scan and new watcher events can hit the same file |
| Scan during a scan      | A scan request arrives while a scan is running                |
| File changing mid-hash  | A file still being written gives a wrong hash                 |

## Availability

| Case                     | Watch out for                                                    |
| ------------------------ | ---------------------------------------------------------------- |
| Watched folder goes away | Unplugged drive or network share. Files are missing, not deleted |
| No file events           | Network and USB drives often send none. Changes go unnoticed     |

## Lifecycle

| Case              | Watch out for                                                                                   |
| ----------------- | ----------------------------------------------------------------------------------------------- |
| Shutdown mid-scan | Ctrl+C mid-scan must leave the DB consistent                                                    |
| Config reload     | Watched folders change while running. Records under a removed one become untracked, not deleted |
