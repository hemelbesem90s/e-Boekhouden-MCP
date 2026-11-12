"""Pytest global configuration and fixtures."""

from collections.abc import Generator
import pytest

from eboekhouden_mcp.biometric_guard import clear_biometric_cache


@pytest.fixture(autouse=True)
def reset_biometric_cache() -> Generator[None, None, None]:
    """Ensure biometric cache is reset before and after every test."""
    clear_biometric_cache()
    yield
    clear_biometric_cache()
