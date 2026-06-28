"""
config_io.py — safe, atomic editor for strategy_config.json.

Multiple processes read strategy_config.json (intraday, capitol, scheduler, TUI)
and a couple write it (sunday_review, the TUI editors). To avoid corrupt/partial
reads and clobbering, all TUI-side writes go through `update_config`, which does
a read → mutate → temp-file write → atomic `os.replace`. It changes ONLY the keys
the mutator touches and never bumps `version` (that stays sunday_review's job).
"""
import json
import os
import tempfile

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "strategy_config.json")


def load_config(path: str = CONFIG_FILE) -> dict:
    with open(path) as f:
        return json.load(f)


def update_config(mutator, path: str = CONFIG_FILE) -> dict:
    """Atomically apply `mutator(cfg)` to the config on disk.

    `mutator` receives the parsed dict and mutates it in place (or returns a new
    dict). The result is written to a temp file in the same directory and moved
    into place with os.replace (atomic on POSIX), so concurrent readers always
    see either the old or the new complete file — never a partial one.
    Returns the written config.
    """
    cfg = load_config(path)
    new = mutator(cfg)
    if new is None:
        new = cfg
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".cfg_", suffix=".json", dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(new, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return new
