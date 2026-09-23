"""L'elenco dei prodotti che nessun fornitore può dare.

Punto 5.  Quattro cose qui valgono da sole l'intero file, e sono quelle in cui
un'implementazione plausibile sbaglia in silenzio:

1. **Le sette colonne, in quest'ordine.**  Il foglio lo apre una persona che
   telefona ai fornitori: una colonna spostata non fa esplodere niente, fa
   leggere il prezzo dove c'è la quantità.  I valori si rileggono con
   `openpyxl` cella per cella, non si controlla che il file «esista».
2. **`righe` vuoto è un errore.**  Un foglio con le sole intestazioni finirebbe
   nell'elenco della compilazione e nello zip, e direbbe a chi lo apre che c'è
   qualcosa da reperire quando non c'è niente.
3. **Il nome porta l'em dash**, come i listini compilati, e deve attraversare
   `consegna.intestazione_allegato` senza uccidere la risposta HTTP: le
   intestazioni si scrivono in latin-1 e l'em dash crudo là dentro alza
   `UnicodeEncodeError` a corpo già promesso.
4. **Il nome si fa riconoscere da `consegna.tipo_file`.**  Se il giro non
   torna, il file nasce e poi viene scambiato per un listino, cioè spedito al
   fornitore.

Niente rete, niente `app/data/`: tutto in `tempfile.TemporaryDirectory()`.
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
    """Una riga verosimile, nella forma dichiarata dal contratto."""
    return {
        "ean": ean,
        "descrizione": descrizione,
        "quantita": quantita,
        "unita": unita,
        "ultimo_prezzo": ultimo_prezzo,
        "motivo": motivo,
    }


def offerta(stato: str, *, chiave: str = "status") -> dict:
    """Un'offerta ridotta a quello che `motivo` guarda davvero."""
    return {"supplierId": "larice", chiave: stato}


class CasoConCartella(unittest.TestCase):
    def setUp(self) -> None:
        self.temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporanea.cleanup)
        self.cartella = Path(self.temporanea.name)

    def scrivi_e_rileggi(self, righe: list[dict], momento: datetime = MOMENTO):
        """Scrive il foglio e lo riapre davvero: il ritorno è `(nome, foglio)`."""
        nome = da_reperire.scrivi(self.cartella, righe, momento)
        libro = load_workbook(self.cartella / nome)
        self.addCleanup(libro.close)
        return nome, libro.active


# ---------------------------------------------------------------------------
# Il nome
# ---------------------------------------------------------------------------

class NomeFileTests(unittest.TestCase):
    def test_nome_file(self) -> None:
        self.assertEqual(da_reperire.nome_file(MOMENTO), NOME_ATTESO)
        self.assertIn(f" {EM_DASH} ", da_reperire.nome_file(MOMENTO))

    def test_nome_file_usa_la_stessa_data_dei_listini(self) -> None:
        # Non una seconda tabella dei mesi: la stessa di `consegna`, quella che
        # non passa da strftime("%B") e quindi non dice "August".
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
        """`BaseHTTPRequestHandler` scrive le intestazioni in latin-1.

        Con l'em dash crudo la risposta muore a corpo già promesso: è lo stesso
        difetto già pagato sui nomi dei listini, e la difesa è la stessa
        funzione.  Qui si prova che il nome nuovo ci passa dentro.
        """

        nome = da_reperire.nome_file(MOMENTO)
        valore = consegna.intestazione_allegato(nome)
        valore.encode("latin-1")  # non deve sollevare
        with self.assertRaises(UnicodeEncodeError):
            f'attachment; filename="{nome}"'.encode("latin-1")
        self.assertIn('filename="Prodotti da reperire - 17 agosto 2026.xlsx"', valore)
        self.assertIn("filename*=UTF-8''", valore)

    def test_il_nome_prodotto_si_fa_riconoscere_dalla_consegna(self) -> None:
        """Il giro completo: il nome che scriviamo è quello che la consegna legge.

        Se questo non torna, il file nasce e poi viene contato come un listino,
        cioè spedito al fornitore dentro lo zip.
        """

        nome = da_reperire.nome_file(MOMENTO)
        self.assertEqual(consegna.tipo_file(nome), da_reperire.TIPO)
        self.assertEqual(da_reperire.TIPO, "da_reperire")
        self.assertTrue(consegna.e_documento(nome))


