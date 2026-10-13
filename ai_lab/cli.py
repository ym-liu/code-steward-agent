import argparse
from pathlib import Path
import re
import shlex
import sys
from urllib.parse import unquote, urlsplit
import webbrowser

from .common import RESULTS, SAMPLES, SETTINGS, LabError, read_json, save_json
from .explain.config import load_config, resolve_model, validate_model
from .explain.files import collect_cases
from .explain.ollama_client import OllamaClient
from .organize import organize_run
from .testing.report_page import friendly_error
from .testing.report_records import regenerate
from .testing.runner import DemoClient, run_experiment
from .testing.suites import MODEL_SUITES, check_inputs, load_suite


def parser():
    root = argparse.ArgumentParser(description="Code Steward local AI lab. No arguments opens a guided menu.")
    commands = root.add_subparsers(dest="command")
    for name, help_text in (("run", "Test one model"), ("compare", "Test several models sequentially"),
                            ("demo", "Try reports offline with scripted outputs"), ("doctor", "Check setup without inference")):
        p = commands.add_parser(name, help=help_text)
        p.add_argument("--config", type=Path,
                       help="Settings file (default: ai_lab/settings/default.json; legacy.json for --suite dev/holdout)")
        if name in {"run", "compare", "demo"}:
            source = p.add_mutually_exclusive_group()
            source.add_argument("--input", type=Path, help="Your text file/folder")
            source.add_argument("--suite", choices=MODEL_SUITES, default="smoke", help="smoke: all supported files in ai_lab/samples; dev: 24; holdout: 12 final cases")
            p.add_argument("--output", type=Path, default=RESULTS, help="Results root directory")
            p.add_argument("--repeat", type=int, default=1, help="1-20 passes through the same samples")
            p.add_argument("--device", choices=("cpu", "auto"), help="cpu by default; auto allows GPU")
            p.add_argument("--open-report", action="store_true", help="Open the readable HTML report when finished")
            p.add_argument("--simple-output", action="store_true", help="Show short, plain-language progress")
        if name == "run":
            p.add_argument("--model", help="Preset alias or any installed local Ollama tag")
        if name == "compare":
            p.add_argument("--models", nargs="+", help="Aliases/tags; omitted uses all presets")
        if name in {"run", "compare"}:
            p.add_argument("--pull", action="store_true", help="Download missing models before testing")
    report = commands.add_parser("report", help="Regenerate a report after filling review.csv")
    report.add_argument("folder", type=Path)
    organize = commands.add_parser("organize", help="Classify files and propose folders from a finished run (dry run)")
    organize.add_argument("folder", type=Path, help="A result folder under ai-results/")
    organize.add_argument("--no-ai", action="store_true", help="Choose topics with program keyword hints only")
    organize.add_argument("--device", choices=("cpu", "auto"), help="Default: the device the run used")
    organize.add_argument("--score", action="store_true", help="Compare with the reference answers for ai_lab/samples")
    checks = commands.add_parser("check-inputs", help="Check six invalid input fixtures offline (no AI)")
    checks.add_argument("--config", type=Path)
    checks.add_argument("--output", type=Path, default=RESULTS)
    return root


def model_name(value):
    """Accept copied names, commands and model-page links as text, never shell code."""
    value = value.strip()
    if "://" in value:
        try:
            url = urlsplit(value)
        except ValueError:
            raise LabError("The model-page link could not be read. Copy it again.", "input_error") from None
        if (url.scheme not in {"http", "https"} or url.hostname not in {"ollama.com", "www.ollama.com"}
                or url.username or url.password):
            raise LabError("Use an Ollama model name or a model-page link from ollama.com.", "input_error")
        value = unquote(url.path).strip("/")
        if value.startswith("library/"):
            value = value[len("library/"):]
        elif "/" not in value:
            raise LabError("Open one model's page and copy its name or run command.", "input_error")
        if not value or value.endswith("/tags") or value.startswith("search"):
            raise LabError("Open one model's page and copy its name or run command.", "input_error")
    else:
        try:
            words = shlex.split(value)
        except ValueError:
            raise LabError("The pasted text has unmatched quotes. Copy the model name again.", "input_error") from None
        if len(words) == 3 and words[:2] in (["ollama", "run"], ["ollama", "pull"]):
            value = words[2]
        elif len(words) == 1:
            value = words[0]
        else:
            raise LabError("Paste just the model name, an ollama run command, or its model-page link.", "input_error")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", value):
        raise LabError("That does not look like an Ollama model name. Example: qwen3.5:4b", "input_error")
    return validate_model(value)


