import logging

from polydriver.config import load_config
from polydriver.devices import Session
from polydriver.protocol.constants import Role
from polydriver.startup import LOG_FILE_NAME, log_to_file, simulate


def test_simulate_names_its_own_ports_where_the_config_names_none(tmp_path):
	config, _ = simulate(load_config(tmp_path / "missing.toml"))
	assert config.ports == {Role.POLYMER: "SIM-POLYMER", Role.COLOUR: "SIM-COLOUR"}


def test_simulate_keeps_the_ports_the_config_names(tmp_path):
	path = tmp_path / "polydriver.toml"
	path.write_text('[ports]\npolymer = "COM6"\n', encoding="utf-8")
	config, _ = simulate(load_config(path))
	assert config.ports == {Role.POLYMER: "COM6", Role.COLOUR: "SIM-COLOUR"}


def test_simulate_changes_nothing_but_the_ports(tmp_path):
	path = tmp_path / "polydriver.toml"
	path.write_text("[safety]\ncomms_timeout_s = 3\n", encoding="utf-8")
	original = load_config(path)
	config, _ = simulate(original)
	assert config.comms_timeout_s == 3
	assert config.base_dir == original.base_dir


def test_simulated_boards_connect_with_their_roles(tmp_path):
	config, link_factory = simulate(load_config(tmp_path / "missing.toml"))
	with Session(config, link_factory=link_factory) as session:
		assert session.connect(Role.POLYMER).port == "SIM-POLYMER"
		assert session.connect(Role.COLOUR).port == "SIM-COLOUR"


def test_log_to_file_writes_into_the_config_folder(tmp_path):
	handler = log_to_file(load_config(tmp_path / "missing.toml"))
	try:
		logging.getLogger("polydriver.test").warning("into the program log")
		handler.flush()
		assert handler in logging.getLogger().handlers
		text = (tmp_path / "logs" / LOG_FILE_NAME).read_text(encoding="utf-8")
		assert "into the program log" in text
	finally:
		logging.getLogger().removeHandler(handler)
		handler.close()
