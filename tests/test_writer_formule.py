"""A formula in the order column must never reach the supplier.

The sheet already refuses formulas in cells it *writes* (`scriviNumero` and
`svuota` stop), but a formula on a row nobody orders was never touched: it
stayed in the copy, the cell-by-cell comparison saw formula against formula
and found no difference, and Excel recalculated it on open. The delivered
order then listed cartons nobody asked for.

This only happens in files *generated programmatically*: a `.xlsx` saved by
Excel always carries a cached result, and that alone stops the compilation.

The test price list is written by openpyxl and then has its empty `<v></v>`
stripped: openpyxl 3.1.5 writes the formula *with* an empty cached value,
which the writer reads as a quantity of zero and that alone would stop it
too. Without this adjustment the test would pass even with the defect
present.

The same gap exists in Noce's `.xls`, for a different reason: there the
order never reached the supplier, because the pre-compilation check scans
the whole column. It was `compila_ordine` alone that only checked half of
it.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Callable

from openpyxl import Workbook

SKILL_ROOT = Path(__file__).resolve().parents[1]
for cartella in (SKILL_ROOT / "app", SKILL_ROOT / "tests"):
    if str(cartella) not in sys.path:
        sys.path.insert(0, str(cartella))

import test_xls_reader as banco_xls  # noqa: E402
import test_xls_writer as banco_noce  # noqa: E402
import xls_writer  # noqa: E402
from test_writer_ordini import node_disponibile  # noqa: E402

WRITER = SKILL_ROOT / "scripts" / "write_supplier_orders.mjs"

# What openpyxl writes for `=5`: the formula plus an empty placeholder for
# the result. A management-software export that generates the price list
# never adds that placeholder.
_FORMULA_DI_OPENPYXL = "<f>5</f><v></v>"


def listino_con_una_formula(percorso: Path, ritocca: Callable[[str], str]) -> Path:
    """Test price list, three rows.

    | row | A (EAN)       | B (DESCRIZIONE) | C (order)                       |
    |-----|---------------|-----------------|----------------------------------|
    | 1   | EAN           | DESCRIZIONE     | ORDINE                           |
    | 2   | 8000000000002 | PRODOTTO DUE    | `=5` — not ordered by the plan   |
    | 3   | 8000000000003 | PRODOTTO TRE    | 4 — the row the plan orders      |
    """

    libro = Workbook()
    foglio = libro.active
    foglio.title = "Listino"
    foglio.append(["EAN", "DESCRIZIONE", "ORDINE"])
    foglio.append(["8000000000002", "PRODOTTO DUE", "=5"])
    foglio.append(["8000000000003", "PRODOTTO TRE", 4])
    libro.save(percorso)
    libro.close()

    with zipfile.ZipFile(percorso) as contenitore:
        parti = {nome: contenitore.read(nome) for nome in contenitore.namelist()}
    # With lxml openpyxl writes `<v></v>`; without it (Windows CI) `<v />`:
    # same empty element, and the patch below matches the first form.
    foglio_xml = parti["xl/worksheets/sheet1.xml"].decode("utf-8").replace("<f>5</f><v />", _FORMULA_DI_OPENPYXL)
    assert _FORMULA_DI_OPENPYXL in foglio_xml, foglio_xml
    parti["xl/worksheets/sheet1.xml"] = ritocca(foglio_xml).encode("utf-8")
    with zipfile.ZipFile(percorso, "w", zipfile.ZIP_DEFLATED) as contenitore:
        for nome, contenuto in parti.items():
            contenitore.writestr(nome, contenuto)
    return percorso


class UnaFormulaNellaColonnaDOrdine(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.node = node_disponibile()
        if cls.node is None:
            raise unittest.SkipTest("Node non disponibile per il writer XLSX")

    def setUp(self) -> None:
        temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(temporanea.cleanup)
        self.cartella = Path(temporanea.name)
        self.uscita = self.cartella / "uscita"

    def compila(self, ritocca: Callable[[str], str]) -> subprocess.CompletedProcess:
        sorgente = listino_con_una_formula(self.cartella / "listino.xlsx", ritocca)
        piano = self.cartella / "final_order_plan.json"
        piano.write_text(json.dumps({
            "orders": [{"supplier": "prova", "supplier_source_row": 3, "quantity": 7}],
        }), encoding="utf-8")
        config = self.cartella / "writer_config.json"
        config.write_text(json.dumps({
            "supplier_files": {"prova": str(sorgente)},
            "supplier_write_rules": {"prova": {
                "sheet": "Listino",
                "order_column": "C",
                "data_start_row": 2,
                "header_row": 1,
                "expected_header": "ORDINE",
                "source_sha256": hashlib.sha256(sorgente.read_bytes()).hexdigest(),
            }},
        }), encoding="utf-8")
        return subprocess.run(
            [
                str(self.node), str(WRITER),
                "--plan", str(piano),
                "--config", str(config),
                "--output-dir", str(self.uscita),
            ],
            cwd=SKILL_ROOT / "scripts",
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
        )

    def pretendi_che_si_fermi(self, esito: subprocess.CompletedProcess) -> None:
        detto = esito.stderr + esito.stdout
        self.assertNotEqual(esito.returncode, 0, detto)
        self.assertIn("formula", detto)
        self.assertIn("C2", detto)
        self.assertIn("non viene creato", detto)
        # No copy on disk: the order is never delivered half-done.
        prodotte = sorted(percorso.name for percorso in self.uscita.glob("*.xlsx")) if self.uscita.is_dir() else []
        self.assertEqual(prodotte, [], prodotte)

    def test_senza_il_valore_in_cache_e_su_una_riga_non_ordinata_ferma_lo_stesso(self) -> None:
        """The bug: the copy was written with the formula still inside."""

        self.pretendi_che_si_fermi(self.compila(lambda xml: xml.replace(_FORMULA_DI_OPENPYXL, "<f>5</f>")))

    def test_con_il_valore_in_cache_si_fermava_gia(self) -> None:
        """The case the bug didn't have: here the formula evaluates to a
        quantity, the zeroing step handles it, and the sheet still refuses
        it. Worth pinning down: it's the boundary between the two behaviors."""

        self.pretendi_che_si_fermi(self.compila(lambda xml: xml.replace(_FORMULA_DI_OPENPYXL, "<f>5</f><v>5</v>")))