def canonical_model(tag):
    return tag if ":" in tag.rsplit("/", 1)[-1] else tag + ":latest"


def saved_models(path):
    if not path.exists():
        return []
    try:
        value = read_json(path)
        if (not isinstance(value, dict) or set(value) != {"models"}
                or not isinstance(value["models"], list)
                or any(not isinstance(tag, str) or model_name(tag) != tag for tag in value["models"])):
            raise LabError("Invalid saved model choices.", "config_error")
        return value["models"]
    except LabError:
        print("Saved model choices could not be read. The preset models are still available.")
        return None


def custom_model(config, choices, saved, saved_path):
    print("\nPaste a model name, an ollama run command, or an Ollama model-page link.")
    while True:
        value = input("Model (0 = back): ").strip()
        if not value or value.lower() in {"0", "q"}:
            return None
        try:
            tag = resolve_model(config, model_name(value))
        except LabError as error:
            print(str(error))
            continue
        print(f"Using: {tag}")
        if canonical_model(tag) in {canonical_model(resolve_model(config, choice)) for choice in choices}:
            return tag
        if saved is None:
            print("This model will run once; the saved model list could not be updated.")
            return tag
        while True:
            remember = input("Keep this model in the menu next time? [Y/n]: ").strip().lower() or "y"
            if remember in {"n", "no"}:
                return tag
            if remember in {"y", "yes"}:
                try:
                    latest = saved_models(saved_path)
                    if latest is None:
                        print("This model will run once; the saved model list could not be updated.")
                        return tag
                    if canonical_model(tag) not in {canonical_model(item) for item in latest}:
                        save_json(saved_path, {"models": [*latest, tag]})
                    print("Saved to the menu for next time.")
                except OSError:
                    print("Could not save the model choice. It will still run this time.")
                return tag
            print("Enter Y or N.")


def guided_args():
    config_path = SETTINGS / "default.json"
    config = load_config(config_path)
    choices = list(config["models"])
    saved_path = SETTINGS / "models.local.json"
    saved = saved_models(saved_path)
    known = {canonical_model(tag) for tag in config["models"].values()}
    for tag in saved or []:
        if canonical_model(tag) not in known:
            choices.append(tag)
            known.add(canonical_model(tag))
    print("\nCode Explanation Test\n")
    print(f"Test folder: {SAMPLES}")
    print("Choose a model. All supported code files in this folder and its subfolders will be tested.\n")
    for n, alias in enumerate(choices, 1):
        tag = config["models"].get(alias, alias)
        label = {"qwen-4b": "Qwen3.5 4B (try this first)", "qwen-9b": "Qwen3.5 9B (larger model)",
                 "qwen-coder-7b": "Qwen2.5-Coder 7B", "qwen-coder-3b": "Qwen2.5-Coder 3B"}.get(alias, tag)
        print(f"  {n}. {label}")
    print("  C. Add / use another model\n  0. Quit\n\nOpen Ollama first. Missing models download automatically on first use.")
    while True:
        choice = input("Choose a model [1]: ").strip().lower() or "1"
        if choice in {"0", "q"}:
            return None
        if choice == "c":
            tag = custom_model(config, choices, saved, saved_path)
            if tag is None:
                continue
            return ["run", "--config", str(config_path), "--model", tag,
                    "--pull", "--simple-output", "--open-report"]
        if choice.isdigit() and 1 <= int(choice) <= len(choices):
            return ["run", "--config", str(config_path), "--model", choices[int(choice) - 1],
                    "--pull", "--simple-output", "--open-report"]
        print(f"Enter 1 to {len(choices)}, C for another model, or 0 to quit.")


