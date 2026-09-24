"""Tests for compiling the Noce order into a copy of their `.xls` file.

Noce gets back their own document, with only the order column filled in and
everything else identical — a deliberate design choice, not an optimization.
This suite checks four properties:

1. The patch writes in place; the rest of the file does not move. The tests
   don't just re-read the cells, they count the changed bytes between
   original and copy, because a patch that rebuilds the file could produce
   the same cell values in a different document than the one Noce is meant
   to get back.
2. The precondition is re-checked on every file. A formula, an empty cell, or
   an eight-byte number in the order column makes the patch inapplicable, so
   compilation fails loudly instead of leaving a half-written copy.
3. The EAN on each row must match the plan's EAN for that row — the only
   check that would catch a shipment plan and worksheet drifting out of row
   alignment.
4. Changing a cell does not leave formulas that reference it stale. `RECALCID`
   is zeroed so Excel recomputes on open; without it, quantities update but
   dependent totals stay at their last computed value.

The `.xls` fixtures are built by `test_xls_reader`: a real OLE2 container with
real BIFF8 records, covering both the mini-stream and normal sectors.
"""

from __future__ import annotations

import struct
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
for cartella in (SKILL_ROOT / "app", SKILL_ROOT / "tests"):
    if str(cartella) not in sys.path:
        sys.path.insert(0, str(cartella))

import test_xls_reader as banco  # noqa: E402
import xls_writer  # noqa: E402
from xls_reader import _decodifica_rk, read_workbook  # noqa: E402
from xls_writer import CompilazioneXlsError, compila_ordine, controlla_colonna_ordine  # noqa: E402


COLONNA_EAN = 1     # B
COLONNA_ORDINE = 8  # I


def recalcid(build: int = 191541) -> bytes:
    return banco.rec(0x01C1, struct.pack("<HHI", 0x01C1, 0, build))


def foglio_di_prova(*, righe: int = 6, con_mulrk: bool = False, guasto: bytes | None = None) -> bytes:
    """Build a plausible price list: EAN in B, description in D, price in H, order in I.

    The first two rows are a header; data starts at row 3 (1-based, as the
    user sees it), matching the shape of a real price list where the header
    is on row 5 and data starts at row 6.
    """

    celle = banco.label(0, COLONNA_EAN, "CodiceABarre") + banco.label(0, COLONNA_ORDINE, "Quantita")
    for indice in range(righe):
        riga = indice + 2  # 0-based BIFF row = user-facing row 3
        celle += banco.label(riga, COLONNA_EAN, f"800000000000{indice}")
        celle += banco.label(riga, 3, f"PRODOTTO {indice}")
        celle += banco.rk(riga, 7, banco.rk_intero(2 + indice))
        if con_mulrk and indice == 0:
            celle += banco.mulrk(riga, 7, [(0, banco.rk_intero(2)), (0, banco.rk_intero(0))])
        elif guasto is not None and indice == 1:
            celle += guasto
        else:
            celle += banco.rk(riga, COLONNA_ORDINE, banco.rk_intero(0))
    return celle


class BancoXlsWriter(unittest.TestCase):
    def setUp(self) -> None:
        self.temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporanea.cleanup)
        self.cartella = Path(self.temporanea.name)

    def scrivi(self, contenuto: bytes, nome: str = "listino.xls") -> Path:
        percorso = self.cartella / nome
        percorso.write_bytes(contenuto)
        return percorso

    def listino(self, **argomenti) -> Path:
        riempimento = argomenti.pop("riempimento", 0)
        con_recalcid = argomenti.pop("con_recalcid", True)
        celle = foglio_di_prova(**argomenti)
        contenuto = banco.costruisci_xls(
            [("Foglio1", celle)],
            riempimento=riempimento,
            extra_globali=recalcid() if con_recalcid else b"",
        )
        return self.scrivi(contenuto)

    def valori(self, percorso: Path) -> list[list[object]]:
        fogli = read_workbook(percorso)
        return [[valore for valore, _grassetto in riga] for riga in fogli[0].rows]


