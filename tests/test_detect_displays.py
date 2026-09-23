#!/usr/bin/env python3

from __future__ import annotations

import tempfile
import unittest
import sys
from pathlib import Path

from openpyxl import Workbook

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from detect_displays import analyse_workbook


def _row(**values):
    columns = {
        "a": 1,
        "b": 2,
        "c": 3,
        "d": 4,
        "e": 5,
        "f": 6,
        "g": 7,
        "h": 8,
        "i": 9,
        "j": 10,
        "o": 15,
        "p": 16,
        "q": 17,
        "r": 18,
    }
    result = [None] * 18
    for name, value in values.items():
        result[columns[name] - 1] = value
    return result


class DisplayDetectionTests(unittest.TestCase):
    def _analyse(self, rows):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "larice.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "Canvass test"
            for row in rows:
                sheet.append(row)
            workbook.save(path)
            workbook.close()
            return analyse_workbook(path)

    def test_solbao_top_performer_x96_is_a_high_confidence_display(self):
        name = "SOLBAO ESPO TOP PERFORMER X 96"
        prices = [7.31] * 3 + [5.75] * 4 + [6.84] + [7.31] * 2 + [6.84] * 2 + [7.31] + [7.88] + [6.84] + [7.88]
        rows = [
            _row(a=name, d=name),
            _row(a=name, b="I", c=28246, e=1, f=1, g=name, o=659.88, p=0.33, q=22, r=""),
        ]
        for index, price in enumerate(prices, start=1):
            rows.append(
                _row(
                    a=name,
                    h=6,
                    i=f"SOLBAO COMPONENTE {index}",
                    o=price,
                    p=0.33,
                    r=str(8009380105345 + index),
                )
            )

        result = self._analyse(rows)

        self.assertEqual(result["display_offer_count"], 1)
        offer = result["display_offers"][0]
        self.assertEqual(offer["record_type"], "DISPLAY_OFFER")
        self.assertEqual(offer["supplier_code"], "28246")
        self.assertEqual(offer["declared_quantity"], 96)
        self.assertEqual(offer["sum_pieces"], 96)
        self.assertTrue(offer["sum_pieces_complete"])
        self.assertEqual(offer["parent_price_pre_discount"], "659.8800")
        self.assertEqual(offer["parent_price_post_discount"], "442.1196")
        self.assertEqual(offer["component_extended_sum_pre_discount"], "659.8800")
        self.assertEqual(offer["component_parent_price_match_basis"], "parent_price_cell")
        self.assertEqual(offer["confidence"], "HIGH")
        self.assertGreaterEqual(offer["score"], 10)
        self.assertTrue(offer["composition_fingerprint"].startswith("sha256:"))
        self.assertEqual(offer["source_rows"]["header_row"], 1)
        self.assertEqual(offer["source_rows"]["parent_row"], 2)
        self.assertEqual(offer["source_rows"]["component_rows"], list(range(3, 19)))
        self.assertEqual(result["orderable_rows"], [2])
        self.assertEqual(result["component_rows_excluded_from_orderable"], list(range(3, 19)))
        row_types = {row["source_row"]: row["row_type"] for row in result["row_classifications"]}
        self.assertEqual(row_types[1], "HEADER")
        self.assertEqual(row_types[2], "PARENT")
        self.assertTrue(all(row_types[row] == "COMPONENT" for row in range(3, 19)))

    def test_explicit_no_espo_is_rejected_even_when_quantity_and_price_reconcile(self):
        name = "SOLARI NEVAL CASSA MISTA X 57 ( NO ESPO )"
        rows = [
            _row(a=name, d=name),
            _row(a=name, b="I", c=22722, e=1, f=1, g="NEVAL SUN CASSA MISTA X 57", o=114, p="TP", q=22),
            _row(a=name, h=19, i="NEVAL COMPONENTE A", o=2, p="TP", r="4005900000001"),
            _row(a=name, h=19, i="NEVAL COMPONENTE B", o=2, p="TP", r="4005900000002"),
            _row(a=name, h=19, i="NEVAL COMPONENTE C", o=2, p="TP", r="4005900000003"),
        ]

        result = self._analyse(rows)

        self.assertEqual(result["display_offer_count"], 0)
        self.assertEqual(len(result["rejected_bundle_candidates"]), 1)
        rejected = result["rejected_bundle_candidates"][0]
        self.assertEqual(rejected["confidence"], "REJECTED")
        self.assertFalse(rejected["display_detected"])
        self.assertIn("NEGATED_DISPLAY_TERM", {item["code"] for item in rejected["evidence"]})
        self.assertEqual(result["orderable_rows"], [2])
        self.assertEqual(result["component_rows_excluded_from_orderable"], [3, 4, 5])

    def test_espresso_multipack_and_unlabelled_assortment_are_not_displays(self):
        espresso = "CAFFE ESPRESSO X 12"
        assortment = "REGALO CASSA MISTA X 12"
        rows = [
            _row(a=espresso, b="I", c=101, e=12, g=espresso, o=2.5, p=0.1, r="8000000000001"),
            _row(a=assortment, d=assortment),
            _row(a=assortment, b="I", c=102, e=1, g=assortment, o=24, p=0.1),
            _row(a=assortment, h=4, i="REGALO A", o=2, p=0.1, r="8000000000018"),
            _row(a=assortment, h=4, i="REGALO B", o=2, p=0.1, r="8000000000025"),
            _row(a=assortment, h=4, i="REGALO C", o=2, p=0.1, r="8000000000032"),
        ]

        result = self._analyse(rows)

        self.assertEqual(result["display_offer_count"], 0)
        self.assertEqual(len(result["rejected_bundle_candidates"]), 1)
        self.assertEqual(result["row_classifications"][0]["row_type"], "STANDARD")
        self.assertTrue(result["row_classifications"][0]["orderable"])
        self.assertNotIn("EXPLICIT_DISPLAY_TERM", {item["code"] for item in result["rejected_bundle_candidates"][0]["evidence"]})

    def test_display_without_component_quantities_uses_parent_pack_but_stays_auditable(self):
        name = "DENTIFRICIO SENSODENT ESPO ASSORT"
        rows = [
            _row(a=name, d=name),
            _row(a=name, b="I", c=25927, e=72, f=1, g="ESPO SENSODENT ASSORT", o=2.9, p=0.25, q=22),
            _row(a=name, i="SENSODENT COMPLEX", r="5059905473735"),
            _row(a=name, i="SENSODENT GENTLE WHITE", r="5059345454691"),
            _row(a=name, i="SENSODENT FRESH CLEAN", r="5059833580611"),
        ]

        result = self._analyse(rows)

        self.assertEqual(result["display_offer_count"], 1)
        offer = result["display_offers"][0]
        self.assertEqual(offer["declared_quantity"], 72)
        self.assertEqual(offer["declared_quantity_source"], "parent_pieces_per_carton")
        self.assertIsNone(offer["sum_pieces"])
        self.assertFalse(offer["sum_pieces_complete"])
        self.assertEqual(offer["component_quantity_coverage"], "0/3")
        self.assertIsNone(offer["component_extended_sum_pre_discount"])

    def test_fingerprint_is_order_independent_and_ignores_supplier_wording_for_known_eans(self):
        name = "MARCA ESPO X 12"

        def fixture(first_description, second_description, reverse=False):
            components = [
                _row(a=name, h=6, i=first_description, o=2, p=0.1, r="8000000000018"),
                _row(a=name, h=6, i=second_description, o=2, p=0.1, r="8000000000025"),
            ]
            if reverse:
                components.reverse()
            return [
                _row(a=name, d=name),
                _row(a=name, b="I", c=500, e=1, g=name, o=24, p=0.1),
                *components,
            ]

        first = self._analyse(fixture("DESCRIZIONE FORNITORE A", "DESCRIZIONE FORNITORE B"))
        second = self._analyse(fixture("TESTO ABBREVIATO UNO", "TESTO ABBREVIATO DUE", reverse=True))

        self.assertEqual(
            first["display_offers"][0]["composition_fingerprint"],
            second["display_offers"][0]["composition_fingerprint"],
        )


if __name__ == "__main__":
    unittest.main()
