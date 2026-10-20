"""Pytest configuration: live (network) tests only run with ``--live``."""

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="run tests that hit live network sources (bourse, TradingView, Medias24)",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "live: hits live network sources; run with pytest --live"
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live"):
        return
    skip_live = pytest.mark.skip(reason="needs --live flag (network access)")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