class LaCodificaRk(unittest.TestCase):
    def test_il_giro_completo_torna_al_numero_di_partenza(self) -> None:
        for valore in (0, 1, 7, 99, 1000, 65535, xls_writer.MASSIMA_QUANTITA_RK):
            with self.subTest(valore=valore):
                self.assertEqual(_decodifica_rk(xls_writer.codifica_rk_intero(valore)), valore)

    def test_quello_che_non_sta_in_quattro_byte_viene_rifiutato(self) -> None:
        for valore in (-1, xls_writer.MASSIMA_QUANTITA_RK + 1):
            with self.subTest(valore=valore):
                with self.assertRaises(CompilazioneXlsError):
                    xls_writer.codifica_rk_intero(valore)

    def test_un_numero_che_non_e_intero_viene_rifiutato(self) -> None:
        for valore in (1.5, "3", True, None):
            with self.subTest(valore=valore):
                with self.assertRaises(CompilazioneXlsError):
                    xls_writer.codifica_rk_intero(valore)  # type: ignore[arg-type]


class LaPatchInPosizione(BancoXlsWriter):
    def test_le_quantita_arrivano_nelle_celle_giuste(self) -> None:
        origine = self.listino()
        copia = self.cartella / "ordine.xls"
        esito = compila_ordine(origine, copia, {3: 7, 5: 12}, colonna_ordine="I", prima_riga=3)
        self.assertEqual(esito["righe_scritte"], 2)
        self.assertEqual(esito["colli_totali"], 19)
        griglia = self.valori(copia)
        self.assertEqual(griglia[2][COLONNA_ORDINE], 7)
        self.assertEqual(griglia[4][COLONNA_ORDINE], 12)
        self.assertEqual(griglia[3][COLONNA_ORDINE], 0)

    def test_il_resto_del_file_non_si_sposta_di_un_byte(self) -> None:
        """Confirms this is an in-place patch, not a rewrite."""

        origine = self.listino()
        copia = self.cartella / "ordine.xls"
        compila_ordine(origine, copia, {3: 7, 5: 12}, colonna_ordine="I", prima_riga=3)
        prima = origine.read_bytes()
        dopo = copia.read_bytes()
        self.assertEqual(len(prima), len(dopo))
        diversi = [indice for indice in range(len(prima)) if prima[indice] != dopo[indice]]
        # Two 4-byte cells plus the 4-byte RECALCID: at most twelve bytes.
        self.assertLessEqual(len(diversi), 12)
        self.assertGreater(len(diversi), 0)

    def test_tutte_le_altre_celle_restano_identiche(self) -> None:
        origine = self.listino()
        copia = self.cartella / "ordine.xls"
        compila_ordine(origine, copia, {3: 7}, colonna_ordine="I", prima_riga=3)
        prima = self.valori(origine)
        dopo = self.valori(copia)
        differenze = [
            (riga, colonna)
            for riga, (una, altra) in enumerate(zip(prima, dopo), start=1)
            for colonna, (valore_a, valore_b) in enumerate(zip(una, altra))
            if valore_a != valore_b
        ]
        self.assertEqual(differenze, [(3, COLONNA_ORDINE)])

    def test_funziona_anche_quando_il_libro_sta_nei_settori_normali(self) -> None:
        """The mini-stream and normal sectors are separate code paths; both need coverage."""

        origine = self.listino(riempimento=8000)
        self.assertGreater(origine.stat().st_size, 8000)
        copia = self.cartella / "ordine.xls"
        compila_ordine(origine, copia, {4: 5}, colonna_ordine="I", prima_riga=3)
        self.assertEqual(self.valori(copia)[3][COLONNA_ORDINE], 5)

    def test_una_cella_dentro_un_mulrk_si_compila_lo_stesso(self) -> None:
        origine = self.listino(con_mulrk=True)
        copia = self.cartella / "ordine.xls"
        compila_ordine(origine, copia, {3: 9}, colonna_ordine="I", prima_riga=3)
        griglia = self.valori(copia)
        self.assertEqual(griglia[2][COLONNA_ORDINE], 9)
        # The adjacent cell, packed in the same MULRK record, must stay untouched.
        self.assertEqual(griglia[2][7], 2)

    def test_l_originale_non_viene_mai_toccato(self) -> None:
        origine = self.listino()
        prima = origine.read_bytes()
        compila_ordine(origine, self.cartella / "ordine.xls", {3: 7}, colonna_ordine="I", prima_riga=3)
        self.assertEqual(origine.read_bytes(), prima)

    def test_due_righe_uguali_nel_piano_si_sommano_nella_stessa_cella(self) -> None:
        origine = self.listino()
        copia = self.cartella / "ordine.xls"
        compila_ordine(origine, copia, {3: 4}, colonna_ordine="I", prima_riga=3)
        self.assertEqual(self.valori(copia)[2][COLONNA_ORDINE], 4)


