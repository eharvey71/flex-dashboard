import json, os

ROOT = os.path.dirname(os.path.abspath(__file__))


def config_file(profile=None):
    return os.path.join(ROOT, "config.json" if not profile
                        else "config.%s.json" % profile)


def load(profile=None):
    path = config_file(profile)
    if not os.path.exists(path):
        raise SystemExit("No %s." % os.path.basename(path))
    with open(path) as f:
        cfg = json.load(f)
    cfg.setdefault("profile", profile or "work")
    return cfg


def path(*parts):
    return os.path.join(ROOT, *parts)


def data_path(cfg, day, suffix="json"):
    """Each profile keeps its own data directory so the two never collide."""
    d = path(cfg.get("output", {}).get("data_dir", "data"))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "%s.%s" % (day, suffix))
