# System context

Who uses Code Kamaji, and what systems it interacts with.

```mermaid
flowchart LR
    user["<b>User</b><br/>organizes their scripts"]
    tester["<b>Tester</b><br/>validates the system"]

    kamaji(["<b>Code Kamaji</b><br/>background agent + REST API"])

    roots[("Watched folders")]
    config["config.yaml"]
    db[("SQLite database")]
    logs["Log files<br/>rotating JSON Lines"]
    library[("Organized library<br/>+ git history")]

    user -- "REST API" --> kamaji
    tester -- "REST API" --> kamaji
    kamaji -- "reads settings" --> config
    kamaji -- "watches, scans" --> roots
    kamaji -- "saves artifacts" --> db
    kamaji -- "writes activity" --> logs
    kamaji -. "copies, commits (later)" .-> library
```

| Actor  | Goal                                   | Uses                                       |
| ------ | -------------------------------------- | ------------------------------------------ |
| User   | Keep their scripts found and organized | Browser (`/docs`), curl, `config.yaml`     |
| Tester | Check the system behaves as specified  | Same API, plus test-only endpoints (later) |

- **Watched folders**: read-only. Code Kamaji never runs, moves or changes the originals.
- **Protected folders**: system folders (e.g. `C:\Windows`) and `deny_paths` from the config are never read.
- **Local only**: nothing leaves the machine.
- **Low impact**: runs at low CPU and disk priority.
- **Logs**: kept out of SQLite so they still work when the DB fails.
