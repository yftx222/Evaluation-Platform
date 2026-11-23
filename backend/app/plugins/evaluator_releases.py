"""Installed historical builds. Only the HTTP client wiring changed between these two releases."""

from dataclasses import replace

from app.plugins.evaluators import BUILD, Plugin, Registry, registry

PHASE2_BUILD = "1d3eca819d47485a674484d1056474f9dd543fb3eb10681d0e05e6de5cc120ce"
OBSERVED_TRANSPORT_BUILD = "95ae95579232160a68b408b5f3a1bc7c0f1e282a94a673057fa63a9e558e481b"

historical = Registry()
# Both releases use identical grading code, configuration, prompts and verdict validation.
# A source change produces a different BUILD and cannot inherit these historical identities.
if BUILD == OBSERVED_TRANSPORT_BUILD:
    for plugin in registry.plugins.values():
        historical.register(replace(plugin, build=PHASE2_BUILD))


def installed(key: str, build: str) -> Plugin:
    return (historical if build == PHASE2_BUILD else registry).get(key, build)
