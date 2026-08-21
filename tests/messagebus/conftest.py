"""Fixtures for the S8 MessageBus suite.

Vocabulary confinement note: see ``_env.py``'s docstring. This file knows the
control plane only through :mod:`tests.messagebus._env` and exposes it as
fixtures so that ``test_stale_readout.py`` can drive the bus without importing
the control plane itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest

from ._env import BusEnv, make_bus_env


@pytest.fixture
def bus_env_factory(tmp_path: Path) -> Callable[[str], BusEnv]:
    created: list[BusEnv] = []

    def make(tag: str) -> BusEnv:
        env = make_bus_env(tmp_path, tag)
        created.append(env)
        return env

    yield make
    for env in created:
        env.close()


@pytest.fixture
def bus_env(bus_env_factory) -> BusEnv:
    return bus_env_factory("main")
