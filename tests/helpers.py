"""Shared test helpers: fixture loading, config building, and a sandboxed main()."""

import collections
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIX = os.path.join(HERE, "fixtures")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import zelda_watch as z  # noqa: E402

Run = collections.namedtuple("Run", "rc sent state out dir")


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()


def repo_json(name):
    with open(os.path.join(ROOT, name), encoding="utf-8-sig") as f:
        return json.load(f)


def make_cfg(products, **over):
    """A minimal valid config: one person, the usual stores, no extras."""
    cfg = {
        "version": 2,
        "notifications": {"people": ["julie"], "status_alerts_to": ["julie"]},
        "approved_stores": ["nintendo", "bestbuy-ca", "amazon", "walmart"],
        "custom_stores": [],
        "products": products,
        "heartbeat_hours": 0,
        "discovery": {"enabled": False},
        "politeness": {"request_spacing_seconds": 0},
    }
    cfg.update(over)
    return cfg


class WatcherTestCase(unittest.TestCase):
    """Captures pushes instead of sending them, and runs main() in a temp folder."""

    def setUp(self):
        self.sent = []
        self.patch(z, "notify", self._capture)
        z._cooldowns.clear()
        z._last_request.clear()

    def _capture(self, topic, server, title, body, *args, **kwargs):
        self.sent.append((topic, title, body))
        return True

    def patch(self, obj, name, value):
        old = getattr(obj, name)
        setattr(obj, name, value)
        self.addCleanup(setattr, obj, name, old)

    def set_env(self, **values):
        for k, v in values.items():
            old = os.environ.get(k)
            self.addCleanup(self._restore_env, k, old)
            os.environ[k] = v

    @staticmethod
    def _restore_env(key, old):
        if old is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old

    def run_main(self, cfg, state=None, argv=(), env=None, keep_dir=None):
        """
        Run the watcher once against `cfg` (and `state`, if given). Defaults to
        looking like a GitHub Actions run with one topic. Returns a Run.
        """
        tmp = keep_dir
        if tmp is None:
            tmp = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, tmp, True)
        self.patch(z, "CONFIG_PATH", os.path.join(tmp, "config.json"))
        self.patch(z, "STATE_PATH", os.path.join(tmp, "state.json"))
        with open(z.CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        if state is not None:
            with open(z.STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(state, f)
        values = {"GITHUB_ACTIONS": "true", "NTFY_TOPIC": "topic-julie", "NTFY_TOPICS": "",
                  "DRY_RUN": "", "GITHUB_REPOSITORY": ""}
        values.update(env or {})
        self.set_env(**values)
        self.patch(sys, "argv", ["zelda_watch.py"] + list(argv))
        del self.sent[:]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = z.main()
        new_state = None
        if os.path.exists(z.STATE_PATH):
            with open(z.STATE_PATH, encoding="utf-8") as f:
                new_state = json.load(f)
        return Run(rc, list(self.sent), new_state, out.getvalue(), tmp)
