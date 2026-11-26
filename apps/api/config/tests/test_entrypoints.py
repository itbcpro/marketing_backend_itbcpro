import subprocess
import sys

SNIPPET = """
import os
os.environ.pop("DJANGO_SETTINGS_MODULE", None)
import config
print(os.environ.get("DJANGO_SETTINGS_MODULE", "unset"))
"""


def test_importing_the_package_never_selects_a_settings_module() -> None:
    """Regression: celery.py once defaulted to dev settings, overriding the prod entry points."""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", SNIPPET], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "unset"