# ---------------------------------------------------------------------------
# Il motivo
# ---------------------------------------------------------------------------

class MotivoTests(unittest.TestCase):
    def test_tutti_non_trovato_vuol_dire_che_non_lo_ha_a_listino_nessuno(self) -> None:
        offerte = [offerta("NON_TROVATO"), offerta("NON_TROVATO"), offerta("NON_TROVATO")]
        self.assertEqual(da_reperire.motivo(offerte), "Nessun fornitore lo ha a listino")
        self.assertEqual(da_reperire.motivo(offerte), da_reperire.MOTIVO_NON_A_LISTINO)

    def test_basta_un_fornitore_che_lo_ha_a_listino_per_cambiare_frase(self) -> None:
        # Ce l'ha, ma l'offerta non è utilizzabile: a chi telefona conviene
        # richiamare lo stesso fornitore fra qualche giorno, non cercarne uno
        # nuovo.  Le due frasi dicono proprio questo.
        offerte = [offerta("NON_TROVATO"), offerta("ESATTO"), offerta("NON_TROVATO")]
        self.assertEqual(da_reperire.motivo(offerte), "Nessun fornitore lo ha disponibile")
        self.assertEqual(da_reperire.motivo(offerte), da_reperire.MOTIVO_NON_DISPONIBILE)

    def test_nessuna_offerta_e_il_caso_limite_di_nessuno_a_listino(self) -> None:
        for vuoto in ([], (), None):
            self.assertEqual(da_reperire.motivo(vuoto), da_reperire.MOTIVO_NON_A_LISTINO)

    def test_legge_anche_match_status_quando_status_manca(self) -> None:
        # Le offerte del confronto portano lo stesso valore in due campi; se un
        # giorno ne arrivasse una col solo `matchStatus`, dire «non lo ha a
        # listino nessuno» quando invece qualcuno ce l'ha sarebbe una bugia
        # nella colonna che l'utente legge.
        self.assertEqual(
            da_reperire.motivo([offerta("NON_TROVATO", chiave="matchStatus")]),
            da_reperire.MOTIVO_NON_A_LISTINO,
        )
        self.assertEqual(
            da_reperire.motivo([offerta("ESATTO", chiave="matchStatus")]),
            da_reperire.MOTIVO_NON_DISPONIBILE,
        )

    def test_uno_stato_illeggibile_non_diventa_non_trovato(self) -> None:
        # `None`, la stringa vuota, un'offerta che non è nemmeno un dizionario:
        # non si può affermare che non sia a listino, e la frase prudente è
        # l'altra.
        for offerte in (
            [{"supplierId": "larice"}],
            [{"status": ""}],
            [{"status": None}],
            ["non un'offerta"],
            [offerta("NON_TROVATO"), "non un'offerta"],
        ):
            self.assertEqual(da_reperire.motivo(offerte), da_reperire.MOTIVO_NON_DISPONIBILE, repr(offerte))

    def test_motivo_non_guarda_il_disco_ne_il_server(self) -> None:
        # È una funzione pura: la stessa lista dà sempre la stessa frase e non
        # lascia niente dietro di sé.
        offerte = [offerta("NON_TROVATO")]
        self.assertEqual(da_reperire.motivo(offerte), da_reperire.motivo(offerte))
        self.assertEqual(offerte, [{"supplierId": "larice", "status": "NON_TROVATO"}])


# ---------------------------------------------------------------------------
# Il file
# ---------------------------------------------------------------------------

