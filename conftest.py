"""Root conftest: load the shared fixtures for every workspace member's tests.

``pytest_plugins`` has to be declared in the rootdir conftest — pytest refuses it in a
nested one — so this file exists to do exactly that. The fixtures themselves live in
:mod:`aria_core.testing`, defined once rather than copied into each package's tests.
"""

from __future__ import annotations

pytest_plugins = ["aria_core.testing"]
