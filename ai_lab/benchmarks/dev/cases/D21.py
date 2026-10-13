import importlib
import os

module = importlib.import_module(os.environ["FORMAT_PLUGIN"])
print(module.render("example"))
