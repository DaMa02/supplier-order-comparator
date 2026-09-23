"""La compilazione dell'ordine Noce dentro una copia del loro `.xls` (6e).

A Noce si rimanda il **loro** documento, con la sola colonna d'ordine
riempita e tutto il resto identico: e' una decisione di Daniele del 12 agosto
2026, non un'ottimizzazione.  Quattro cose valgono da sole l'intero file:

1. **Si scrive in posizione, e il resto del file non si sposta.**  Il collaudo
   non guarda solo le celle rilette: conta i **byte diversi** fra originale e
   copia, perche' una patch che ricostruisce il file darebbe le stesse celle e
   consegnerebbe un documento diverso da quello che il fornitore aspetta.
2. **La condizione da cui dipende tutto va ricontrollata a ogni file.**  Una
   formula, una cella vuota o un numero lungo otto byte nella colonna d'ordine
   rendono la patch inapplicabile: allora la compilazione Noce **fallisce e
   lo dice**, e non lascia in giro una copia a meta'.
3. **L'EAN della riga dev'essere quello del piano.**  E' l'unico controllo che
   avrebbe intercettato il difetto del 12 agosto — la riga 2600 che era olio
   Carapelli invece del prodotto atteso.
4. ⚠ **Cambiare una cella non sporca le formule che la usano.**  Misurato:
   dopo la patch le quantita' erano giuste e i totali fermi a zero. Per questo
   si azzera `RECALCID`, cosi' Excel rifa' i conti aprendo il file.

I `.xls` di prova li costruisce `test_xls_reader`: contenitore OLE2 vero,
record BIFF8 veri, sia nel mini-stream sia nei settori normali.
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
    """Un listino verosimile: EAN in B, descrizione in D, prezzo in H, ordine in I.

    Le prime due righe fanno da intestazione, i dati cominciano alla riga 3
    (numerata come la vede l'utente): la stessa forma del listino vero, dove
    l'intestazione sta alla riga 5 e i dati alla 6.
    """

    celle = banco.label(0, COLONNA_EAN, "CodiceABarre") + banco.label(0, COLONNA_ORDINE, "Quantita")
    for indice in range(righe):
        riga = indice + 2  # zero-based: la riga 3 dell'utente
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
        """La prova che questa è una patch e non una riscrittura."""

        origine = self.listino()
        copia = self.cartella / "ordine.xls"
        compila_ordine(origine, copia, {3: 7, 5: 12}, colonna_ordine="I", prima_riga=3)
        prima = origine.read_bytes()
        dopo = copia.read_bytes()
        self.assertEqual(len(prima), len(dopo))
        diversi = [indice for indice in range(len(prima)) if prima[indice] != dopo[indice]]
        # Due celle da quattro byte piu' i quattro di RECALCID: mai piu' di dodici.
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
        """Il mini-stream e i settori grandi sono due strade diverse, e vanno provate tutte e due."""

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
        # La cella accanto, dentro lo stesso record, non dev'essere stata toccata.
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
        """Senza, le quantità sono giuste e i totali restano a zero. Misurato."""

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
        """Un file senza quel record Excel lo ricalcola comunque all'apertura."""

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
        # ⚠ Niente copia a meta': un documento che somiglia a un ordine e non lo
        # e' e' peggio di nessun documento.
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
        """`MULBLANK` e' un record a se', e nel listino vero ce ne sono 4.578.

        Le celle vuote in fila Excel non le scrive una per una: le impacchetta
        in un solo record che ne copre un tratto.  Il ramo che lo apre non era
        mai stato attraversato da nessuna prova, e senza di quello una colonna
        d'ordine vuota per tutta la sua larghezza sarebbe passata per compilabile
        — cioe' la patch a lunghezza fissa avrebbe scritto quattro byte dentro
        celle che quei quattro byte non ce li hanno.
        """

        # Un solo record che copre le colonne 7, 8 e 9: la colonna d'ordine e' l'8.
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
        """Zero celle scrivibili non e' «tutto a posto»: e' una colonna che non c'e'.

        `not altre` diceva «compilabile» anche li': il controllo preventivo non
        trovava niente da segnalare, e la compilazione falliva piu' avanti con
        «Il piano indica N righe che nel listino non hanno una cella d'ordine».
        Il campo `celle_rk: 0` c'era gia', e nessuno lo guardava.
        """

        # Una colonna oltre quelle scritte: nel listino non c'e' nessuna cella.
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
        """La difesa che gli altri tre fornitori non hanno, e che qui costa zero."""

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
        """Il ramo che prima saltava la guardia in silenzio.

        Il piano che non porta l'EAN di una riga la sta scegliendo per il solo
        numero di riga.  Se nel listino quell'EAN c'e', la difesa che avrebbe
        intercettato il difetto del 12 agosto — la riga 2600 che era olio
        Carapelli — non si puo' fare: e allora non si compila.  Prima si
        passava oltre senza dire niente, e `ean_controllati` diceva «zero»
        esattamente come quando l'EAN non lo chiede nessuno.
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
        """Se nemmeno il listino ha l'EAN lì non c'è niente da confrontare."""

        origine = self.listino()
        copia = self.cartella / "ordine.xls"
        esito = compila_ordine(
            origine, copia, {3: 7, 4: 2},
            colonna_ordine="I", prima_riga=3,
            # La colonna E in questo listino e' vuota su tutte le righe: e' il
            # caso della riga di servizio che l'EAN non ce l'ha da nessuna parte.
            ean_attesi={3: "", 4: ""}, colonna_ean="E",
        )
        self.assertEqual(esito["ean_controllati"], 0)
        self.assertTrue(copia.is_file())

    def test_la_colonna_dell_ean_si_puo_indicare_per_numero(self) -> None:
        """Nel registro sta per nome, e chi chiama la risolve dalle intestazioni."""

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
    """⚠ Era l'unico posto del programma che apriva e troncava il file finale.

    Le cinque memorie passano tutte da `scrittura_sicura` — temporaneo, `fsync`,
    `os.replace` — e questo documento, che e' quello che va davvero a Noce,
    lo scriveva con un `write_bytes` diretto. Disco pieno o processo ucciso a
    meta' lasciavano al suo posto un `.xls` monco; se la destinazione esisteva
    gia', al posto della copia buona di prima.

    Prove eseguite.
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

        # Il documento di prima e' ancora quello, byte per byte…
        self.assertEqual(copia.read_bytes(), prima)
        # …e non e' rimasto niente in giro.
        rimasti = [voce.name for voce in self.cartella.iterdir() if voce.name.startswith(".")]
        self.assertEqual(rimasti, [])



class LeQuantitaDiPrimaNonRestano(BancoXlsWriter):
    """I due scrittori facevano due cose diverse, e questo era il piu' permissivo.

    `write_supplier_orders.mjs` azzera le quantita' gia' scritte nella colonna
    d'ordine — «senza, si spedirebbero righe fantasma» — e qui si scrivevano
    solo le righe del piano. Il caso che morde e' quello che capita davvero:
    come origine finisce la copia compilata della settimana prima, e Noce
    riceve anche le sue righe mentre gli altri fornitori no.

    Prove eseguite.
    """

    def test_una_quantita_fuori_dal_piano_torna_a_zero(self) -> None:
        origine = self.listino()
        primo = self.cartella / "settimana-scorsa.xls"
        compila_ordine(origine, primo, {3: 7, 5: 12}, colonna_ordine="I", prima_riga=3)
        self.assertEqual(self.valori(primo)[4][COLONNA_ORDINE], 12)

        # La settimana dopo si riparte per sbaglio dalla copia compilata, e il
        # piano nuovo tocca una riga sola.
        secondo = self.cartella / "questa-settimana.xls"
        esito = compila_ordine(primo, secondo, {3: 4}, colonna_ordine="I", prima_riga=3)

        griglia = self.valori(secondo)
        self.assertEqual(griglia[2][COLONNA_ORDINE], 4)
        self.assertEqual(griglia[4][COLONNA_ORDINE], 0)
        self.assertEqual(esito["quantita_azzerate"], 1)

    def test_su_un_listino_pulito_non_azzera_niente(self) -> None:
        """La controprova: il caso normale non deve toccare una cella in piu'."""

        origine = self.listino()
        copia = self.cartella / "ordine.xls"
        esito = compila_ordine(origine, copia, {3: 7}, colonna_ordine="I", prima_riga=3)
        self.assertEqual(esito["quantita_azzerate"], 0)

    def test_le_righe_sopra_i_dati_non_si_toccano(self) -> None:
        """L'intestazione della colonna d'ordine non e' una quantita' da azzerare."""

        origine = self.listino()
        copia = self.cartella / "ordine.xls"
        compila_ordine(origine, copia, {3: 7}, colonna_ordine="I", prima_riga=3)
        prima = self.valori(origine)
        dopo = self.valori(copia)
        for indice in range(0, 2):
            self.assertEqual(dopo[indice][COLONNA_ORDINE], prima[indice][COLONNA_ORDINE])


class QuattroByteACavalloDiDueSettoriTests(unittest.TestCase):
    """Una cella spezzata fra due settori non adiacenti del contenitore OLE.

    Un `.xls` e' un contenitore OLE: il flusso «Workbook» sta in settori che nel
    file **non sono per forza uno dietro l'altro** — fra loro si infilano i
    settori della tabella di allocazione. Nel listino Noce vero ci sono
    10.652 segmenti e **84 discontinuita'**, e cinque celle ci cadono sopra.

    Fino al 26 agosto 2026 si scriveva `dati[posizione:posizione + 4]`, cioe'
    quattro byte di fila a partire da un offset tradotto per il primo soltanto:
    per quelle celle uno o piu' byte finivano dentro il settore sbagliato — la
    tabella di allocazione — e Excel si rifiutava di aprire il documento.

    Non si vedeva prima perche' si scrivevano le sole righe del piano, un
    centinaio su diciassettemila. Dal 23 agosto si azzerano tutte le celle della
    colonna, e da allora quelle cinque vengono toccate a ogni compilazione.
    """

    # Il flusso e' contiguo (0..7); nel file i due settori distano 100 byte.
    MAPPA = [(0, 100, 4), (4, 204, 4)]

    def test_i_byte_vanno_dove_stanno_davvero_nel_file(self) -> None:
        dati = bytearray(300)
        xls_writer._scrivi_nel_flusso(dati, self.MAPPA, 2, b"\x01\x02\x03\x04")

        # I primi due byte chiudono il settore che comincia a 100...
        self.assertEqual(bytes(dati[102:104]), b"\x01\x02")
        # ...e gli altri due aprono quello che comincia a 204.
        self.assertEqual(bytes(dati[204:206]), b"\x03\x04")
        # E soprattutto: NIENTE e' finito di fila dopo il primo settore, che e'
        # dove stava la tabella di allocazione del contenitore.
        self.assertEqual(bytes(dati[104:106]), b"\x00\x00")

    def test_si_rilegge_quello_che_si_e_scritto(self) -> None:
        dati = bytearray(300)
        xls_writer._scrivi_nel_flusso(dati, self.MAPPA, 2, b"\x01\x02\x03\x04")

        self.assertEqual(xls_writer._leggi_dal_flusso(dati, self.MAPPA, 2, 4), b"\x01\x02\x03\x04")


if __name__ == "__main__":
    unittest.main()