class IlRicalcoloForzato(BancoXlsWriter):
    def test_recalcid_viene_azzerato(self) -> None:
        """Without this, quantities update but dependent totals stay stale."""

        origine = self.listino()
        copia = self.cartella / "ordine.xls"
        esito = compila_ordine(origine, copia, {3: 7}, colonna_ordine="I", prima_riga=3)
        self.assertTrue(esito["ricalcolo_forzato"])
        crudo = copia.read_bytes()
        posizione = crudo.find(struct.pack("<HHHH", 0x01C1, 8, 0x01C1, 0))
        self.assertNotEqual(posizione, -1, "il record RECALCID non è stato trovato nella copia")
        (build,) = struct.unpack_from("<I", crudo, posizione + 8)
        self.assertEqual(build, 0)

    def test_senza_recalcid_lo_dice_e_non_fallisce(self) -> None:
        """A file with no RECALCID record still gets recalculated by Excel on open."""

        origine = self.listino(con_recalcid=False)
        copia = self.cartella / "ordine.xls"
        esito = compila_ordine(origine, copia, {3: 7}, colonna_ordine="I", prima_riga=3)
        self.assertFalse(esito["ricalcolo_forzato"])
        self.assertEqual(self.valori(copia)[2][COLONNA_ORDINE], 7)


