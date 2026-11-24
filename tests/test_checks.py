"""Automated checks, including the cases requested in the brief."""

from __future__ import annotations

import json
import math
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from transport.capacity import capacity_for, classify_credible  # noqa: E402
from transport.common import hms_to_sec  # noqa: E402
from transport.gtfs import load_network  # noqa: E402
from transport.normalize import route_direction  # noqa: E402
from transport.loads import in_window, inclusive_window, segment_loads  # noqa: E402
from transport.model import Aggregated, HistoricalModel, Profile, pooled_average  # noqa: E402
from transport.planner import Planner  # noqa: E402

DB = ROOT / "output" / "transport.db"


class LoadChecks(unittest.TestCase):
    def test_overlap_example(self):
        loads = segment_loads([(1, 10, 10), (1, 2, 5), (2, 4, 3)])
        self.assertEqual(loads[1], 15)
        self.assertEqual(loads[2], 13)
        self.assertEqual(loads[3], 13)
        self.assertEqual(loads[9], 10)
        self.assertEqual(len(loads), 10)
        self.assertEqual(segment_loads([(1, 2, 5), (2, 4, 3)])[0:4], [0, 5, 3, 3])

    def test_two_minute_inclusive_window(self):
        lo, hi = inclusive_window(hms_to_sec("08:40"), 2)
        self.assertEqual((lo, hi), (hms_to_sec("08:38:00"), hms_to_sec("08:42:00")))
        self.assertTrue(in_window(hms_to_sec("08:38:00"), hms_to_sec("08:40")))
        self.assertTrue(in_window(hms_to_sec("08:42:00"), hms_to_sec("08:40")))
        self.assertFalse(in_window(hms_to_sec("08:37:59"), hms_to_sec("08:40")))
        self.assertFalse(in_window(hms_to_sec("08:42:01"), hms_to_sec("08:40")))

    def test_60_25_15_pooled(self):
        runs = [[0] * 6 for _ in range(3)]
        for loads, pax in zip(runs, (60, 25, 15)):
            loads[2] = pax
        out = pooled_average(runs, 2, 3, 3, "observed")
        self.assertEqual(out["individual_recorded_passengers"], [60, 25, 15])
        self.assertEqual(out["total_recorded_passengers"], 100)
        self.assertAlmostEqual(out["average_recorded_per_departure"], 100 / 3)
        self.assertTrue(out["pooled_hides_individual_differences"])

    def test_missing_capacity(self):
        cap = capacity_for(None, 1, None)
        self.assertEqual(cap.mode, "recorded_only")
        self.assertIsNone(cap.seats)
        self.assertEqual(classify_credible(10, cap), "unknown")


class DataChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = sqlite3.connect(DB)
        cls.net = load_network(cls.conn)

    def test_greater_than_two_options_demo(self):
        opts, info = self.net.options("Tara Apartment", "Punj Sons / Govind Puri Metro Station",
                                      target_sec=hms_to_sec("08:40"), window_min=2, date_iso="2026-10-23")
        self.assertEqual(len(opts), 3)
        self.assertEqual(info["board_match"], "exact")

    def test_munirka_to_iit_gate_actual_number(self):
        opts, info = self.net.options("Munirka Village (T)", "IIT Gate",
                                      target_sec=hms_to_sec("08:40"), window_min=2, date_iso="2026-10-23")
        self.assertEqual(len(opts), 0)
        self.assertEqual(info["alight_match"], "exact")
        all_day, _ = self.net.options("Munirka Village (T)", "IIT Gate", date_iso="2026-10-23")
        self.assertGreaterEqual(len(all_day), 3)
        absent, miss = self.net.options("IIIT Delhi", "IIT Gate")
        self.assertEqual(absent, [])
        self.assertEqual(miss["board_match"], "unmatched")

    def test_opposite_directions(self):
        fwd, _ = self.net.options("Munirka Village (T)", "IIT Gate", date_iso="2026-10-23")
        rev, _ = self.net.options("IIT Gate", "Munirka Village (T)", date_iso="2026-10-23")
        self.assertTrue(fwd and rev)
        self.assertFalse({o.trip_id for o in fwd} & {o.trip_id for o in rev})
        self.assertTrue({o.route_name for o in fwd} - {o.route_name for o in rev})
        major_down = [o for o in fwd if o.route_name.endswith("DOWN")]
        self.assertTrue(major_down)
        self.assertFalse(any(o.route_name.endswith("DOWN") and o.trip_id in
                             {x.trip_id for x in rev} for o in major_down))

    def test_repeated_trips_one_vehicle(self):
        rows = self.conn.execute(
            "SELECT vehicle,date,route_name,count(distinct trip_id) n FROM run_info "
            "WHERE vehicle IS NOT NULL GROUP BY 1,2,3 HAVING n>=2 LIMIT 1").fetchall()
        self.assertTrue(rows)
        rows2 = self.conn.execute(
            "SELECT count(*) FROM run_info WHERE vehicle=? AND date=? AND route_name=?",
            rows[0][:3]).fetchone()
        self.assertGreaterEqual(rows2[0], 2)


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.planner = Planner(DB)

    def test_real_journey(self):
        result = self.planner.plan("Tara Apartment", "Punj Sons / Govind Puri Metro Station",
                                   "2026-10-23", "08:40")
        self.assertEqual(len(result["options"]), 3)
        self.assertEqual(result["distinct_departures_in_window"], 3)
        for opt in result["options"]:
            self.assertTrue(opt["stops"])
            self.assertTrue(math.isfinite(opt["expected_recorded_load"]))
        self.assertIsNotNone(result["later_comparison"])
        self.assertEqual(result["capacity_mode"], "recorded_only")


class AliasAndCompatChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = sqlite3.connect(DB)
        cls.aliases = json.loads((ROOT / "transport" / "aliases.json").read_text(encoding="utf-8"))
        cls.net = load_network(cls.conn, ROOT / "transport" / "aliases.json")

    def test_curated_aliases_resolve_to_real_stops(self):
        available = {row[0] for row in self.conn.execute("SELECT DISTINCT stop_name FROM stops")}
        for raw, target in self.aliases.items():
            self.assertIn(target, available, raw)
            match = self.net.matcher.match_stop(raw)
            self.assertEqual(match.label, "verified", raw)
            self.assertTrue(match.ids, raw)

    def test_no_alias_backward_compatibility(self):
        plain = load_network(self.conn)
        self.assertTrue(plain.matcher.match_stop("Tara Apartment").ids)
        self.assertEqual(self.net.matcher.match_stop("Tara Apartment").ids,
                         plain.matcher.match_stop("Tara Apartment").ids)

    def test_direction_and_clock_back_compat(self):
        self.assertEqual(hms_to_sec("08:40"), hms_to_sec("08:40:00"))
        self.assertTrue(route_direction("717BDOWN").endswith(":DOWN"))
        self.assertTrue(route_direction("543UP").endswith(":UP"))

    def test_optional_capacity_map_enables_credible_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "map.json"
            path.write_text(json.dumps({"DL51EV9844":
                "JBM CITYLIFE, Delhi low-floor AC cluster configuration (2020)"}), encoding="utf-8")
            cap = capacity_for("DL51EV9844", 1, path)
            self.assertEqual(cap.mode, "credible")
            self.assertEqual(cap.seats, 35)
            self.assertEqual(classify_credible(36, cap), "standing_range")
            self.assertEqual(capacity_for("UNKNOWN", 1, path).mode, "recorded_only")


def fake_model(profiles=None, day_profiles=None, route_medians=None, route_support=None):
    model = HistoricalModel.__new__(HistoricalModel)
    model.bin_minutes = 30
    model.shrinkage_k = 0.0
    model.agg = Aggregated({}, {}, route_medians or {}, route_support or {},
                           profiles or {}, {}, day_profiles or {}, {}, [])
    return model


