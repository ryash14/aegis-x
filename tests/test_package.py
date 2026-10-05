"""Verify that the distribution installs its intended import package."""

from importlib.metadata import distribution

import aegis


def test_installed_package() -> None:
    installed = distribution("aegis-x")

    assert installed.metadata["Name"] == "aegis-x"
    assert aegis.__name__ == "aegis"