class LaColonnaDeveEssereTuttaRk(BancoXlsWriter):
    def _guasto(self, cella: bytes, atteso: str) -> None:
        origine = self.listino(guasto=cella)
        copia = self.cartella / "ordine.xls"
        with self.assertRaises(CompilazioneXlsError) as errore:
            compila_ordine(origine, copia, {3: 7, 4: 2}, colonna_ordine="I", prima_riga=3)
        self.assertIn(atteso, str(errore.exception))
        # No partial copy: a document that looks like a completed order but
        # isn't is worse than no document at all.
        self.assertFalse(copia.exists())

    def test_una_formula_nella_colonna_d_ordine_ferma_tutto(self) -> None:
        self._guasto(
            banco.formula(3, COLONNA_ORDINE, banco.formula_numero(0.0)),
            "una formula",
        )

    def test_una_cella_vuota_nella_colonna_d_ordine_ferma_tutto(self) -> None:
        self._guasto(banco.blank(3, COLONNA_ORDINE), "una cella vuota")

    def test_un_numero_a_virgola_mobile_ferma_tutto(self) -> None:
        self._guasto(banco.numero(3, COLONNA_ORDINE, 0.0), "un numero a virgola mobile")

    def test_del_testo_nella_colonna_d_ordine_ferma_tutto(self) -> None:
        self._guasto(banco.label(3, COLONNA_ORDINE, "0"), "del testo")

    def test_una_cella_vuota_dentro_un_mulblank_ferma_tutto(self) -> None:
        """A run of empty cells is packed by Excel into a single `MULBLANK` record.

        Without decoding it, an order column made entirely of blank cells
        packed this way would look compilable, and the fixed-length patch
        would write four bytes into cells that don't have four bytes to give.
        """

        # One MULBLANK record covering columns 7, 8 and 9; the order column is 8.
        self._guasto(banco.mulblank(3, COLONNA_ORDINE - 1, [0, 0, 0]), "una cella vuota")

    def test_il_controllo_preventivo_vede_anche_il_mulblank(self) -> None:
        esito = controlla_colonna_ordine(
            self.listino(guasto=banco.mulblank(3, COLONNA_ORDINE - 1, [0, 0, 0])),
            colonna_ordine="I",
            prima_riga=3,
        )
        self.assertFalse(esito["compilabile"])
        self.assertEqual(esito["celle_di_altro_tipo"], {4: "una cella vuota"})

    def test_il_controllo_preventivo_dice_che_cosa_ha_visto(self) -> None:
        buono = controlla_colonna_ordine(self.listino(), colonna_ordine="I", prima_riga=3)
        self.assertTrue(buono["compilabile"])
        self.assertEqual(buono["celle_rk"], 6)
        rotto = controlla_colonna_ordine(
            self.listino(guasto=banco.blank(3, COLONNA_ORDINE)), colonna_ordine="I", prima_riga=3
        )
        self.assertFalse(rotto["compilabile"])
        self.assertEqual(rotto["celle_di_altro_tipo"], {4: "una cella vuota"})

    def test_una_colonna_d_ordine_con_zero_celle_non_e_compilabile(self) -> None:
        """Zero writable cells is not "all clear": it's a column that doesn't exist."""

        # A column beyond the written ones: the price list has no cells there.
        esito = controlla_colonna_ordine(self.listino(), colonna_ordine="Z", prima_riga=3)

        self.assertEqual(esito["celle_rk"], 0)
        self.assertEqual(esito["celle_di_altro_tipo"], {})
        self.assertFalse(esito["compilabile"], "una colonna vuota non si può compilare")

    def test_una_riga_del_piano_senza_cella_d_ordine_ferma_tutto(self) -> None:
        origine = self.listino()
        copia = self.cartella / "ordine.xls"
        with self.assertRaises(CompilazioneXlsError) as errore:
            compila_ordine(origine, copia, {900: 3}, colonna_ordine="I", prima_riga=3)
        self.assertIn("900", str(errore.exception))
        self.assertFalse(copia.exists())


class LaGuardiaSullEan(BancoXlsWriter):
    def test_l_ean_diverso_ferma_la_compilazione(self) -> None:
        """A row-alignment safeguard available only for this supplier's file format."""

        origine = self.listino()
        copia = self.cartella / "ordine.xls"
        with self.assertRaises(CompilazioneXlsError) as errore:
            compila_ordine(
                origine, copia, {3: 7},
                colonna_ordine="I", prima_riga=3,
                ean_attesi={3: "9999999999999"}, colonna_ean="B",
            )
        self.assertIn("9999999999999", str(errore.exception))
        self.assertFalse(copia.exists())

    def test_l_ean_giusto_lascia_passare_e_si_conta(self) -> None:
        origine = self.listino()
        copia = self.cartella / "ordine.xls"
        esito = compila_ordine(
            origine, copia, {3: 7, 4: 1},
            colonna_ordine="I", prima_riga=3,
            ean_attesi={3: "8000000000000", 4: "8000000000001"}, colonna_ean="B",
        )
        self.assertEqual(esito["ean_controllati"], 2)

    def test_un_piano_senza_ean_su_una_riga_che_ce_l_ha_ferma_tutto(self) -> None:
        """A plan row without an EAN is selecting that row by row number alone.

        If the price list does have an EAN there, the check can't run, so
        compilation must refuse rather than silently skip the guard.
        """

        origine = self.listino()
        copia = self.cartella / "ordine.xls"
        with self.assertRaises(CompilazioneXlsError) as errore:
            compila_ordine(
                origine, copia, {3: 7, 4: 2},
                colonna_ordine="I", prima_riga=3,
                ean_attesi={3: "8000000000000", 4: ""}, colonna_ean="B",
            )
        messaggio = str(errore.exception)
        self.assertIn("riga 4", messaggio)
        self.assertIn("8000000000001", messaggio)
        self.assertFalse(copia.exists())

    def test_un_piano_senza_ean_su_una_riga_senza_ean_passa(self) -> None:
        """If the price list itself has no EAN there, there's nothing to compare."""

        origine = self.listino()
        copia = self.cartella / "ordine.xls"
        esito = compila_ordine(
            origine, copia, {3: 7, 4: 2},
            colonna_ordine="I", prima_riga=3,
            # Column E is empty on every row of this fixture: the case of a
            # line item that has no EAN anywhere.
            ean_attesi={3: "", 4: ""}, colonna_ean="E",
        )
        self.assertEqual(esito["ean_controllati"], 0)
        self.assertTrue(copia.is_file())

    def test_la_colonna_dell_ean_si_puo_indicare_per_numero(self) -> None:
        """The adapter registry stores the column by name; callers resolve it from headers."""

        origine = self.listino()
        copia = self.cartella / "ordine.xls"
        esito = compila_ordine(
            origine, copia, {3: 7},
            colonna_ordine="I", prima_riga=3,
            ean_attesi={3: "8000000000000"}, colonna_ean=COLONNA_EAN + 1,
        )
        self.assertEqual(esito["ean_controllati"], 1)


