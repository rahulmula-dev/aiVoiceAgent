import os
import sys
import importlib.util
import importlib.machinery

# 1. Find the standard library 'logging' location
# We search sys.path excluding the current working directory to find the real one.
cwd = os.path.abspath(os.getcwd())
std_paths = [p for p in sys.path if p and os.path.abspath(p) != cwd]
# Correct usage of PathFinder to search specific paths
std_spec = importlib.machinery.PathFinder.find_spec('logging', path=std_paths)

if std_spec and std_spec.submodule_search_locations:
    # 2. Merge our local 'logging' path with the standard library's search paths
    # This effectively makes 'logging' a combined package.
    __path__ = [os.path.dirname(__file__)] + list(std_spec.submodule_search_locations)
    
    # 3. Proxy top-level attributes from the standard library
    # To prevent recursion, we load it carefully
    std_mod = importlib.util.module_from_spec(std_spec)
    std_spec.loader.exec_module(std_mod)
    for name in dir(std_mod):
        if not name.startswith('__'):
            globals()[name] = getattr(std_mod, name)
else:
    # Fallback if we can't find the stdlib
    pass

# 4. Expose our custom modules
from .voice_logger import bind_call_context, log_conversation_turn, setup_global_logging
from .call_logger import CallLogger
