from polydriver.calibration import load_calibration
from polydriver.config import load_config
from polydriver.gui.screenshots import write_demo
from polydriver.jobs import check_job, compile_gradient_job
from polydriver.jobs.editing import list_jobs
from polydriver.materials import load_materials


def test_the_demo_files_make_a_job_that_runs(tmp_path):
	config = load_config(write_demo(tmp_path))
	materials = load_materials(config.resolve(config.paths.materials))
	store = load_calibration(config.resolve(config.paths.calibration))
	(entry,) = list_jobs(config.resolve(config.paths.jobs))
	assert check_job(entry.job, materials, store).ok
	compile_gradient_job(entry.job, materials, store)
	assert config.boards[next(iter(config.boards))].has_gains