class ScriviTests(CasoConCartella):
    def test_il_file_nasce_con_il_nome_giusto_e_lo_restituisce(self) -> None:
        nome = da_reperire.scrivi(self.cartella, [riga()], MOMENTO)
        self.assertEqual(nome, NOME_ATTESO)
        self.assertTrue((self.cartella / nome).is_file())
        # Nessun temporaneo rimasto indietro.
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

        self.assertEqual(foglio.max_row, 3)  # intestazioni + due prodotti
        self.assertEqual(foglio["A2"].value, "8005905000123")
        self.assertEqual(foglio["B2"].value, "Caffè macinato 250 g")
        self.assertEqual(foglio["C2"].value, 4)
        self.assertEqual(foglio["D2"].value, 3.9)
        self.assertEqual(foglio["E2"].value, "Nessun fornitore lo ha a listino")
        self.assertIsNone(foglio["F2"].value)  # la riempie l'utente

        self.assertEqual(foglio["A3"].value, "8001234000999")
        self.assertEqual(foglio["B3"].value, "Espositore natalizio")
        self.assertEqual(foglio["E3"].value, "Nessun fornitore lo ha disponibile")

    def test_l_ean_resta_testo_e_non_diventa_notazione_scientifica(self) -> None:
        """Tredici cifre trattate da numero diventano `8.0059e+12`.

        Chi copia quel codice per cercarlo su un altro listino trova zero
        risultati, e non ha modo di capire perché.
        """

        _, foglio = self.scrivi_e_rileggi([riga(ean="8005905000123")])
        cella = foglio["A2"]
        self.assertIsInstance(cella.value, str)
        self.assertEqual(cella.value, "8005905000123")
        self.assertEqual(cella.data_type, "s")

    def test_un_ean_gia_numerico_arriva_lo_stesso_come_testo(self) -> None:
        # Il chiamante potrebbe passarlo come intero: il foglio non deve
        # cambiare faccia a seconda di come è tipizzato a monte.
        _, foglio = self.scrivi_e_rileggi([riga(ean=8005905000123)])
        self.assertEqual(foglio["A2"].value, "8005905000123")

    def test_il_prezzo_mancante_lascia_la_cella_vuota(self) -> None:
        """Vuoto e zero dicono due cose diverse.

        Uno zero in quella colonna direbbe che quel prodotto costava zero, che
        è un'informazione falsa; vuoto dice «non lo sappiamo», che è la verità.
        """

        _, foglio = self.scrivi_e_rileggi([riga(ultimo_prezzo=None)])
        self.assertIsNone(foglio["D2"].value)
        self.assertNotEqual(foglio["D2"].value, 0)

    def test_il_prezzo_presente_resta_un_numero(self) -> None:
        # Un prezzo scritto come testo non si somma e non si ordina.
        _, foglio = self.scrivi_e_rileggi([riga(ultimo_prezzo=12.5)])
        self.assertEqual(foglio["D2"].value, 12.5)
        self.assertIsInstance(foglio["D2"].value, (int, float))

    def test_i_colli_restano_un_numero_e_l_unita_diversa_entra_nella_cella(self) -> None:
        """L'intestazione resta «Colli richiesti» e l'elenco mescola le due cose.

        Tre espositori scritti come `3` sotto quell'intestazione si leggono come
        tre colli: chi telefona ordinerebbe la cosa sbagliata.
        """

        _, foglio = self.scrivi_e_rileggi([
            riga(quantita=4, unita="colli"),
            riga(quantita=3, unita="espositori"),
            riga(quantita=2, unita=""),
            riga(quantita=1, unita="Colli"),
        ])
        self.assertEqual(foglio["C2"].value, 4)
        self.assertEqual(foglio["C3"].value, "3 espositori")
        self.assertEqual(foglio["C4"].value, 2)  # unità assente: sono colli
        self.assertEqual(foglio["C5"].value, 1)  # maiuscola o minuscola è lo stesso
        self.assertIsInstance(foglio["C2"].value, int)

    def test_righe_vuote_sono_un_errore_del_chiamante_e_non_lasciano_file(self) -> None:
        """Chi chiama non deve mai creare un file vuoto.

        Un foglio con le sole intestazioni finirebbe nell'elenco della
        compilazione e nello zip, e direbbe a chi lo apre che c'è qualcosa da
        reperire quando non c'è niente.
        """

        for vuoto in ([], (), None):
            with self.assertRaises(ValueError, msg=repr(vuoto)):
                da_reperire.scrivi(self.cartella, vuoto, MOMENTO)
        self.assertEqual(list(self.cartella.iterdir()), [])

    def test_una_riga_a_cui_manca_un_campo_non_fa_saltare_la_compilazione(self) -> None:
        # La cartella datata viene cancellata se qualcosa esplode qui dentro:
        # un campo mancante non deve costare l'intera compilazione.
        _, foglio = self.scrivi_e_rileggi([{"descrizione": "Solo la descrizione"}])
        self.assertIsNone(foglio["A2"].value)
        self.assertEqual(foglio["B2"].value, "Solo la descrizione")
        self.assertIsNone(foglio["C2"].value)
        self.assertIsNone(foglio["D2"].value)
        self.assertIsNone(foglio["E2"].value)

    def test_due_scritture_nella_stessa_cartella_non_si_sommano(self) -> None:
        # Stesso momento, stesso nome: la seconda riscrive, non appende.
        da_reperire.scrivi(self.cartella, [riga(), riga()], MOMENTO)
        _, foglio = self.scrivi_e_rileggi([riga()])
        self.assertEqual(foglio.max_row, 2)

    def test_il_file_e_un_xlsx_vero_e_apribile(self) -> None:
        # `openpyxl` non era mai stato usato per **scrivere** in produzione:
        # questa è la prova che quello che esce si riapre.
        nome = da_reperire.scrivi(self.cartella, [riga()], MOMENTO)
        crudo = (self.cartella / nome).read_bytes()
        self.assertTrue(crudo.startswith(b"PK"))  # è uno zip, cioè un OOXML
        libro = load_workbook(self.cartella / nome)
        self.addCleanup(libro.close)
        self.assertEqual(libro.sheetnames, [da_reperire.NOME_FOGLIO])

    def test_niente_formule_dentro_le_celle(self) -> None:
        # Il foglio lo apre una persona, non un motore di calcolo: una formula
        # qui dentro sarebbe una cosa in più che può rompersi.
        nome = da_reperire.scrivi(self.cartella, [riga()], MOMENTO)
        libro = load_workbook(self.cartella / nome)
        self.addCleanup(libro.close)
        for riga_celle in libro.active.iter_rows():
            for cella in riga_celle:
                self.assertNotEqual(cella.data_type, "f", cella.coordinate)

    def test_una_descrizione_o_un_ean_che_comincia_per_uguale_non_diventa_formula(self) -> None:
        """EAN e descrizione arrivano dai listini dei fornitori: testo che non
        controlliamo.  openpyxl scrive come FORMULA una stringa che comincia
        per «=», e chi apre il foglio per telefonare in giro si troverebbe un
        calcolo (o un `#NAME?`) al posto del nome del prodotto o del codice.

        L'asserzione è su `data_type` alla rilettura, non sulla forma
        dell'XML: openpyxl serializza l'elemento vuoto in modo diverso a
        seconda che trovi `lxml` installato o no, e un test che guardasse
        l'XML passerebbe su una macchina e fallirebbe sull'altra a parità
        di codice.
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
        # Senza, la descrizione e le note escono tagliate e vanno allargate a
        # mano ogni volta.
        nome = da_reperire.scrivi(self.cartella, [riga()], MOMENTO)
        libro = load_workbook(self.cartella / nome)
        self.addCleanup(libro.close)
        for lettera in ("A", "B", "C", "D", "E", "F", "G"):
            self.assertGreater(libro.active.column_dimensions[lettera].width, 0, lettera)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
