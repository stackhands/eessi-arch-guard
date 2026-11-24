"""Pytest-visible wrappers for unit checks kept in test_unit.UnitTests."""

from test_unit import UnitTests


def test_read_regular_bytes_rejects_fifo_and_directory():
    UnitTests().test_read_regular_bytes_rejects_fifo_and_directory()


def test_read_cpu_flags_rejects_fifo_and_directory():
    UnitTests().test_read_cpu_flags_rejects_fifo_and_directory()


def test_site_config_rechecks_non_regular_file_after_lstat():
    UnitTests().test_site_config_rechecks_non_regular_file_after_lstat()


def test_site_config_rechecks_owner_after_lstat():
    UnitTests().test_site_config_rechecks_owner_after_lstat()


def test_site_config_rechecks_mode_after_lstat():
    UnitTests().test_site_config_rechecks_mode_after_lstat()


def test_site_config_does_not_follow_path_after_lstat():
    UnitTests().test_site_config_does_not_follow_path_after_lstat()
