"""Test time limits that are generous in CI (copy of web-quality-kit
templates/ci_timeouts.py). A CI job may run on a slow or busy machine: with
CI=true no test limit is under 30 s, so a red test means a real error.

    from ci_timeouts import ci_timeout
    subprocess.run(cmd, timeout=ci_timeout(5))

With pytest-timeout, add to conftest.py (it raises the global limit from the
command line, PYTEST_TIMEOUT or the ini file; a mark on one test still wins,
so write it as @pytest.mark.timeout(ci_timeout(5))):

    from ci_timeouts import ci_timeouts_hook as pytest_configure
"""
import os

CI_FLOOR_S = 30


def in_ci():
    return os.environ.get("CI", "").lower() in ("true", "1")


def ci_timeout(local, floor=CI_FLOOR_S):
    """A time limit in seconds: `local` on a laptop, never under `floor` in
    CI. None and 0 stay "no limit"."""
    if not local or not in_ci():
        return local
    return max(local, floor)


def ci_timeouts_hook(config):
    """pytest_configure: raise pytest-timeout's global limit in CI."""
    if not in_ci():
        return
    set_to = getattr(config.option, "timeout", None)
    if set_to is None:
        set_to = os.environ.get("PYTEST_TIMEOUT")
    if set_to is None:
        try:
            set_to = config.getini("timeout")
        except (KeyError, ValueError):  # pytest-timeout is not installed
            return
    if set_to:
        config.option.timeout = ci_timeout(float(set_to))


# pytest-timeout reads its settings in its own pytest_configure: run before it.
try:
    import pytest
    ci_timeouts_hook = pytest.hookimpl(tryfirst=True)(ci_timeouts_hook)
except ImportError:
    pass
