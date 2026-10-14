"""The organization library as a private local Git repository: each execution or undo becomes one commit.

Only paths that Code Steward wrote or removed are committed, under its own name. Sensitive copies and
build/system junk are never committed. Repository problems left by interrupted Git commands are
found and, where it is safe, repaired.
"""
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import unicodedata

from Services.ScanService import _is_link

logger = logging.getLogger("code-steward.versions")
AUTHOR_NAME, AUTHOR_EMAIL = "Code Steward", "code-steward@localhost"
BRANCH = "main"
MESSAGE_FILE = "CODE_STEWARD_COMMIT_MSG"
# Build intermediates and system/editor files are never committed (they stay untracked in the library).
# Programs themselves (.exe, .so, ...) are artifacts the library organizes, so they are versioned.
JUNK = ["*.o", "*.obj", "*.pyc", "*.pyo", "*.class", "__pycache__/", ".DS_Store", "Thumbs.db", "desktop.ini",
        "*.swp", "*~", ".code-steward-*.tmp"]
EXCLUDE_START, EXCLUDE_END = "# >>> Code Steward (managed block)", "# <<< Code Steward"
SUBJECT = re.compile(r"^[A-Z][A-Za-z0-9 ,.;:'()&+-]{9,71}$")  # no slashes: never a path
VERBS = {"organize", "organise", "update", "add", "move", "rename", "copy", "remove", "undo", "organized",
         "organised", "updated", "added", "moved", "renamed", "copied", "removed", "new", "changed"}
STALE_LOCK_SECONDS = 60


class GitError(RuntimeError):
    pass


def ignore_pattern(path):
    """A .gitignore line matching exactly this library path (Git wildcards and spaces escaped)."""
    return "/" + "".join("\\" + c if c in "\\*?[]#! " else c for c in path)


def folded(value):
    return unicodedata.normalize("NFC", value).casefold()


def ai_subject(analysis_service, facts):
    """Ask the local model to name the kinds of code in a change (a short noun phrase)."""
    client, tag, _ = analysis_service.topic_model()
    messages = [
        {"role": "system", "content": "You describe changes for Git commit messages of a tool that organizes code "
                                      "files into topic folders. Answer with JSON only."},
        {"role": "user", "content": "Name the main kinds of code in this change as a short English noun phrase of at "
                                    "most 50 characters, without a verb at the start, file paths, numbers, quotes or "
                                    "secrets. Examples: 'machine-learning examples and setup scripts', "
                                    "'the word-count script'.\n\n" + facts},
    ]
    schema = {"type": "object", "properties": {"description": {"type": "string"}}, "required": ["description"]}
    response = client.ask(tag, messages, schema, num_predict=48)
    return json.loads(response["message"]["content"])["description"]


