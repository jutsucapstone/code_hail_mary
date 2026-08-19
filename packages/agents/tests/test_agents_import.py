"""Smoke test: the package resolves from the workspace.

Cheap, but it is what catches a broken workspace member before a slice starts
writing into it.
"""

import jutsu_agents


def test_package_imports() -> None:
    assert jutsu_agents.__doc__
