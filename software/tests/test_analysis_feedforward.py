import pytest

from polydriver.analysis.feedforward import (
	FeedforwardFitError, SweepRow, fit_feedforward, load_sweep,
)
from polydriver.bench.sweep import SWEEP_COLUMNS


def line_rows(duties, offset=128.0, gain=1.5):
	return [SweepRow(duty=duty, mean_raw_hz=(duty - offset) / gain, samples=10, stalled_samples=0) for duty in duties]


def standing(duty):
	return SweepRow(duty=duty, mean_raw_hz=0.0, samples=10, stalled_samples=10)


def test_a_straight_line_gives_back_its_offset_and_gain():
	fit = fit_feedforward(line_rows(range(140, 256, 10)))
	assert fit.ff_offset_duty == pytest.approx(128.0)
	assert fit.ff_gain_duty_per_hz == pytest.approx(1.5)
	assert fit.r_squared == pytest.approx(1.0)
	assert fit.points == 12


def test_rows_where_the_screw_stood_still_are_left_out():
	rows = [standing(100), standing(120)] + line_rows(range(140, 256, 10))
	fit = fit_feedforward(rows)
	assert fit.start_duty == 140
	assert fit.points == 12
	assert fit.ff_offset_duty == pytest.approx(128.0)


def test_a_row_with_some_stalled_polls_is_left_out():
	partly = SweepRow(duty=135, mean_raw_hz=1.0, samples=10, stalled_samples=4)
	fit = fit_feedforward([partly] + line_rows(range(140, 200, 10)))
	assert fit.start_duty == 140
	assert fit.points == 6


def test_the_start_duty_is_the_lowest_turning_duty_in_any_order():
	rows = list(reversed(line_rows(range(150, 210, 10)))) + [standing(140)]
	assert fit_feedforward(rows).start_duty == 150


def test_scatter_lowers_r_squared():
	rows = line_rows(range(140, 220, 10))
	rows[3] = SweepRow(duty=rows[3].duty, mean_raw_hz=rows[3].mean_raw_hz + 5, samples=10, stalled_samples=0)
	assert fit_feedforward(rows).r_squared < 0.99


def test_too_few_turning_rows_is_refused():
	with pytest.raises(FeedforwardFitError, match="at least 3"):
		fit_feedforward([standing(100)] + line_rows([140, 150]))


def test_all_turning_at_one_speed_is_refused():
	rows = [SweepRow(duty=duty, mean_raw_hz=10.0, samples=10, stalled_samples=0) for duty in (140, 150, 160)]
	with pytest.raises(FeedforwardFitError, match="speed did not change"):
		fit_feedforward(rows)


def test_a_speed_that_falls_with_the_duty_is_refused():
	rows = line_rows(range(140, 200, 10), gain=-1.5, offset=250)
	with pytest.raises(FeedforwardFitError, match="falls as the duty rises"):
		fit_feedforward(rows)


def test_an_offset_outside_the_duty_range_is_refused():
	with pytest.raises(FeedforwardFitError, match="offset .* outside 0-255"):
		fit_feedforward(line_rows(range(140, 200, 10), offset=-50.0, gain=1.0))


def test_report_names_the_numbers():
	text = fit_feedforward(line_rows(range(140, 256, 10))).report()
	assert text == "start duty 140 · offset 128.0 · gain 1.500 duty/Hz · R² 1.000 (12 steps)"


def write_sweep(path, rows):
	lines = [",".join(SWEEP_COLUMNS)]
	for row in rows:
		lines.append(f"{row[0]},{row[1]},0.1,0,0,{row[1]},{row[2]},{row[3]}")
	path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_a_sweep_csv_loads_as_rows(tmp_path):
	path = tmp_path / "sweep.csv"
	write_sweep(path, [(130, 0.0, 10, 10), (140, 8.0, 10, 0)])
	assert load_sweep(path) == [SweepRow(130, 0.0, 10, 10), SweepRow(140, 8.0, 10, 0)]


def test_a_csv_without_the_sweep_columns_is_refused(tmp_path):
	path = tmp_path / "other.csv"
	path.write_text("t_s,polymer_raw_hz\n0,1\n", encoding="utf-8")
	with pytest.raises(FeedforwardFitError, match="not a sweep file.*duty"):
		load_sweep(path)


def test_a_bad_number_names_its_line(tmp_path):
	path = tmp_path / "sweep.csv"
	write_sweep(path, [(130, 0.0, 10, 10)])
	path.write_text(path.read_text(encoding="utf-8") + "abc,1,0,0,0,1,10,0\n", encoding="utf-8")
	with pytest.raises(FeedforwardFitError, match="line 3"):
		load_sweep(path)


def test_a_step_at_0_hz_is_not_turning_even_without_stalled_polls():
	rows = [SweepRow(130, 0.0, 10, 0)] + line_rows(range(140, 200, 10))
	assert fit_feedforward(rows).start_duty == 140


def test_a_gently_falling_speed_is_refused_too():
	rows = [SweepRow(duty, (250 - duty) * 2.0, 10, 0) for duty in range(140, 200, 10)]
	with pytest.raises(FeedforwardFitError, match="falls as the duty rises"):
		fit_feedforward(rows)
