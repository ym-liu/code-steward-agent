import argparse
from pathlib import Path
import sys

from .core import ROOT, LabError, collect_cases, load_config, resolve_model
from .ollama_client import OllamaClient
from .reporting import regenerate
from .runner import DemoClient, run_experiment
from .suites import MODEL_SUITES, check_inputs, load_suite


def parser():
    root = argparse.ArgumentParser(description="Code Steward local AI lab. No arguments opens a guided menu.")
    commands = root.add_subparsers(dest="command")
    for name, help_text in (("run", "Test one model"), ("compare", "Test several models sequentially"),
                            ("demo", "Try reports offline with scripted outputs"), ("doctor", "Check setup without inference")):
        p = commands.add_parser(name, help=help_text)
        p.add_argument("--config", type=Path, help="JSON config (default: ai_lab/config.json)")
        if name in {"run", "compare", "demo"}:
            source = p.add_mutually_exclusive_group()
            source.add_argument("--input", type=Path, help="Your text file/folder")
            source.add_argument("--suite", choices=MODEL_SUITES, default="smoke", help="smoke: 6 quick cases; dev: 24; holdout: 12 final cases")
            p.add_argument("--output", type=Path, default=ROOT / "ai-results", help="Results root directory")
            p.add_argument("--repeat", type=int, default=1, help="1-20 passes through the same samples")
            p.add_argument("--device", choices=("cpu", "auto"), help="cpu by default; auto allows GPU")
        if name == "run":
            p.add_argument("--model", help="Preset alias or any installed local Ollama tag")
        if name == "compare":
            p.add_argument("--models", nargs="+", help="Aliases/tags; omitted uses all presets")
        if name in {"run", "compare"}:
            p.add_argument("--pull", action="store_true", help="Download missing models before testing")
    report = commands.add_parser("report", help="Regenerate a report after filling review.csv")
    report.add_argument("folder", type=Path)
    checks = commands.add_parser("check-inputs", help="Check six invalid input fixtures offline (no AI)")
    checks.add_argument("--config", type=Path)
    checks.add_argument("--output", type=Path, default=ROOT / "ai-results")
    return root


def choose_suite(arguments):
    print("\nTest set: 1. Quick check (6)  2. Development (24)  3. Final holdout (12)")
    print("Keep holdout unused until your prompt and settings are fixed.")
    selected = input("Choose a test set [1]: ").strip() or "1"
    suites = {"1": "smoke", "2": "dev", "3": "holdout"}
    if selected not in suites:
        raise LabError("Choose test set 1, 2 or 3.", "input_error")
    return arguments + ["--suite", suites[selected]]


def guided_args():
    config = load_config()
    aliases = list(config["models"])
    print("\nCode Steward AI Test Lab\n")
    for n, alias in enumerate(aliases, 1):
        print(f"  {n}. Test {alias}: {config['models'][alias]}")
    print("  A. Compare all presets\n  C. Test a custom local model\n  D. Offline demo (no Ollama needed)\n  I. Check invalid inputs (no AI)\n  H. Check setup\n  Q. Quit")
    print(f"\nReal tests use bundled samples with device={config['device']} and download missing models.\n")
    choice = input("Choose an option [1]: ").strip().lower() or "1"
    if choice == "q":
        return None
    if choice == "d":
        return choose_suite(["demo"])
    if choice == "i":
        return ["check-inputs"]
    if choice == "h":
        return ["doctor"]
    if choice == "a":
        return choose_suite(["compare", "--pull"])
    if choice == "c":
        return choose_suite(["run", "--model", input("Ollama model tag: ").strip(), "--pull"])
    if choice.isdigit() and 1 <= int(choice) <= len(aliases):
        return choose_suite(["run", "--model", aliases[int(choice)-1], "--pull"])
    raise LabError("Choose a number, A, C, D, I, H or Q.", "input_error")


def main(argv=None):
    try:
        argv = list(sys.argv[1:] if argv is None else argv)
        if not argv:
            if not sys.stdin.isatty():
                parser().print_help()
                return 0
            argv = guided_args()
            if argv is None:
                return 0
        args = parser().parse_args(argv)
        if args.command == "report":
            regenerate(args.folder)
            print(f"Updated {args.folder / 'summary.md'} (human scores preserved).")
            return 0
        config = load_config(args.config)
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
            print(f"Bundled samples: {len(collect_cases(None, config))}")
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
        _, code = run_experiment(client, config, models, cases, args.output.expanduser().absolute(), args.repeat,
                                 getattr(args, "pull", False), demo)
        return code
    except (LabError, OSError, EOFError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        print("See docs/AI_TESTING.md for setup and troubleshooting.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