def guided_device():
    """CPU is the product setting; GPU is only a faster preview for testing."""
    print("\nRun on:\n  1. CPU (default, same as the final product)\n"
          "  2. GPU (faster preview; answers and speed can differ from CPU)\n  0. Quit\n")
    while True:
        choice = input("Choose CPU or GPU [1]: ").strip().lower() or "1"
        if choice in {"0", "q"}:
            return None
        if choice in {"1", "cpu"}:
            return "cpu"
        if choice in {"2", "gpu"}:
            return "auto"
        print("Enter 1 for CPU, 2 for GPU, or 0 to quit.")


def main(argv=None):
    try:
        argv = list(sys.argv[1:] if argv is None else argv)
        if not argv or argv == ["--interactive"]:
            if not argv and not sys.stdin.isatty():
                parser().print_help()
                return 0
            argv = guided_args()
            if argv is None:
                return 0
            device = guided_device()
            if device is None:
                return 0
            argv += ["--device", device]
        args = parser().parse_args(argv)
        if args.command == "organize":
            organize_run(args.folder, use_ai=not args.no_ai, device=args.device, check=args.score)
            return 0
        if args.command == "report":
            regenerate(args.folder)
            print(f"Updated {args.folder / 'summary.md'} (human scores preserved).")
            return 0
        config_path = args.config
        if (config_path is None and args.command in {"run", "compare", "demo", "doctor"}
                and getattr(args, "suite", "smoke") == "smoke"):
            config_path = SETTINGS / "default.json"
        config = load_config(config_path)
        if args.command == "check-inputs":
            return check_inputs(config, args.output.expanduser().absolute())
        if getattr(args, "device", None):
            config["device"] = args.device
        client = OllamaClient(config)
        if args.command == "doctor":
            print(f"Python: {sys.version.split()[0]}\nOllama: {client.base_url}\nDevice: {config['device']}")
            print(f"Server version: {client.version()}")
            installed = client.installed()
            for alias, tag in config["models"].items():
                state = "installed" if client.match(installed, tag) else "missing (use --pull)"
                print(f"  {alias}: {tag} - {state}")
            print(f"Samples folder files: {len(collect_cases(None, config))}")
            print("Setup checked. No inference was performed.")
            return 0
        if not 1 <= args.repeat <= 20:
            raise LabError("--repeat must be between 1 and 20.", "config_error")
        cases = collect_cases(args.input, config) if args.input is not None else load_suite(args.suite, config)
        demo = args.command == "demo"
        if demo:
            models, client = ["DEMO-NOT-A-REAL-MODEL"], DemoClient()
        else:
            if args.command == "compare":
                names = args.models or list(config["models"])
            else:
                names = [args.model or config["default_model"]]
            models = list(dict.fromkeys(resolve_model(config, name) for name in names))
        folder, code = run_experiment(client, config, models, cases, args.output.expanduser().absolute(), args.repeat,
                                      getattr(args, "pull", False), demo, simple=args.simple_output)
        if args.open_report:
            try:
                if not webbrowser.open((folder / "report.html").resolve().as_uri()):
                    print("The browser did not open. Double-click report.html in the result folder.")
            except (OSError, webbrowser.Error):
                print("The browser did not open. Double-click report.html in the result folder.")
        return code
    except (LabError, OSError, EOFError) as error:
        if "--simple-output" in argv or argv == ["--interactive"]:
            code = error.code if isinstance(error, LabError) else "error"
            print(friendly_error(code, str(error)), file=sys.stderr)
        else:
            print(f"ERROR: {error}", file=sys.stderr)
            print("See docs/AI_TESTING.md for setup and troubleshooting.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
