"""The "items to be sourced" list: products no supplier can provide.

Covers the invariants a plausible-but-wrong implementation would silently break:

1. The six columns, in this order. The sheet is opened by a person calling
   suppliers: a shifted column doesn't crash anything, it just puts the price
   where the quantity should be. Values are read back with `openpyxl`
   cell by cell, not by checking that the file merely "exists".
2. An empty `righe` is an error. A sheet with only headers would still enter
   the delivery listing and the zip, telling whoever opens it there's
   something to source when there's nothing.
3. The file name carries an em dash, like compiled price lists, and must
   survive `consegna.intestazione_allegato` without breaking the HTTP
   response: headers are written in latin-1, and a raw em dash there raises
   `UnicodeEncodeError` after the body is already promised.
4. The name must be recognized by `consegna.tipo_file`. If that check fails,
   the file gets misclassified as a price list and shipped to a supplier.

No network, no `app/data/`: everything runs in `tempfile.TemporaryDirectory()`.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from openpyxl import load_workbook


SKILL_ROOT = Path(__file__).resolve().parents[1]
for cartella in (SKILL_ROOT / "app",):
    if str(cartella) not in sys.path:
        sys.path.insert(0, str(cartella))

import consegna  # noqa: E402
import da_reperire  # noqa: E402


MOMENTO = datetime(2026, 8, 17, 9, 30, 12)
EM_DASH = "—"
NOME_ATTESO = f"Prodotti da reperire {EM_DASH} 17 agosto 2026.xlsx"

INTESTAZIONI_ATTESE = [
    "EAN",
    "Descrizione",
    "Colli richiesti",
    "Ultimo prezzo noto",
    "Motivo",
    "Note",
]


def riga(
    *,
    ean: str = "8005905000123",
    descrizione: str = "Caffè macinato 250 g",
    quantita: object = 4,
    unita: str = "colli",
    ultimo_prezzo: object = 3.9,
    motivo: str = da_reperire.MOTIVO_NON_A_LISTINO,
) -> dict:
    """Build a plausible row in the contract's declared shape."""
    return {
        "ean": ean,
        "descrizione": descrizione,
        "quantita": quantita,
        "unita": unita,
        "ultimo_prezzo": ultimo_prezzo,
        "motivo": motivo,
    }


def offerta(stato: str, *, chiave: str = "status") -> dict:
    """An offer trimmed to what `motivo` actually inspects."""
    return {"supplierId": "larice", chiave: stato}


class CasoConCartella(unittest.TestCase):
    def setUp(self) -> None:
        self.temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporanea.cleanup)
        self.cartella = Path(self.temporanea.name)

    def scrivi_e_rileggi(self, righe: list[dict], momento: datetime = MOMENTO):
        """Write the sheet and actually reopen it; returns `(nome, foglio)`."""
        nome = da_reperire.scrivi(self.cartella, righe, momento)
        libro = load_workbook(self.cartella / nome)
        self.addCleanup(libro.close)
        return nome, libro.active


# ---------------------------------------------------------------------------
# File name
# ---------------------------------------------------------------------------

class NomeFileTests(unittest.TestCase):
    def test_nome_file(self) -> None:
        self.assertEqual(da_reperire.nome_file(MOMENTO), NOME_ATTESO)
        self.assertIn(f" {EM_DASH} ", da_reperire.nome_file(MOMENTO))

    def test_nome_file_usa_la_stessa_data_dei_listini(self) -> None:
        # Reuses `consegna`'s own month table rather than a second one, so it
        # never goes through strftime("%B") and never prints "August".
        for momento in (datetime(2026, 1, 5), datetime(2026, 9, 3), datetime(2026, 12, 31)):
            self.assertEqual(
                da_reperire.nome_file(momento),
                f"Prodotti da reperire {EM_DASH} {consegna.data_leggibile(momento)}.xlsx",
            )
        self.assertEqual(
            da_reperire.nome_file(datetime(2026, 3, 30)),
            f"Prodotti da reperire {EM_DASH} 30 marzo 2026.xlsx",
        )

    def test_il_nome_non_contiene_caratteri_vietati_da_windows(self) -> None:
        nome = da_reperire.nome_file(MOMENTO)
        for vietato in consegna.VIETATI_WINDOWS:
            self.assertNotIn(vietato, nome)

    def test_il_nome_attraversa_l_intestazione_di_scaricamento(self) -> None:
        """`BaseHTTPRequestHandler` writes headers in latin-1.

        A raw em dash breaks the response after the body is already
        promised — the same failure mode as the price-list names, guarded by
        the same function. This checks the new file name passes through it too.
        """

        nome = da_reperire.nome_file(MOMENTO)
        valore = consegna.intestazione_allegato(nome)
        valore.encode("latin-1")  # must not raise
        with self.assertRaises(UnicodeEncodeError):
            f'attachment; filename="{nome}"'.encode("latin-1")
        self.assertIn('filename="Prodotti da reperire - 17 agosto 2026.xlsx"', valore)
        self.assertIn("filename*=UTF-8''", valore)

    def test_il_nome_prodotto_si_fa_riconoscere_dalla_consegna(self) -> None:
        """End-to-end: the name written here is the same one `consegna` reads.

        If this diverges, the file gets counted as a price list and shipped
        to the supplier inside the zip.
        """

        nome = da_reperire.nome_file(MOMENTO)
        self.assertEqual(consegna.tipo_file(nome), da_reperire.TIPO)
        self.assertEqual(da_reperire.TIPO, "da_reperire")
        self.assertTrue(consegna.e_documento(nome))