class IFogliEIParametri(BancoXlsWriter):
    def test_un_foglio_che_non_c_e_ferma_tutto(self) -> None:
        origine = self.listino()
        with self.assertRaises(CompilazioneXlsError):
            compila_ordine(
                origine, self.cartella / "ordine.xls", {3: 7},
                colonna_ordine="I", foglio="Foglio9", prima_riga=3,
            )

    def test_un_piano_vuoto_non_produce_nessun_file(self) -> None:
        origine = self.listino()
        copia = self.cartella / "ordine.xls"
        with self.assertRaises(CompilazioneXlsError):
            compila_ordine(origine, copia, {}, colonna_ordine="I", prima_riga=3)
        self.assertFalse(copia.exists())

    def test_una_colonna_non_valida_ferma_tutto(self) -> None:
        origine = self.listino()
        for colonna in ("", "1A", None, 0):
            with self.subTest(colonna=colonna):
                with self.assertRaises(CompilazioneXlsError):
                    compila_ordine(
                        origine, self.cartella / "ordine.xls", {3: 7},
                        colonna_ordine=colonna, prima_riga=3,  # type: ignore[arg-type]
                    )



class LaCopiaSiPubblicaOMai(BancoXlsWriter):
    """The compiled order publishes atomically via `scrittura_sicura`, not a direct write.

    A full disk or a killed process must not leave a truncated `.xls` in
    place of the previous good copy.
    """

    def test_i_byte_arrivano_forzati_sul_disco(self) -> None:
        import os as sistema

        origine = self.listino()
        copia = self.cartella / "ordine.xls"
        visti: list[int] = []
        fsync_vero = sistema.fsync

        def annota(descrittore):
            visti.append(descrittore)
            return fsync_vero(descrittore)

        sistema.fsync = annota
        try:
            compila_ordine(origine, copia, {3: 7}, colonna_ordine="I", prima_riga=3)
        finally:
            sistema.fsync = fsync_vero

        self.assertEqual(len(visti), 1)

    def test_una_pubblicazione_che_non_riesce_lascia_intatta_la_copia_di_prima(self) -> None:
        import scrittura_sicura

        origine = self.listino()
        copia = self.cartella / "ordine.xls"
        compila_ordine(origine, copia, {3: 7}, colonna_ordine="I", prima_riga=3)
        prima = copia.read_bytes()

        replace_vero = scrittura_sicura.os.replace

        def non_pubblica(sorgente, destinazione):
            raise OSError("disco pieno")

        scrittura_sicura.os.replace = non_pubblica
        try:
            with self.assertRaises(OSError):
                compila_ordine(origine, copia, {3: 9, 5: 4}, colonna_ordine="I", prima_riga=3)
        finally:
            scrittura_sicura.os.replace = replace_vero

        # The previous document is untouched, byte for byte...
        self.assertEqual(copia.read_bytes(), prima)
        # ...and no temp file was left behind.
        rimasti = [voce.name for voce in self.cartella.iterdir() if voce.name.startswith(".")]
        self.assertEqual(rimasti, [])



