import argparse
from pathlib import Path
import sys
import webbrowser

from .core import ROOT, LAB, LabError, collect_cases, load_config, resolve_model
from .ollama_client import OllamaClient
from .reporting import regenerate
from .readable_report import friendly_error
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
            source.add_argument("--suite", choices=MODEL_SUITES, default="smoke", help="smoke: all supported files in ai_lab/samples; dev: 24; holdout: 12 final cases")
            p.add_argument("--output", type=Path, default=ROOT / "ai-results", help="Results root directory")
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
    checks = commands.add_parser("check-inputs", help="Check six invalid input fixtures offline (no AI)")
    checks.add_argument("--config", type=Path)
    checks.add_argument("--output", type=Path, default=ROOT / "ai-results")
    return root


def guided_args():
    config_path = LAB / "config.simple.json"
    config = load_config(config_path)
    aliases = list(config["models"])
    print("\nCode Explanation Test\n")
    print(f"Test folder: {LAB / 'samples'}")
    print("Choose a model. All supported code files in this folder and its subfolders will be tested.\n")
    for n, alias in enumerate(aliases, 1):
        tag = config["models"][alias]
        label = {"qwen-4b": "Qwen3.5 4B (try this first)", "qwen-9b": "Qwen3.5 9B (larger model)"}.get(alias, tag)
        print(f"  {n}. {label}")
    print("  0. Quit\n\nOpen Ollama first. Missing models download automatically on first use.")
    while True:
        choice = input("Choose a model [1]: ").strip().lower() or "1"
        if choice in {"0", "q"}:
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(aliases):
            return ["run", "--config", str(config_path), "--model", aliases[int(choice) - 1],
                    "--pull", "--simple-output", "--open-report"]
        print(f"Enter 1 to {len(aliases)}, or 0 to quit.")


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
        args = parser().parse_args(argv)
        if args.command == "report":
            regenerate(args.folder)
            print(f"Updated {args.folder / 'summary.md'} (human scores preserved).")
            return 0
        config_path = args.config
        if (config_path is None and args.command in {"run", "compare", "demo", "doctor"}
                and getattr(args, "suite", "smoke") == "smoke"):
            config_path = LAB / "config.simple.json"
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