# ---------------------------------------------------------------------------
# Reason column
# ---------------------------------------------------------------------------

class MotivoTests(unittest.TestCase):
    def test_tutti_non_trovato_vuol_dire_che_non_lo_ha_a_listino_nessuno(self) -> None:
        offerte = [offerta("NON_TROVATO"), offerta("NON_TROVATO"), offerta("NON_TROVATO")]
        self.assertEqual(da_reperire.motivo(offerte), "Nessun fornitore lo ha a listino")
        self.assertEqual(da_reperire.motivo(offerte), da_reperire.MOTIVO_NON_A_LISTINO)

    def test_basta_un_fornitore_che_lo_ha_a_listino_per_cambiare_frase(self) -> None:
        # The supplier has it, but the offer isn't usable: whoever calls
        # should try that same supplier again later, not look for a new one.
        # The two messages say exactly that.
        offerte = [offerta("NON_TROVATO"), offerta("ESATTO"), offerta("NON_TROVATO")]
        self.assertEqual(da_reperire.motivo(offerte), "Nessun fornitore lo ha disponibile")
        self.assertEqual(da_reperire.motivo(offerte), da_reperire.MOTIVO_NON_DISPONIBILE)

    def test_nessuna_offerta_e_il_caso_limite_di_nessuno_a_listino(self) -> None:
        for vuoto in ([], (), None):
            self.assertEqual(da_reperire.motivo(vuoto), da_reperire.MOTIVO_NON_A_LISTINO)

    def test_legge_anche_match_status_quando_status_manca(self) -> None:
        # The comparison's offers carry the same value in two fields; if one
        # arrived with only `matchStatus`, saying "no supplier has it" when
        # one actually does would be a lie in the column the user reads.
        self.assertEqual(
            da_reperire.motivo([offerta("NON_TROVATO", chiave="matchStatus")]),
            da_reperire.MOTIVO_NON_A_LISTINO,
        )
        self.assertEqual(
            da_reperire.motivo([offerta("ESATTO", chiave="matchStatus")]),
            da_reperire.MOTIVO_NON_DISPONIBILE,
        )

    def test_uno_stato_illeggibile_non_diventa_non_trovato(self) -> None:
        # `None`, an empty string, an offer that isn't even a dict: none of
        # these can prove it's absent from every price list, so the cautious
        # message is the other one.
        for offerte in (
            [{"supplierId": "larice"}],
            [{"status": ""}],
            [{"status": None}],
            ["non un'offerta"],
            [offerta("NON_TROVATO"), "non un'offerta"],
        ):
            self.assertEqual(da_reperire.motivo(offerte), da_reperire.MOTIVO_NON_DISPONIBILE, repr(offerte))

    def test_motivo_non_guarda_il_disco_ne_il_server(self) -> None:
        # A pure function: the same input always yields the same message and
        # leaves no side effects.
        offerte = [offerta("NON_TROVATO")]
        self.assertEqual(da_reperire.motivo(offerte), da_reperire.motivo(offerte))
        self.assertEqual(offerte, [{"supplierId": "larice", "status": "NON_TROVATO"}])


# ---------------------------------------------------------------------------
# File output
# ---------------------------------------------------------------------------