class UnaFormulaNellaColonnaDOrdineDelXls(unittest.TestCase):
    """Same gap in Noce's `.xls`, where the copy is written by `app/xls_writer.py`.

    It never reached the supplier: the pre-compilation check in
    `app/server.py` (`controlla_colonna_ordine`) scans the whole order
    column, so a formula anywhere in it blocks compilation. `compila_ordine`
    alone only reaches the last row the plan touches: a formula further down
    passes through it, since the zeroing step skips rows outside the plan
    (not a fixed-length scan), and stays in the copy. Both checks agree on
    rejecting it.
    """

    def setUp(self) -> None:
        temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(temporanea.cleanup)
        self.cartella = Path(temporanea.name)

    def test_una_formula_sotto_l_ultima_riga_ordinata_ferma_la_compilazione(self) -> None:
        # The formula sits in row 4 of the order column; the plan only
        # orders row 3.
        celle = banco_noce.foglio_di_prova(
            guasto=banco_xls.formula(3, banco_noce.COLONNA_ORDINE, banco_xls.formula_numero(9.0)),
        )
        origine = self.cartella / "listino.xls"
        origine.write_bytes(banco_xls.costruisci_xls(
            [("Foglio1", celle)], extra_globali=banco_noce.recalcid(),
        ))
        copia = self.cartella / "ordine.xls"

        with self.assertRaises(xls_writer.CompilazioneXlsError) as errore:
            xls_writer.compila_ordine(origine, copia, {3: 7}, colonna_ordine="I", prima_riga=3)

        self.assertIn("una formula", str(errore.exception))
        self.assertIn("riga 4", str(errore.exception))
        self.assertFalse(copia.exists())


if __name__ == "__main__":
    unittest.main()