class StretchModelChecks(unittest.TestCase):
    def test_same_hour_two_positions_are_different(self):
        profiles = {
            ("P", "weekday", 20, 2): Profile(("P", "weekday", 20, 2), 12, 6, 10, 12, 14),
            ("P", "weekday", 20, 3): Profile(("P", "weekday", 20, 3), 12, 6, 35, 38, 42),
        }
        model = fake_model(profiles)
        at_two = model.predict_stretch("P", "weekday", 2, hms_to_sec("10:05"), "R")
        at_three = model.predict_stretch("P", "weekday", 3, hms_to_sec("10:05"), "R")
        self.assertEqual(at_two.load, 10)
        self.assertEqual(at_three.load, 35)
        self.assertEqual(at_two.method, "exact_stretch")
        self.assertEqual(at_three.method, "exact_stretch")

    def test_midnight_fallback_does_not_use_wrong_clock_half_hour(self):
        profiles = {
            ("P", "weekday", 47, 5): Profile(("P", "weekday", 47, 5), 8, 4, 7, 8, 9),
            ("P", "weekday", 18, 5): Profile(("P", "weekday", 18, 5), 20, 10, 99, 99, 99),
        }
        model = fake_model(profiles)
        prediction = model.predict_stretch("P", "weekday", 5, hms_to_sec("00:10"), "R")
        self.assertEqual(prediction.load, 7)
        self.assertEqual(prediction.method, "nearby_stretch")

    def test_weekday_weekend_midnight_boundary(self):
        profiles = {
            ("P", "weekend", 47, 5): Profile(("P", "weekend", 47, 5), 8, 4, 4, 5, 6),
        }
        model = fake_model(profiles)
        prediction = model.predict_stretch("P", "weekday", 5, hms_to_sec("00:10"), "R")
        self.assertEqual(prediction.load, 4)
        self.assertEqual(prediction.method, "adjacent_day_type_boundary")

    def test_zero_load_and_no_evidence_are_different(self):
        day_profiles = {
            ("P", "weekday", 5): Profile(("P", "weekday", 5), 8, 4, 0, 0, 0),
        }
        model = fake_model(day_profiles=day_profiles, route_medians={"R": 6},
                           route_support={"R": 100})
        zero = model.predict_stretch("P", "weekday", 5, hms_to_sec("10:00"), "R")
        missing = model.predict_stretch("P", "weekday", 6, hms_to_sec("10:00"), "R")
        self.assertEqual(zero.load, 0)
        self.assertTrue(zero.evidence_based)
        self.assertEqual(missing.load, 6)
        self.assertFalse(missing.evidence_based)
        self.assertEqual(missing.status, "Limited data")


class ScenarioChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.planner = Planner(DB)

    def test_busy_corridor_reports_busiest_stretch(self):
        result = self.planner.plan("Badarpur Border (T)", "Kapashera Border",
                                   "2026-10-30", "06:35", 2)
        self.assertTrue(result["options"])
        option = result["options"][0]
        self.assertIsNotNone(option["expected_recorded_load"])
        self.assertIsNotNone(option["busiest_load"])
        self.assertGreaterEqual(option["busiest_load"], option["expected_recorded_load"])
        self.assertTrue(option["busiest_stretches"])

    def test_sparse_route_is_not_recommended_as_low_crowding(self):
        result = self.planner.plan("Jama Masjid", "Super Bazar", "2026-10-21", "00:16", 2)
        self.assertTrue(result["options"])
        option = result["options"][0]
        self.assertEqual(option["confidence"], "No data")
        self.assertIsNone(option["expected_recorded_load"])
        self.assertTrue(any(stop["status"] == "Limited data" and not stop["evidence_based"]
                            for stop in option["stops"]))

    def test_all_nearby_departures_left(self):
        result = self.planner.plan("Tara Apartment", "Punj Sons / Govind Puri Metro Station",
                                   "2026-10-23", "24:30", 0)
        self.assertTrue(result["all_nearby_departures_left"])
        self.assertEqual(len(result["options"]), 1)
        self.assertFalse(result["options"][0]["boarding_recommended"])

    def test_platform_ids_and_history_label(self):
        result = self.planner.plan("Tara Apartment", "Punj Sons / Govind Puri Metro Station",
                                   "2026-11-02", "08:40", 2)
        self.assertTrue(result["options"])
        option = result["options"][0]
        self.assertTrue(option["board_platform"])
        self.assertTrue(option["alight_platform"])
        self.assertEqual(result["query"]["data_vintage"], "Based on October 2026 ticket history")

    def test_misspelled_stop_still_matches(self):
        match = self.planner.net.matcher.match_stop("Tara Apatment")
        self.assertTrue(match.ids)
        self.assertEqual(match.label, "verified")


if __name__ == "__main__":
    unittest.main(verbosity=2)
