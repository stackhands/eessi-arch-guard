"""Pytest-visible wrappers for unit checks kept in test_unit.UnitTests."""

from test_unit import UnitTests


def test_read_regular_bytes_rejects_fifo_and_directory():
    UnitTests().test_read_regular_bytes_rejects_fifo_and_directory()
