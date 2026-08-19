"""Smoke test: the package resolves from the workspace.

Cheap, but it is what catches a broken workspace member before a slice starts
writing into it.
"""

import jutsu_evals


def test_package_imports() -> None:
    assert jutsu_evals.__doc__
