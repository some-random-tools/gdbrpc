"""Configuration: the ~/.config/gdbrpc/.env file and its defaults."""

import os
from dataclasses import dataclass
from pathlib import Path

# Config folder (decision #12): a constant, everything else is configurable.
CONFIG_DIR = Path("~/.config/gdbrpc").expanduser()
ENV_FILE = CONFIG_DIR / ".env"
SHIM_FILE = CONFIG_DIR / "bootstrap.py"
DEFAULT_PLUGIN_DIR = CONFIG_DIR / "plugins"
DEFAULT_PORT = 8944


def parse_env_file(path):
    """Minimal KEY=VALUE parser (stdlib only, '#' comments, ~ expansion)."""
    values = {}
    path = Path(os.path.expanduser(str(path)))
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = os.path.expanduser(value.strip())
    return values


@dataclass
class Config:
    port: int = DEFAULT_PORT
    plugin_dir: str = str(DEFAULT_PLUGIN_DIR)
    site_packages: str = ""
    autoload: bool = True   # GDBRPC_AUTOLOAD: load all discovered
                             # plugins immediately at gdb startup


def _env_flag(value, default=True):
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def load_config(env_file=ENV_FILE):
    values = parse_env_file(env_file)
    # environment variables override .env (dev/test convenience)
    for key in ("GDBRPC_PORT", "GDBRPC_PLUGIN_DIR", "GDBRPC_AUTOLOAD"):
        if os.environ.get(key):
            values[key] = os.environ[key]
    return Config(
        port=int(values.get("GDBRPC_PORT", str(DEFAULT_PORT))),
        plugin_dir=values.get("GDBRPC_PLUGIN_DIR", str(DEFAULT_PLUGIN_DIR)),
        site_packages=values.get("GDBRPC_SITE_PACKAGES", ""),
        autoload=_env_flag(values.get("GDBRPC_AUTOLOAD")),
    )