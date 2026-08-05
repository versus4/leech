"""Roster hygiene for the hand-maintained model list.

use.ai exposes no endpoint that lists its models, so the roster in config stays
manual and goes stale whenever they change it. What this module can still do is
stop a stale entry from failing silently: the blacklist keeps known-useless models
out of the picker, and validate() proves every alias points at a slug that is
actually in MODELS instead of quietly resolving to the default.

To check the roster against the live gateway, stream one message per slug and see
which answer -- there is no list to fetch, only models to try.
"""
import logging
import re

from . import config

log = logging.getLogger("catalog")

BLACKLIST = {
    "claude-fable-5",
}
BLACKLIST_PATTERNS = [
    re.compile(r"^claude-fable-"),
    re.compile(r"(?:^|-)(?:embed|embedding|tts|whisper|moderation|rerank)(?:-|$)"),
]


def is_blacklisted(slug):
    if slug in BLACKLIST:
        return True
    return any(p.search(slug) for p in BLACKLIST_PATTERNS)


def validate():
    """Drop blacklisted models, then repoint DEFAULT_MODEL and every alias at a
    slug that exists. Returns the list of problems found so a stale roster shows
    up in the log at startup instead of as a mystery fallback at request time."""
    problems = []
    kept = [m for m in config.MODELS if not is_blacklisted(m["slug"])]
    for m in config.MODELS:
        if is_blacklisted(m["slug"]):
            problems.append("blacklisted model %s removed from roster" % m["slug"])
    config.MODELS = kept
    slugs = {m["slug"] for m in kept}
    config._MODEL_SLUGS = slugs

    if config.DEFAULT_MODEL not in slugs:
        old = config.DEFAULT_MODEL
        config.DEFAULT_MODEL = _first_present(slugs, config.PREFERRED_DEFAULTS) or (
            kept[0]["slug"] if kept else old)
        problems.append("default %s not in roster -> %s" % (old, config.DEFAULT_MODEL))

    for alias, prefs in config.ALIAS_PREFERENCES.items():
        current = config.MODEL_ALIASES.get(alias)
        if current in slugs:
            continue
        pick = _first_present(slugs, prefs) or config.DEFAULT_MODEL
        config.MODEL_ALIASES[alias] = pick
        problems.append("alias %r pointed at %r which is not in the roster -> %s"
                        % (alias, current, pick))

    config.MODEL_MAP = {**config.MODEL_ALIASES, **{s: s for s in slugs}}
    for p in problems:
        log.warning("catalog: %s", p)
    return problems


def _first_present(slugs, preferences):
    for want in preferences:
        if want in slugs:
            return want
    for want in preferences:
        for s in slugs:
            if s.startswith(want):
                return s
    return None
