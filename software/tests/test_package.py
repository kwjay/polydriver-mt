from pathlib import Path

import polydriver


def test_package_has_a_version():
	assert polydriver.__version__ == "0.1.0"


def test_package_is_the_rebuilt_one_not_legacy():
	assert Path(polydriver.__file__).parent.name == "polydriver"
	assert "legacy" not in Path(polydriver.__file__).parts