class ScriviTests(CasoConCartella):
    def test_il_file_nasce_con_il_nome_giusto_e_lo_restituisce(self) -> None:
        nome = da_reperire.scrivi(self.cartella, [riga()], MOMENTO)
        self.assertEqual(nome, NOME_ATTESO)
        self.assertTrue((self.cartella / nome).is_file())
        # No leftover temp file.
        self.assertEqual(sorted(p.name for p in self.cartella.iterdir()), [NOME_ATTESO])

    def test_le_sei_intestazioni_sono_quelle_e_in_quest_ordine(self) -> None:
        _, foglio = self.scrivi_e_rileggi([riga()])
        prima_riga = [cella.value for cella in foglio[1]]
        self.assertEqual(prima_riga, INTESTAZIONI_ATTESE)
        self.assertEqual(foglio.max_column, 6)

    def test_le_intestazioni_sono_in_grassetto(self) -> None:
        _, foglio = self.scrivi_e_rileggi([riga()])
        for colonna in range(1, 7):
            self.assertTrue(foglio.cell(row=1, column=colonna).font.bold, colonna)
        self.assertFalse(foglio.cell(row=2, column=3).font.bold)

    def test_ogni_valore_finisce_nella_sua_cella(self) -> None:
        _, foglio = self.scrivi_e_rileggi([
            riga(),
            riga(
                ean="8001234000999",
                descrizione="Espositore natalizio",
                quantita=3,
                unita="espositori",
                ultimo_prezzo=None,
                motivo=da_reperire.MOTIVO_NON_DISPONIBILE,
            ),
        ])

        self.assertEqual(foglio.max_row, 3)  # header + two products
        self.assertEqual(foglio["A2"].value, "8005905000123")
        self.assertEqual(foglio["B2"].value, "Caffè macinato 250 g")
        self.assertEqual(foglio["C2"].value, 4)
        self.assertEqual(foglio["D2"].value, 3.9)
        self.assertEqual(foglio["E2"].value, "Nessun fornitore lo ha a listino")
        self.assertIsNone(foglio["F2"].value)  # filled in by the user

        self.assertEqual(foglio["A3"].value, "8001234000999")
        self.assertEqual(foglio["B3"].value, "Espositore natalizio")
        self.assertEqual(foglio["E3"].value, "Nessun fornitore lo ha disponibile")

    def test_l_ean_resta_testo_e_non_diventa_notazione_scientifica(self) -> None:
        """A 13-digit code stored as a number renders as `8.0059e+12`.

        Whoever copies that value to search another price list gets zero
        results with no way to understand why.
        """

        _, foglio = self.scrivi_e_rileggi([riga(ean="8005905000123")])
        cella = foglio["A2"]
        self.assertIsInstance(cella.value, str)
        self.assertEqual(cella.value, "8005905000123")
        self.assertEqual(cella.data_type, "s")

    def test_un_ean_gia_numerico_arriva_lo_stesso_come_testo(self) -> None:
        # The caller might pass it as an int: the sheet must render it the
        # same way regardless of the upstream type.
        _, foglio = self.scrivi_e_rileggi([riga(ean=8005905000123)])
        self.assertEqual(foglio["A2"].value, "8005905000123")

    def test_il_prezzo_mancante_lascia_la_cella_vuota(self) -> None:
        """Empty and zero mean different things.

        A zero in that column would claim the product cost nothing, which is
        false; an empty cell says "unknown", which is true.
        """

        _, foglio = self.scrivi_e_rileggi([riga(ultimo_prezzo=None)])
        self.assertIsNone(foglio["D2"].value)
        self.assertNotEqual(foglio["D2"].value, 0)

    def test_il_prezzo_presente_resta_un_numero(self) -> None:
        # A price stored as text can't be summed or sorted.
        _, foglio = self.scrivi_e_rileggi([riga(ultimo_prezzo=12.5)])
        self.assertEqual(foglio["D2"].value, 12.5)
        self.assertIsInstance(foglio["D2"].value, (int, float))

    def test_i_colli_restano_un_numero_e_l_unita_diversa_entra_nella_cella(self) -> None:
        """The "Colli richiesti" header stays fixed even though the list mixes
        cartons and displays.

        Three displays written as `3` under that header would read as three
        cartons, and whoever calls the supplier would order the wrong thing.
        """

        _, foglio = self.scrivi_e_rileggi([
            riga(quantita=4, unita="colli"),
            riga(quantita=3, unita="espositori"),
            riga(quantita=2, unita=""),
            riga(quantita=1, unita="Colli"),
        ])
        self.assertEqual(foglio["C2"].value, 4)
        self.assertEqual(foglio["C3"].value, "3 espositori")
        self.assertEqual(foglio["C4"].value, 2)  # no unit given: it's cartons
        self.assertEqual(foglio["C5"].value, 1)  # case-insensitive match
        self.assertIsInstance(foglio["C2"].value, int)

    def test_righe_vuote_sono_un_errore_del_chiamante_e_non_lasciano_file(self) -> None:
        """A caller must never end up creating an empty file.

        A sheet with only headers would still enter the delivery listing and
        the zip, telling whoever opens it there's something to source when
        there's nothing.
        """

        for vuoto in ([], (), None):
            with self.assertRaises(ValueError, msg=repr(vuoto)):
                da_reperire.scrivi(self.cartella, vuoto, MOMENTO)
        self.assertEqual(list(self.cartella.iterdir()), [])

    def test_una_riga_a_cui_manca_un_campo_non_fa_saltare_la_compilazione(self) -> None:
        # The dated run folder is deleted if anything crashes here: a missing
        # field must not cost the whole run.
        _, foglio = self.scrivi_e_rileggi([{"descrizione": "Solo la descrizione"}])
        self.assertIsNone(foglio["A2"].value)
        self.assertEqual(foglio["B2"].value, "Solo la descrizione")
        self.assertIsNone(foglio["C2"].value)
        self.assertIsNone(foglio["D2"].value)
        self.assertIsNone(foglio["E2"].value)

    def test_due_scritture_nella_stessa_cartella_non_si_sommano(self) -> None:
        # Same timestamp, same name: the second write replaces, it doesn't append.
        da_reperire.scrivi(self.cartella, [riga(), riga()], MOMENTO)
        _, foglio = self.scrivi_e_rileggi([riga()])
        self.assertEqual(foglio.max_row, 2)

    def test_il_file_e_un_xlsx_vero_e_apribile(self) -> None:
        # Proves what this module writes actually reopens as a valid workbook.
        nome = da_reperire.scrivi(self.cartella, [riga()], MOMENTO)
        crudo = (self.cartella / nome).read_bytes()
        self.assertTrue(crudo.startswith(b"PK"))  # it's a zip, i.e. OOXML
        libro = load_workbook(self.cartella / nome)
        self.addCleanup(libro.close)
        self.assertEqual(libro.sheetnames, [da_reperire.NOME_FOGLIO])

    def test_niente_formule_dentro_le_celle(self) -> None:
        # The sheet is opened by a person, not a spreadsheet engine: a
        # formula here would be one more thing that can break.
        nome = da_reperire.scrivi(self.cartella, [riga()], MOMENTO)
        libro = load_workbook(self.cartella / nome)
        self.addCleanup(libro.close)
        for riga_celle in libro.active.iter_rows():
            for cella in riga_celle:
                self.assertNotEqual(cella.data_type, "f", cella.coordinate)

    def test_una_descrizione_o_un_ean_che_comincia_per_uguale_non_diventa_formula(self) -> None:
        """EAN and description come from supplier price lists: text outside
        our control. openpyxl writes a string starting with "=" as a
        FORMULA, so whoever opens the sheet to call suppliers would see a
        calculation (or `#NAME?`) instead of the product name or code.

        Asserted via `data_type` on reread rather than the raw XML: openpyxl
        serializes an empty element differently depending on whether `lxml`
        is installed, so a test checking the XML directly could pass on one
        machine and fail on another with the same code.
        """

        nome = da_reperire.scrivi(
            self.cartella,
            [riga(ean="=cmd|'/C calc'!A1", descrizione='=HYPERLINK("http://esempio.test")')],
            MOMENTO,
        )
        libro = load_workbook(self.cartella / nome)
        self.addCleanup(libro.close)
        foglio = libro.active

        cella_ean = foglio["A2"]
        self.assertEqual(cella_ean.data_type, "s")
        self.assertEqual(cella_ean.value, "=cmd|'/C calc'!A1")

        cella_descrizione = foglio["B2"]
        self.assertEqual(cella_descrizione.data_type, "s")
        self.assertEqual(cella_descrizione.value, '=HYPERLINK("http://esempio.test")')

    def test_le_colonne_hanno_una_larghezza(self) -> None:
        # Without this, the description and notes columns render truncated
        # and need to be widened by hand every time.
        nome = da_reperire.scrivi(self.cartella, [riga()], MOMENTO)
        libro = load_workbook(self.cartella / nome)
        self.addCleanup(libro.close)
        for lettera in ("A", "B", "C", "D", "E", "F", "G"):
            self.assertGreater(libro.active.column_dimensions[lettera].width, 0, lettera)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