class VersionService:
    """Commits organization runs to a Git repository inside the library (VC-10)."""

    def __init__(self, config_service, subject_writer=None, git=None):
        self.config_service = config_service
        self.subject_writer = subject_writer  # callable(facts) -> str; the local model writes the subject
        self.git_path = git or shutil.which("git")

    def enabled(self):
        return bool(self.config_service.get("organization", "git", default=True))

    # Running Git

    def git(self, library, *args, check=True, stdin=None, literal=True):
        """Run Git in the library only: no parent repository, hooks, signing or user identity is used.

        literal: paths are never patterns (check-ignore takes plain paths and rejects this setting)."""
        if not self.git_path:
            raise GitError("Git is not installed, so the library has no version history.")
        library = Path(library)
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(GIT_CEILING_DIRECTORIES=str(library.parent), GIT_TERMINAL_PROMPT="0", LC_ALL="C")
        if literal:
            env["GIT_LITERAL_PATHSPECS"] = "1"
        command = [self.git_path, "--no-optional-locks", "-C", str(library),
                   "-c", f"core.hooksPath={library / '.git' / 'code-steward-no-hooks'}",
                   "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false",
                   "-c", "core.quotepath=false", "-c", f"user.name={AUTHOR_NAME}", "-c", f"user.email={AUTHOR_EMAIL}",
                   *args]
        try:
            result = subprocess.run(command, input=stdin, capture_output=True, timeout=120, env=env)
        except subprocess.TimeoutExpired as error:
            raise GitError(f"git {args[0]} did not finish in time") from error
        if check and result.returncode != 0:
            raise GitError(result.stderr.decode("utf-8", "replace").strip() or f"git {args[0]} failed")
        return result

    @staticmethod
    def _nul(paths):
        return b"".join(path.encode("utf-8") + b"\0" for path in paths)

    def _lines(self, library, *args):
        output = self.git(library, *args).stdout.decode("utf-8", "replace")
        return [item for item in output.split("\0") if item]

    # The repository

    def ensure(self, library, sensitive=()):
        """Create the repository when needed, keep its exclusions current and fix safe problems."""
        library = Path(library)
        dot_git = library / ".git"
        if os.path.lexists(dot_git):
            if _is_link(dot_git) or not dot_git.is_dir():
                raise GitError("The library's .git is a link or a file; it is left alone.")
        else:
            self.git(library, "init", "--quiet")
            self.git(library, "symbolic-ref", "HEAD", f"refs/heads/{BRANCH}")
            logger.info("Created the library's private Git repository: %s", library)
        top = self.git(library, "rev-parse", "--show-toplevel").stdout.decode("utf-8", "replace").strip()
        if folded(os.path.realpath(top)) != folded(os.path.realpath(library)):
            raise GitError("Git did not use the library's own repository; nothing was committed.")
        self._write_excludes(library, sensitive)
        self.repair(library)

    @staticmethod
    def _write_excludes(library, sensitive):
        """Junk and sensitive paths in .git/info/exclude (local to this repository, never committed)."""
        path = library / ".git" / "info" / "exclude"
        path.parent.mkdir(exist_ok=True)
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        start, end = text.find(EXCLUDE_START), text.find(EXCLUDE_END)
        if start != -1 and end > start:
            text = text[:start] + text[end + len(EXCLUDE_END):].lstrip("\n")
        block = "\n".join([EXCLUDE_START, *JUNK, *(ignore_pattern(item) for item in sorted(set(sensitive))), EXCLUDE_END])
        path.write_text(text.rstrip("\n") + ("\n" if text.strip() else "") + block + "\n", encoding="utf-8")

    def commit(self, library, changed, removed, facts, body, sensitive=()):
        """Commit exactly these library paths (written or removed by Code Steward).

        Returns {"commit", "subject", "subject_by", "files", "not_committed"} or None when nothing changed.
        Sensitive and ignored files are never staged; other staged changes in the library stay as they are.
        """
        library = Path(library)
        sensitive = set(sensitive)
        self.ensure(library, sensitive)
        written = sorted({path for path in changed if (library / path).is_file()})
        ignored = self._ignored(library, written)
        not_committed = sorted((set(written) & sensitive) | ignored)
        written = [path for path in written if path not in sensitive and path not in ignored]
        if written:
            self.git(library, "add", "--pathspec-from-file=-", "--pathspec-file-nul", stdin=self._nul(written))
        tracked = set(self._lines(library, "ls-files", "-z"))
        paths = written + sorted({path for path in removed if path in tracked and not (library / path).exists()})
        if not paths or not self._differs(library, paths):
            return None
        subject, subject_by = self._subject(facts)
        message = library / ".git" / MESSAGE_FILE
        message.write_text(f"{subject}\n\n{body.strip()}\n", encoding="utf-8")
        try:
            self.git(library, "commit", "--quiet", "--no-verify", "--only", "-F", str(message),
                     "--pathspec-from-file=-", "--pathspec-file-nul", stdin=self._nul(paths))
        finally:
            message.unlink(missing_ok=True)
        head = self.git(library, "rev-parse", "HEAD").stdout.decode().strip()
        logger.info("Library commit %s: %s (%d files)", head[:12], subject, len(paths))
        return {"commit": head, "subject": subject, "subject_by": subject_by, "files": len(paths),
                "not_committed": not_committed}

    def _ignored(self, library, paths):
        """The paths that .gitignore or .git/info/exclude rules exclude (checked even if already tracked)."""
        if not paths:
            return set()
        result = self.git(library, "check-ignore", "-z", "--no-index", "--stdin", check=False, stdin=self._nul(paths),
                          literal=False)
        if result.returncode not in (0, 1):  # 1: none of them is ignored
            raise GitError(result.stderr.decode("utf-8", "replace").strip() or "git check-ignore failed")
        return {item for item in result.stdout.decode("utf-8", "replace").split("\0") if item}

    def _changes(self, library):
        """(status letters, path) for every changed, staged or untracked path."""
        output = self.git(library, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
        items, changes = output.decode("utf-8", "replace").split("\0"), []
        while items:
            item = items.pop(0)
            if len(item) > 3:
                changes.append((item[:2], item[3:]))
                if item[0] in "RC" and items:
                    items.pop(0)  # the old name of a rename or copy
        return changes

    def own_unchanged(self, library, path):
        """The committed blob id when path is Code Steward's own unchanged file, else None.

        Own: the last commit that changed it is Code Steward's. Unchanged: the file and the index equal
        that commit. Only such a file may be replaced by a newer version (Git keeps the older one).
        """
        library = Path(library)
        if not (library / ".git").is_dir():
            return None
        last = self.git(library, "log", "-1", "--format=%ae", "--", path, check=False)
        if last.returncode != 0 or last.stdout.decode("utf-8", "replace").strip() != AUTHOR_EMAIL:
            return None
        for extra in ((), ("--cached",)):
            if self.git(library, "diff", "--quiet", *extra, "HEAD", "--", path, check=False).returncode != 0:
                return None
        blob = self.git(library, "rev-parse", "--verify", "--quiet", f"HEAD:{path}", check=False)
        return blob.stdout.decode().strip() if blob.returncode == 0 else None

    def blob(self, library, blob_id):
        return self.git(library, "cat-file", "blob", blob_id).stdout

    def _differs(self, library, paths):
        """True when any of the paths differs from the last commit (or there is no commit yet)."""
        if self.git(library, "rev-parse", "--verify", "--quiet", "HEAD", check=False).returncode != 0:
            return True
        wanted = set(paths)
        return any(path in wanted for _, path in self._changes(library))

    def _subject(self, facts):
        """facts: the program's subject line, then the details the model summarizes.

        The program's verb ("Organize", "Update", ...) starts the subject; the model only names the kinds of
        code, so it cannot misstate what happened. Without a usable answer the program's line is used.
        """
        lines = facts.splitlines()
        fallback = lines[0][:72]
        if self.subject_writer is None or len(lines) < 2:
            return fallback, "program"
        try:
            words = " ".join(str(self.subject_writer("\n".join(lines[1:]))).split()).strip(" .").split(" ")
        except Exception as error:  # the model is optional for a commit; the program subject is used
            logger.info("Commit subject from the program (no model answer: %s).", error)
            return fallback, "program"
        while words and words[0].lower() in VERBS:
            words = words[1:]  # the model added a verb or "updated"/"new" anyway
        subject = f"{lines[0].split(' ', 1)[0]} {' '.join(words)}".strip()
        if SUBJECT.fullmatch(subject) and len(words) >= 2:
            return subject, "ai"
        logger.info("Commit subject from the program (model answer not usable: %r).", subject[:100])
        return fallback, "program"

    # Status and repair (VC-10.5, VC-10.6)

    def status(self, library):
        library = Path(library)
        if not (library / ".git").is_dir():
            return {"path": str(library), "repository": False,
                    "message": "No version history yet; it is created by the first organization run."}
        problems = self.problems(library)
        result = {"path": str(library), "repository": True, "problems": problems}
        branch = self.git(library, "symbolic-ref", "--short", "--quiet", "HEAD", check=False)
        result["branch"] = branch.stdout.decode().strip() if branch.returncode == 0 else None
        head = self.git(library, "rev-parse", "--verify", "--quiet", "HEAD", check=False)
        result["head"] = head.stdout.decode().strip() if head.returncode == 0 else None
        result["remotes"] = self.git(library, "remote").stdout.decode().split()
        try:
            changes = self._changes(library)
            result["uncommitted"] = {"untracked": sum(code == "??" for code, _ in changes),
                                     "changed": sum(code != "??" for code, _ in changes)}
        except GitError as error:
            result["uncommitted"] = {"error": str(error)}
        if result["head"]:
            log = self.git(library, "log", "-10", "--format=%H%x1f%an%x1f%aI%x1f%s", check=False)
            result["commits"] = [dict(zip(("commit", "author", "date", "subject"), line.split("\x1f")))
                                 for line in log.stdout.decode("utf-8", "replace").splitlines() if line]
        return result

    def problems(self, library):
        library = Path(library)
        git_dir, found = library / ".git", []
        if (git_dir / "index.lock").exists():
            found.append("An interrupted Git command left index.lock behind.")
        if not self._index_readable(library):
            found.append("The Git index is damaged.")
        branch = self.git(library, "symbolic-ref", "--quiet", "HEAD", check=False)
        head = self.git(library, "rev-parse", "--verify", "--quiet", "HEAD", check=False)
        if branch.returncode != 0:
            found.append("HEAD is detached from the main branch.")
        elif head.returncode != 0 and self._last_head(library):
            found.append("The branch reference is missing although commits exist.")
        for name, text in (("MERGE_HEAD", "merge"), ("rebase-merge", "rebase"), ("rebase-apply", "rebase"),
                           ("CHERRY_PICK_HEAD", "cherry-pick")):
            if (git_dir / name).exists():
                found.append(f"An unfinished {text} needs a person to finish or abort it.")
        if self.git(library, "remote", check=False).stdout.strip():
            found.append("A remote is configured; the library should stay private.")
        return found

    def _index_readable(self, library):
        result = self.git(library, "ls-files", "--stage", check=False)
        return result.returncode == 0 or b"index" not in result.stderr

    def _last_head(self, library):
        """The newest commit in HEAD's reflog that still exists, or None."""
        log = library / ".git" / "logs" / "HEAD"
        if not log.exists():
            return None
        for line in reversed(log.read_text(encoding="utf-8", errors="replace").splitlines()):
            parts = line.split()
            if len(parts) > 1 and re.fullmatch(r"[0-9a-f]{40,64}", parts[1]) and \
                    self.git(library, "cat-file", "-e", parts[1] + "^{commit}", check=False).returncode == 0:
                return parts[1]
        return None

    def repair(self, library):
        """Fix what is safe to fix automatically; the working files are never changed."""
        library = Path(library)
        git_dir, fixed = library / ".git", []
        lock = git_dir / "index.lock"
        if lock.exists() and time.time() - lock.stat().st_mtime > STALE_LOCK_SECONDS:
            lock.unlink()
            fixed.append("Removed index.lock left by an interrupted Git command.")
        if not self._index_readable(library):
            (git_dir / "index").replace(git_dir / f"index.damaged-{int(time.time())}")
            if self.git(library, "rev-parse", "--verify", "--quiet", "HEAD", check=False).returncode == 0:
                self.git(library, "reset", "--quiet")  # rebuilds the index from the last commit only
            fixed.append("Rebuilt the damaged Git index from the last commit.")
        main = self.git(library, "rev-parse", "--verify", "--quiet", f"refs/heads/{BRANCH}", check=False)
        if self.git(library, "symbolic-ref", "--quiet", "HEAD", check=False).returncode != 0:
            head = self.git(library, "rev-parse", "HEAD").stdout.decode().strip()
            if main.returncode != 0:
                self.git(library, "update-ref", f"refs/heads/{BRANCH}", head)
            if self.git(library, "rev-parse", f"refs/heads/{BRANCH}").stdout.decode().strip() == head:
                self.git(library, "symbolic-ref", "HEAD", f"refs/heads/{BRANCH}")
                fixed.append("Re-attached HEAD to the main branch.")
        elif self.git(library, "rev-parse", "--verify", "--quiet", "HEAD", check=False).returncode != 0:
            last = self._last_head(library)
            if last:
                target = self.git(library, "symbolic-ref", "HEAD").stdout.decode().strip()
                self.git(library, "update-ref", target, last)
                fixed.append("Restored the missing branch reference from the reflog.")
        for item in fixed:
            logger.warning("Library repository repaired: %s", item)
        return {"fixed": fixed, "problems": self.problems(library)}