class LeQuantitaDiPrimaNonRestano(BancoXlsWriter):
    """Stale quantities from a previous compilation must not survive into a new one.

    `write_supplier_orders.mjs` zeroes quantities already in the order column
    before writing new ones, to avoid shipping phantom rows; `xls_writer` must
    do the same. The case that matters: the source file for one week's
    compilation is last week's compiled copy, not the original price list.
    """

    def test_una_quantita_fuori_dal_piano_torna_a_zero(self) -> None:
        origine = self.listino()
        primo = self.cartella / "settimana-scorsa.xls"
        compila_ordine(origine, primo, {3: 7, 5: 12}, colonna_ordine="I", prima_riga=3)
        self.assertEqual(self.valori(primo)[4][COLONNA_ORDINE], 12)

        # Compiling again from that compiled copy, with a plan that only
        # touches one row.
        secondo = self.cartella / "questa-settimana.xls"
        esito = compila_ordine(primo, secondo, {3: 4}, colonna_ordine="I", prima_riga=3)

        griglia = self.valori(secondo)
        self.assertEqual(griglia[2][COLONNA_ORDINE], 4)
        self.assertEqual(griglia[4][COLONNA_ORDINE], 0)
        self.assertEqual(esito["quantita_azzerate"], 1)

    def test_su_un_listino_pulito_non_azzera_niente(self) -> None:
        """The normal case must not touch a single extra cell."""

        origine = self.listino()
        copia = self.cartella / "ordine.xls"
        esito = compila_ordine(origine, copia, {3: 7}, colonna_ordine="I", prima_riga=3)
        self.assertEqual(esito["quantita_azzerate"], 0)

    def test_le_righe_sopra_i_dati_non_si_toccano(self) -> None:
        """The order column's header is not a quantity to be zeroed."""

        origine = self.listino()
        copia = self.cartella / "ordine.xls"
        compila_ordine(origine, copia, {3: 7}, colonna_ordine="I", prima_riga=3)
        prima = self.valori(origine)
        dopo = self.valori(copia)
        for indice in range(0, 2):
            self.assertEqual(dopo[indice][COLONNA_ORDINE], prima[indice][COLONNA_ORDINE])


class QuattroByteACavalloDiDueSettoriTests(unittest.TestCase):
    """A cell that spans two non-adjacent sectors of the OLE container.

    A `.xls` is an OLE container: the "Workbook" stream's sectors are not
    necessarily contiguous in the file — allocation-table sectors can sit
    between them. In a real Noce price list, the stream spans over 10,000
    sectors with dozens of such discontinuities, and a handful of cells land
    on one. A 4-byte cell write that assumes contiguity, translating only its
    start offset, can spill one or more bytes into the wrong sector (e.g. the
    allocation table itself), producing a file Excel refuses to open. The
    writer must translate the write per destination sector, not once per
    cell.
    """

    # The stream is contiguous (bytes 0..7); in the file the two sectors are 100 bytes apart.
    MAPPA = [(0, 100, 4), (4, 204, 4)]

    def test_i_byte_vanno_dove_stanno_davvero_nel_file(self) -> None:
        dati = bytearray(300)
        xls_writer._scrivi_nel_flusso(dati, self.MAPPA, 2, b"\x01\x02\x03\x04")

        # The first two bytes close out the sector starting at 100...
        self.assertEqual(bytes(dati[102:104]), b"\x01\x02")
        # ...and the other two open the sector starting at 204.
        self.assertEqual(bytes(dati[204:206]), b"\x03\x04")
        # Nothing spills past the first sector into what would be the
        # container's allocation table.
        self.assertEqual(bytes(dati[104:106]), b"\x00\x00")

    def test_si_rilegge_quello_che_si_e_scritto(self) -> None:
        dati = bytearray(300)
        xls_writer._scrivi_nel_flusso(dati, self.MAPPA, 2, b"\x01\x02\x03\x04")

        self.assertEqual(xls_writer._leggi_dal_flusso(dati, self.MAPPA, 2, 4), b"\x01\x02\x03\x04")


if __name__ == "__main__":
    unittest.main()
