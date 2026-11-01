from polydriver.calibration import CalibrationStore, curve_warnings


def store_with(*rows):
	store = CalibrationStore()
	curve = None
	for speed_hz, grams, seconds in rows:
		curve = store.add_measurement("B", "PP", speed_hz, grams, seconds)
	return curve


def test_a_clean_curve_has_no_warnings():
	curve = store_with((10, 10, 60), (20, 21, 60), (20, 21.5, 60))
	assert curve_warnings(curve, ["PP"]) == []


def test_repeats_that_disagree_by_more_than_ten_percent_are_named():
	curve = store_with((10, 10, 60), (10, 12, 60))
	assert curve_warnings(curve) == [
		"the 2 weighings at 10 Hz disagree by 18 % (more than 10 %): weigh that speed again"]


def test_a_single_weighing_or_a_steady_zero_is_never_noisy():
	curve = store_with((5, 0, 60), (5, 0, 60), (10, 10, 60))
	assert curve_warnings(curve) == []


def test_a_falling_curve_is_named():
	curve = store_with((10, 12, 60), (20, 10, 60))
	warnings = curve_warnings(curve)
	assert len(warnings) == 1 and "10 Hz" in warnings[0] and "20 Hz" in warnings[0]


def test_one_speed_cannot_fall():
	assert curve_warnings(store_with((10, 12, 60))) == []


def test_a_material_missing_from_the_materials_file_is_named():
	curve = store_with((10, 10, 60))
	assert curve_warnings(curve, ["PLA", "TPU"]) == [
		"PP is not in the materials file (there: PLA, TPU); a job can only use a material that is"]
	assert curve_warnings(curve, []) == [
		"PP is not in the materials file (there: none yet); a job can only use a material that is"]


def test_without_names_the_material_is_not_checked():
	assert curve_warnings(store_with((10, 10, 60)), None) == []
