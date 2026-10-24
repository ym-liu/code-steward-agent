"""Resolve local Python imports without running user code."""
import ast
import hashlib
import posixpath
from pathlib import Path
from collections import defaultdict


def direct_imports(records, sources=None):
    """Resolve Python imports from unchanged source, including uniquely identifiable flattened modules.

    Return source keys mapped to importing file keys and literal import evidence.
    Ambiguous names, changed files and descriptions alone never establish an import.
    """
    parsed, evidence = {}, defaultdict(list)
    for key, record in records.items():
        if not record["path"].endswith(".py") or record.get("sensitive"):
            continue
        try:
            data = sources[record["path"]].encode("utf-8") if sources is not None else Path(record["root_folder"], record["path"]).read_bytes()
            if sources is None and hashlib.sha256(data).hexdigest() != record.get("sha256"):
                continue
            tree = ast.parse(data)
        except (OSError, SyntaxError, ValueError, KeyError):
            continue
        exports = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                exports.update(t.id for target in targets for t in ast.walk(target) if isinstance(t, ast.Name))
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                exports.update(a.asname or a.name.split(".")[0] for a in node.names)
        parsed[key] = tree, exports
    for importer, (tree, _) in parsed.items():
        root, path = importer
        available = {p: key for key in parsed for r, p in [key] if r == root and key != importer}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                specs = [(a.name, set(), 0) for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                specs = [(node.module or "", {a.name for a in node.names}, node.level)]
                # from package import module is also a module import.
                specs += [(".".join(filter(None, [node.module, a.name])), set(), node.level)
                          for a in node.names if a.name != "*"]
            else:
                continue
            for module, symbols, level in specs:
                stem = module.replace(".", "/")
                base = posixpath.dirname(path)
                if level:
                    for _ in range(level - 1):
                        base = posixpath.dirname(base)
                choices = [posixpath.join(base, stem)]
                if not level:
                    choices.append(stem)
                matches = []
                for choice in choices:
                    matches = [available[p] for p in (choice + ".py", choice + "/__init__.py") if p in available]
                    if matches:
                        break
                if not matches and not level:
                    matches = [key for p, key in available.items()
                               if p.endswith("/" + stem + ".py") or p.endswith("/" + stem + "/__init__.py")]
                if not matches and symbols and "*" not in symbols and "." in module and not level:
                    # A missing package folder can be recovered only with unique name AND symbol evidence.
                    matches = [key for p, key in available.items() if posixpath.basename(p) == module.split(".")[-1] + ".py"
                               and symbols <= parsed[key][1]]
                if len(matches) != 1:
                    continue
                source = matches[0]
                quote = ast.unparse(node)
                item = {"importer": importer, "quote": quote}
                if item not in evidence[source]:
                    evidence[source].append(item)
    return evidence
