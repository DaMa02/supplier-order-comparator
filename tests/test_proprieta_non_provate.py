#!/usr/bin/env python3
"""Four properties the code claims in comments that no test protected.

Each one is a statement written in the program's own comments that, if it
stopped being true tomorrow, the suite would not notice. None is a bug; they
were simply uncovered.

1. `active_rows` closes the document after reading it. This matters
   because a file left open can't be deleted or replaced on Windows, and
   `catalog_search` calls these readers inside the service process, not
   in a subprocess that exits and releases the handle for free.
2. The writer declares the supplier's display name, not its internal
   identifier: this is what keeps a raw id like `NUOVO_FORNITORE` out of
   user-facing text.
3. `copia_fedele` with quantity zero: the branch is unreachable today —
   whoever builds the order plan discards zero-carton rows — but the
   comment states which rule applies if it were reached, and that rule is
   worth protecting.
4. A display with no declared unit count counts as one unit. This is
   the conservative fallback: it makes the offer look more expensive, never
   cheaper. A negative count must fall back the same way.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

RADICE = Path(__file__).resolve().parents[1]
for cartella in (RADICE / "app", RADICE / "scripts"):
    if str(cartella) not in sys.path:
        sys.path.insert(0, str(cartella))

import copia_fedele  # noqa: E402
import prepare_sources  # noqa: E402
from build_review_data import display_offer  # noqa: E402


class UnListinoLettoSiPuoAncoraCancellare(unittest.TestCase):
    """The property `active_rows` must have: it closes the document after reading it.

    The first two tests check this by deleting or replacing the file, which
    is how it matters on Windows: there, a file left open can't be deleted
    or replaced, and this is the real-world case that occurs on the store
    PC. On macOS and Linux `unlink()` and `replace()` succeed on an open
    file regardless, so outside Windows these two tests can't fail no
    matter what `active_rows` actually does. The fourth test below checks
    the same property in an OS-independent way.
    """

    def setUp(self) -> None:
        temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(temporanea.cleanup)
        self.radice = Path(temporanea.name)

    def listino(self, nome: str = "listino.xlsx") -> Path:
        percorso = self.radice / nome
        libro = Workbook()
        pagina = libro.active
        pagina.title = "Listino"
        pagina.append(["EAN", "Descrizione"])
        pagina.append(["8000000000001", "SAPONE"])
        libro.save(percorso)
        libro.close()
        return percorso

    def test_dopo_la_lettura_il_file_si_elimina(self) -> None:
        percorso = self.listino()

        _foglio, righe = prepare_sources.active_rows(percorso)

        self.assertEqual(len(righe), 2)
        percorso.unlink()
        self.assertFalse(percorso.exists())

    def test_e_si_sostituisce_con_quello_della_settimana_dopo(self) -> None:
        """The real-world case: a new price list replaces the old one while
        the service is running and has already read that file."""

        percorso = self.listino()
        nuovo = self.listino("listino_nuovo.xlsx")

        prepare_sources.active_rows(percorso)

        nuovo.replace(percorso)
        self.assertTrue(percorso.is_file())

    def test_le_righe_sono_gia_tutte_lette_non_un_generatore(self) -> None:
        """A lazy generator passes tests that iterate it right away while
        leaving the file open for the rest of the process's life — the same
        bug as above, visible only by checking the return type."""

        percorso = self.listino()

        _foglio, righe = prepare_sources.active_rows(percorso)

        self.assertIsInstance(righe, list)

    def test_il_documento_resta_chiuso_dopo_la_lettura(self) -> None:
        """Checks the close without depending on Windows-specific behavior.

        `_archive` is the `zipfile.ZipFile` openpyxl uses to keep the .xlsx
        (which is technically a zip) open: while the document is open its
        `fp` — the underlying file handle — is not `None`; once closed it
        is. This is a private attribute, used because openpyxl exposes no
        public way to ask whether a workbook is still open.
        """

        percorso = self.listino()

        foglio, _righe = prepare_sources.active_rows(percorso)

        self.assertIsNone(foglio.parent._archive.fp, "il documento è rimasto aperto")


class IlNomeCheIlWriterDichiara(unittest.TestCase):
    """The supplier's display name travels inside the write rule, sourced
    from the adapter registry.

    This matters because the Node writer never reads the registry — the
    rule object is its only declared input — so without this it would print
    the raw internal id, underscore and all, in messages and order file
    names: that's how `NUOVO_FORNITORE` ended up in front of the user.
    """

    def setUp(self) -> None:
        temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(temporanea.cleanup)
        self.radice = Path(temporanea.name)
        import launcher
        import registro

        self.launcher = launcher
        self.percorso_registro = self.radice / "adapters.json"
        for modulo, attributo in ((launcher, "ADAPTERS_PATH"), (registro, "REGISTRO")):
            originale = getattr(modulo, attributo)
            setattr(modulo, attributo, self.percorso_registro)
            self.addCleanup(setattr, modulo, attributo, originale)

    def listino(self) -> Path:
        percorso = self.radice / "listino.xlsx"
        libro = Workbook()
        pagina = libro.active
        pagina.title = "Listino"
        pagina.append(["EAN", "Descrizione", None])
        pagina.append(["8000000000001", "SAPONE", None])
        libro.save(percorso)
        libro.close()
        return percorso

    def scrivi_registro(self, display_name: str | None) -> dict:
        """Write the adapter to the registry. `display_name=None` omits that
        key — the case of a learned adapter the registry has no display
        name for yet."""

        import json

        adattatore = {
            "id": "nuovo_v1",
            "supplier_id": "fornitore_nuovo",
            "kind": "supplier",
            "order_write": {
                "sheet": "FIRST",
                "header_row": 1,
                "data_start_row": 2,
                "order_column": "C",
                "allow_blank_header_if_confirmed": True,
                "order_header_blank_confirmed": True,
            },
        }
        if display_name is not None:
            adattatore["display_name"] = display_name
        self.percorso_registro.write_text(
            json.dumps({"schema_version": 1, "adapters": [adattatore]}, ensure_ascii=False),
            encoding="utf-8",
        )
        return adattatore

    def test_la_regola_porta_il_nome_dichiarato_dal_registro(self) -> None:
        adattatore = self.scrivi_registro("Fornitore Nuovo")

        regola, motivo = self.launcher.source_rule(
            "fornitore_nuovo", self.listino(), {}, adattatore,
        )

        self.assertIsNone(motivo)
        self.assertEqual(regola["display_name"], "Fornitore Nuovo")

    def test_senza_display_name_dichiarato_la_regola_porta_il_nome_leggibile(self) -> None:
        """The fallback name, checked at the point where it actually matters: `source_rule()`.

        A version of this test relying on `assertNotIn("_", …)` could
        never fail: it reused the same setup as the test above, whose exact
        equality against `"Fornitore Nuovo"` already implies the absence of
        an underscore. It was green by construction whenever the other test
        was.

        The real fallback — which fires when the registry has no
        `display_name` — is already covered elsewhere, at the level of the
        name alone: `tests/test_web_app.py`
        (`IlRegistroEUnaVeritaSolaTests`) and
        `tests/test_registro_impronte.py` (`IlNomeDelFornitoreInOgniFrase`).
        What was missing is the same case exercised through `source_rule()`
        itself: the one place the name flows into the rule handed to the
        Node writer, which is where `NUOVO_FORNITORE` ended up in front of
        the user.
        """

        adattatore = self.scrivi_registro(None)

        regola, motivo = self.launcher.source_rule(
            "fornitore_nuovo", self.listino(), {}, adattatore,
        )

        self.assertIsNone(motivo)
        self.assertEqual(regola["display_name"], "FORNITORE NUOVO")


class LaCopiaConQuantitaZero(unittest.TestCase):
    """The `attesa == 0` branch: unreachable today, but the rule that would
    apply if it were reached is stated in the code. This test catches a
    silent change to that rule."""

    def setUp(self) -> None:
        temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(temporanea.cleanup)
        self.radice = Path(temporanea.name)

    def coppia(self, valore_copia) -> tuple[Path, Path]:
        originale = self.radice / "originale.xlsx"
        copia = self.radice / "copia.xlsx"
        for percorso, valore in ((originale, None), (copia, valore_copia)):
            libro = Workbook()
            pagina = libro.active
            pagina.title = "Listino"
            pagina.append(["EAN", "Descrizione", "ORDINE"])
            pagina.append(["8000000000001", "SAPONE", valore])
            libro.save(percorso)
            libro.close()
        return originale, copia

    def test_il_piano_chiede_zero_e_la_copia_mostra_zero(self) -> None:
        originale, copia = self.coppia(0)

        esito = copia_fedele.confronta_copia(
            originale, copia, colonna_ordine="C", prima_riga=2, quantita={2: 0},
        )

        self.assertEqual(esito.righe_mancanti, [])
        self.assertEqual(esito.quante_rifiutate, 0)

    def test_il_piano_chiede_zero_e_la_copia_mostra_altro(self) -> None:
        """The copy must match what the plan asked for: it stops rather than
        ship with a cell that disagrees with the plan."""

        originale, copia = self.coppia(5)

        esito = copia_fedele.confronta_copia(
            originale, copia, colonna_ordine="C", prima_riga=2, quantita={2: 0},
        )

        self.assertEqual(esito.righe_mancanti, [2])


class UnEspositoreSenzaPezziDichiarati(unittest.TestCase):
    """The conservative fallback: with no declared unit count, a display
    counts as one unit, so the offer looks more expensive rather than
    cheaper. A negative count is equally unusable data and must get the
    same fallback, not a negative factor that would flip the comparison."""

    BASE = {
        "supplier": "larice",
        "net_price_per_display": 24.0,
        "confidence": "ALTA",
        "auto_confirmed": True,
    }

    def test_senza_pezzi_dichiarati_vale_un_pezzo(self) -> None:
        offerta = display_offer({**self.BASE})

        self.assertEqual(offerta["quantityFactor"], 1)
        self.assertEqual(offerta["unitPriceNet"], 24.0)

    def test_con_pezzi_dichiarati_negativi_vale_un_pezzo_lo_stesso(self) -> None:
        offerta = display_offer({**self.BASE, "declared_units": -12})

        self.assertEqual(offerta["quantityFactor"], 1)
        self.assertEqual(offerta["unitPriceNet"], 24.0)

    def test_con_pezzi_dichiarati_veri_il_prezzo_e_quello_del_pezzo(self) -> None:
        offerta = display_offer({**self.BASE, "declared_units": 12})

        self.assertEqual(offerta["quantityFactor"], 12)
        self.assertEqual(offerta["unitPriceNet"], 2.0)
        self.assertEqual(offerta["orderUnitPriceNet"], 24.0)


if __name__ == "__main__":
    unittest.main()
