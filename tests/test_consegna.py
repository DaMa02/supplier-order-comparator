"""La consegna: cartelle datate, elenco, difese sul percorso, zip, intestazione.

Fase 6d.  Tre punti valgono da soli l'intero file, e sono quelli in cui una
implementazione plausibile sbaglia in silenzio:

1. **La cartella si crea in modo atomico.**  Un `if not exists(): mkdir()` e'
   una corsa: due compilazioni nello stesso minuto tornerebbero la *stessa*
   cartella e si sovrascriverebbero, cioe' il difetto che la 6d chiude.  Qui la
   corsa si prova due volte: davvero (due chiamate con lo stesso `momento`) e
   simulata (il `mkdir` perde la corsa e risponde `FileExistsError`).
2. **Le difese sul percorso sono a lista bianca.**  Il file bersaglio fuori
   dalla cartella viene creato **davvero** e si verifica prima che sia
   leggibile: altrimenti la prova passerebbe anche con la difesa spenta.  Lo
   stesso per il flusso alternativo NTFS `compilazione.json::$DATA`, che su
   questa macchina restituisce il contenuto del file (misurato) e che nessuna
   lista nera di caratteri fermerebbe.
3. **L'intestazione di scaricamento si codifica in latin-1.**
   `BaseHTTPRequestHandler` scrive le intestazioni in latin-1: con l'em dash
   crudo la risposta muore a corpo gia' promesso.  Il test prova proprio
   `.encode("latin-1")`.

Niente rete, niente `app/data/`: tutto in `tempfile.TemporaryDirectory()`.
"""

from __future__ import annotations

import io
import json
import locale
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parents[1]
for cartella in (SKILL_ROOT / "app",):
    if str(cartella) not in sys.path:
        sys.path.insert(0, str(cartella))

import consegna  # noqa: E402


MOMENTO = datetime(2026, 8, 12, 14, 35, 7)
EM_DASH = "\u2014"
NOME_LISTINO_LARICE = f"Ordine LARICE {EM_DASH} 12 agosto 2026.xlsx"
CONTENUTO_SEGRETO = b"chiave-che-non-deve-uscire"

# L'elenco dei prodotti che nessun fornitore puo' dare (punto 5).  Il nome si
# scrive qui a mano e non si chiede a `da_reperire`: e' `consegna` che deve
# riconoscerlo dal solo nome quando la cartella si rilegge senza audit, e un
# test che se lo facesse dare dal modulo che lo produce passerebbe anche se
# tutti e due cambiassero insieme senza dirlo a nessuno.
NOME_DA_REPERIRE = f"Prodotti da reperire {EM_DASH} 12 agosto 2026.xlsx"

# Il piano del vecchio carrello sul sito Noce.  Quel percorso e' stato
# smontato e il programma non scrive piu' niente del genere, ma le cartelle
# d'ordine fatte prima ce l'hanno ancora dentro: e' il nome sconosciuto su cui
# si prova il ripiego generico di `tipo_file`.
NOME_DI_UN_ALTRO_TEMPO = "PIANO_CARRELLO_NOCE.json"


def audit_finto(
    nome_cartella: str,
    *,
    creato_il: str,
    fornitori: tuple[str, ...] = ("larice",),
    file: tuple[dict, ...] = (),
) -> dict:
    """Un `compilazione.json` verosimile, nella forma del §3 del contratto."""
    return {
        "schema_audit": consegna.SCHEMA_AUDIT,
        "cartella": nome_cartella,
        "run_id": "run-di-prova",
        "creato_il": creato_il,
        "stato": "FILES_READY",
        "messaggio": "Piano ordini JSON e copie dei listini generati.",
        "fornitori": list(fornitori),
        "totali_netti": {identificativo: 100.0 for identificativo in fornitori},
        "totale_netto": 100.0 * len(fornitori),
        "righe": 42,
        "sotto_soglia": [],
        "avvisi": [],
        "file": list(file) or [
            {"nome": NOME_LISTINO_LARICE, "tipo": "listino", "fornitore": "larice", "byte": 12},
            {"nome": consegna.NOME_PIANO, "tipo": "piano", "byte": 3},
        ],
    }


class CasoConCartella(unittest.TestCase):
    """Banco comune: una radice degli ordini dentro una cartella temporanea."""

    def setUp(self) -> None:
        self.temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporanea.cleanup)
        self.base = Path(self.temporanea.name)
        self.radice = self.base / "ordini"
        self.radice.mkdir()

    def compilazione(self, nome: str, *, creato_il: str | None = None, con_audit: bool = True) -> Path:
        cartella = self.radice / nome
        cartella.mkdir()
        (cartella / NOME_LISTINO_LARICE).write_bytes(b"finto xlsx")
        (cartella / consegna.NOME_PIANO).write_bytes(b"{}")
        if con_audit:
            consegna.scrivi_audit(
                cartella,
                audit_finto(nome, creato_il=creato_il or "2026-08-12T14:35:07.421+02:00"),
            )
        return cartella


# ---------------------------------------------------------------------------
# I nomi
# ---------------------------------------------------------------------------

class NomiTests(unittest.TestCase):
    def test_nome_cartella_non_contiene_mai_i_due_punti(self) -> None:
        # Windows non accetta `:` in un nome: l'ora si scrive `1435`.
        for minuto in range(0, 60, 7):
            nome = consegna.nome_cartella(datetime(2026, 8, 12, 14, minuto))
            self.assertNotIn(":", nome)
        self.assertEqual(consegna.nome_cartella(MOMENTO), "2026-08-12_1435")

    def test_nome_listino_mese_lungo_e_mese_corto(self) -> None:
        lungo = consegna.nome_listino("larice", datetime(2026, 9, 3, 8, 5))
        corto = consegna.nome_listino("betulla", datetime(2026, 3, 30, 18, 0))
        self.assertEqual(lungo, f"Ordine LARICE {EM_DASH} 3 settembre 2026.xlsx")
        self.assertEqual(corto, f"Ordine BETULLA {EM_DASH} 30 marzo 2026.xlsx")
        self.assertIn(EM_DASH, lungo)
        self.assertIn(f" {EM_DASH} ", lungo)

    def test_nome_listino_ripulisce_i_caratteri_vietati_da_windows(self) -> None:
        prodotto = consegna.nome_listino('gri:eco/lo*co?', MOMENTO)
        self.assertEqual(prodotto, f"Ordine GRI_ECO_LO_CO_ {EM_DASH} 12 agosto 2026.xlsx")
        for vietato in '<>:"/\\|?*':
            self.assertNotIn(vietato, prodotto.replace(EM_DASH, ""))

    def test_nome_listino_sostituisce_punti_e_spazi_in_coda(self) -> None:
        # Windows li toglierebbe in silenzio: `larice.` e `larice` finirebbero
        # nello stesso file senza che nessuno se ne accorga.
        self.assertEqual(
            consegna.nome_listino("larice. ", MOMENTO),
            f"Ordine LARICE__ {EM_DASH} 12 agosto 2026.xlsx",
        )

    def test_data_leggibile_non_dipende_dalla_localizzazione(self) -> None:
        try:
            precedente = locale.setlocale(locale.LC_TIME)
            locale.setlocale(locale.LC_TIME, "C")
        except (locale.Error, ValueError):  # pragma: no cover - dipende dal sistema
            self.skipTest("la localizzazione 'C' non è disponibile su questo sistema")
        self.addCleanup(locale.setlocale, locale.LC_TIME, precedente)
        # Con LC_TIME=C, strftime("%B") direbbe "August": e' la prova che i nomi
        # dei mesi vengono da MESI e non dal sistema.
        self.assertEqual(MOMENTO.strftime("%B"), "August")
        self.assertEqual(consegna.data_leggibile(MOMENTO), "12 agosto 2026")
        self.assertIn("agosto", consegna.nome_listino("larice", MOMENTO))

    def test_data_leggibile_tutti_i_mesi(self) -> None:
        attesi = [
            "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
            "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre",
        ]
        for numero, atteso in enumerate(attesi, start=1):
            self.assertEqual(consegna.data_leggibile(datetime(2026, numero, 1)), f"1 {atteso} 2026")

    def test_etichetta(self) -> None:
        self.assertEqual(consegna.etichetta("2026-08-12_1435"), "12 agosto 2026, 14:35")
        self.assertEqual(consegna.etichetta("2026-08-12_1435_2"), "12 agosto 2026, 14:35 (2)")
        self.assertEqual(consegna.etichetta("2026-01-05_0007"), "5 gennaio 2026, 00:07")

    def test_etichetta_di_un_nome_illeggibile_torna_il_nome(self) -> None:
        # Una cartella creata a mano non deve far esplodere l'elenco.
        for nome in ("cartella mia", "", "2026-13-45_9999", "2026-02-30_1435", "2026-08-12"):
            self.assertEqual(consegna.etichetta(nome), nome)

    def test_nome_zip(self) -> None:
        self.assertEqual(
            consegna.nome_zip("2026-08-12_1435"),
            "Listini pronti per invio 2026-08-12 14-35.zip",
        )
        self.assertEqual(
            consegna.nome_zip("2026-08-12_1435_2"),
            "Listini pronti per invio 2026-08-12 14-35 (2).zip",
        )

    def test_nome_zip_di_un_nome_illeggibile(self) -> None:
        self.assertEqual(
            consegna.nome_zip("cartella:mia"),
            "Listini pronti per invio cartella_mia.zip",
        )


# ---------------------------------------------------------------------------
# La cartella
# ---------------------------------------------------------------------------

class CreaCartellaTests(CasoConCartella):
    def test_due_compilazioni_nello_stesso_minuto_danno_due_cartelle(self) -> None:
        prima = consegna.crea_cartella(self.radice, MOMENTO)
        seconda = consegna.crea_cartella(self.radice, MOMENTO)
        self.assertNotEqual(prima, seconda)
        self.assertTrue(prima.is_dir())
        self.assertTrue(seconda.is_dir())
        self.assertEqual(prima.name, "2026-08-12_1435")
        self.assertEqual(seconda.name, "2026-08-12_1435_2")
        # Nessuna sovrascrittura: quello che sta nella prima ci resta.
        (prima / "listino.xlsx").write_bytes(b"prima")
        terza = consegna.crea_cartella(self.radice, MOMENTO)
        self.assertEqual(terza.name, "2026-08-12_1435_3")
        self.assertEqual((prima / "listino.xlsx").read_bytes(), b"prima")

    def test_crea_cartella_si_fida_di_file_exists_error_e_non_di_exists(self) -> None:
        # La corsa vera: due processi chiedono lo stesso minuto e il nostro
        # `mkdir` arriva secondo.  Con `exist_ok=True` questa risposta non
        # arriverebbe mai e le due compilazioni scriverebbero nella stessa
        # cartella; con `if not exists(): mkdir()` la cartella non esiste ancora
        # nel momento del controllo e si finirebbe lo stesso li' dentro.
        base = consegna.nome_cartella(MOMENTO)
        mkdir_reale = Path.mkdir

        def mkdir_che_perde_la_corsa(self, *args, **kwargs):
            if self.name == base:
                raise FileExistsError(17, "l'ha creata un altro un istante fa")
            return mkdir_reale(self, *args, **kwargs)

        with mock.patch.object(Path, "mkdir", mkdir_che_perde_la_corsa):
            creata = consegna.crea_cartella(self.radice, MOMENTO)

        self.assertEqual(creata.name, f"{base}_2")
        self.assertTrue(creata.is_dir())
        self.assertFalse((self.radice / base).exists())

    def test_crea_cartella_crea_la_radice_mancante(self) -> None:
        radice = self.base / "mai" / "vista" / "ordini"
        creata = consegna.crea_cartella(radice, MOMENTO)
        self.assertTrue(creata.is_dir())
        self.assertEqual(creata.parent, radice)

    def test_crea_cartella_salta_i_suffissi_gia_occupati(self) -> None:
        base = consegna.nome_cartella(MOMENTO)
        (self.radice / base).mkdir()
        (self.radice / f"{base}_2").mkdir()
        self.assertEqual(consegna.crea_cartella(self.radice, MOMENTO).name, f"{base}_3")

    def test_crea_cartella_alza_valore_dopo_il_novantanove(self) -> None:
        base = consegna.nome_cartella(MOMENTO)
        (self.radice / base).mkdir()
        for numero in range(2, consegna.MAX_SUFFISSO + 1):
            (self.radice / f"{base}_{numero}").mkdir()
        with self.assertRaises(ValueError) as errore:
            consegna.crea_cartella(self.radice, MOMENTO)
        self.assertIn("stesso minuto", str(errore.exception))


class UltimaTests(CasoConCartella):
    def test_ultima_e_la_piu_recente(self) -> None:
        self.compilazione("2026-08-10_0900", creato_il="2026-08-10T09:00:00+02:00")
        recente = self.compilazione("2026-08-12_1435", creato_il="2026-08-12T14:35:00+02:00")
        self.compilazione("2026-08-11_1000", creato_il="2026-08-11T10:00:00+02:00")
        self.assertEqual(consegna.ultima(self.radice), recente)

    def test_ultima_senza_compilazioni(self) -> None:
        self.assertIsNone(consegna.ultima(self.radice))
        self.assertIsNone(consegna.ultima(self.base / "mai-vista"))

    def test_ultima_salta_una_cartella_che_non_e_una_compilazione(self) -> None:
        """Il riquadro nuovo insegna che `ordini` e' l'archivio dell'utente.

        Appena lui ci crea dentro una cartella sua — «da mandare a Larice», o la
        «Nuova cartella» di un clic sbagliato — quella diventa la piu' recente.
        Se `ultima()` la scegliesse, a chi ha appena compilato si risponderebbe
        che compilazioni non ce ne sono.
        """

        vera = self.compilazione("2026-08-12_1435", creato_il="2026-08-12T14:35:00+02:00")
        (self.radice / "da mandare a Larice").mkdir()

        self.assertEqual(
            [voce["cartella"] for voce in consegna.elenco(self.radice)][0],
            "da mandare a Larice",
            "la cartella dell'utente è davvero la più recente: senza questo il test non prova niente",
        )
        self.assertEqual(consegna.ultima(self.radice), vera)

    def test_ultima_salta_una_compilazione_a_cui_manca_il_piano(self) -> None:
        vera = self.compilazione("2026-08-12_1435", creato_il="2026-08-12T14:35:00+02:00")
        senza = self.compilazione("2026-08-12_1440", creato_il="2026-08-12T14:40:00+02:00")
        (senza / consegna.NOME_PIANO).unlink()

        self.assertEqual(consegna.ultima(self.radice), vera)


# ---------------------------------------------------------------------------
# L'audit
# ---------------------------------------------------------------------------

class AuditTests(CasoConCartella):
    def test_scrivi_audit_va_a_capo_con_lf_e_rilegge_lo_stesso_dizionario(self) -> None:
        cartella = self.radice / "2026-08-12_1435"
        cartella.mkdir()
        dati = audit_finto("2026-08-12_1435", creato_il="2026-08-12T14:35:07.421+02:00")
        percorso = consegna.scrivi_audit(cartella, dati)

        crudo = percorso.read_bytes()
        self.assertNotIn(b"\r\n", crudo)  # write_text su Windows lo farebbe
        self.assertIn(b"\n", crudo)
        self.assertTrue(crudo.endswith(b"\n"))
        self.assertEqual(percorso.name, consegna.NOME_AUDIT)
        self.assertEqual(consegna.leggi_audit(cartella), dati)

    def test_scrivi_audit_tiene_i_caratteri_non_ascii_e_non_lascia_temporanei(self) -> None:
        cartella = self.radice / "2026-08-12_1435"
        cartella.mkdir()
        consegna.scrivi_audit(cartella, {"file": [{"nome": NOME_LISTINO_LARICE}]})
        self.assertIn(EM_DASH, (cartella / consegna.NOME_AUDIT).read_bytes().decode("utf-8"))
        self.assertEqual(
            sorted(p.name for p in cartella.iterdir()),
            [consegna.NOME_AUDIT],
        )

    def test_scrivi_audit_non_distrugge_l_audit_di_prima_se_la_scrittura_muore(self) -> None:
        # E' la prova della scrittura atomica: senza temporaneo + os.replace, la
        # scrittura interrotta lascerebbe al suo posto un audit troncato, cioe'
        # un documento che dice il falso sulla compilazione.
        cartella = self.radice / "2026-08-12_1435"
        cartella.mkdir()
        buono = audit_finto("2026-08-12_1435", creato_il="2026-08-12T14:35:00+02:00")
        consegna.scrivi_audit(cartella, buono)

        write_bytes_reale = Path.write_bytes

        def write_bytes_che_muore(self, dati):
            write_bytes_reale(self, dati[:5])  # meta' scrittura
            raise OSError(28, "disco pieno")

        with mock.patch.object(Path, "write_bytes", write_bytes_che_muore):
            with self.assertRaises(OSError):
                consegna.scrivi_audit(cartella, {"schema_audit": 1, "nuovo": True})

        self.assertEqual(consegna.leggi_audit(cartella), buono)

    def test_leggi_audit_non_solleva_mai(self) -> None:
        cartella = self.radice / "2026-08-12_1435"
        cartella.mkdir()
        self.assertIsNone(consegna.leggi_audit(cartella))  # non c'e'
        self.assertIsNone(consegna.leggi_audit(self.base / "mai-vista"))  # nemmeno la cartella

        percorso = cartella / consegna.NOME_AUDIT
        for crudo in (b"{", b"[]", b'"testo"', b"", b"\xff\xfe{\x00", b"null"):
            percorso.write_bytes(crudo)
            self.assertIsNone(consegna.leggi_audit(cartella), crudo)

        percorso.write_bytes(b'{"schema_audit": 1}')
        self.assertEqual(consegna.leggi_audit(cartella), {"schema_audit": 1})


# ---------------------------------------------------------------------------
# L'elenco
# ---------------------------------------------------------------------------

class ElencoTests(CasoConCartella):
    def test_elenco_ordina_dalla_piu_recente(self) -> None:
        self.compilazione("2026-08-10_0900", creato_il="2026-08-10T09:00:00+02:00")
        self.compilazione("2026-08-12_1435", creato_il="2026-08-12T14:35:00+02:00")
        self.compilazione("2026-08-11_1000", creato_il="2026-08-11T10:00:00+02:00")
        self.assertEqual(
            [v["cartella"] for v in consegna.elenco(self.radice)],
            ["2026-08-12_1435", "2026-08-11_1000", "2026-08-10_0900"],
        )

    def test_elenco_ordina_per_creato_il_e_non_per_nome(self) -> None:
        # I nomi dicono una cosa e gli audit un'altra: comanda l'audit.
        self.compilazione("2026-08-10_0900", creato_il="2026-09-01T09:00:00+02:00")
        self.compilazione("2026-08-12_1435", creato_il="2026-07-01T14:35:00+02:00")
        self.assertEqual(
            [v["cartella"] for v in consegna.elenco(self.radice)],
            ["2026-08-10_0900", "2026-08-12_1435"],
        )

    def test_elenco_ripiega_sull_mtime_quando_l_audit_manca(self) -> None:
        vecchia = self.compilazione("2026-08-10_0900", con_audit=False)
        nuova = self.compilazione("2026-08-11_1000", con_audit=False)
        adesso = datetime.now(tz=timezone.utc).timestamp()
        os.utime(vecchia, (adesso - 10_000, adesso - 10_000))
        os.utime(nuova, (adesso, adesso))
        self.assertEqual(
            [v["cartella"] for v in consegna.elenco(self.radice)],
            ["2026-08-11_1000", "2026-08-10_0900"],
        )

    def test_elenco_a_parita_di_istante_ordina_per_nome_decrescente(self) -> None:
        istante = "2026-08-12T14:35:00+02:00"
        self.compilazione("2026-08-12_1435", creato_il=istante)
        self.compilazione("2026-08-12_1435_2", creato_il=istante)
        self.assertEqual(
            [v["cartella"] for v in consegna.elenco(self.radice)],
            ["2026-08-12_1435_2", "2026-08-12_1435"],
        )

    def test_un_documento_di_un_altro_tempo_e_altro_e_non_nasconde_la_cartella(self) -> None:
        """Il ripiego che regge le cartelle d'ordine fatte prima dello smontaggio.

        `tipo_file` non conosce piu' quel nome.  Deve cadere sulla
        classificazione generica — «altro» — senza contarlo fra i listini,
        senza far esplodere la voce e soprattutto senza far sparire dall'elenco
        una cartella che sul disco c'e'.

        L'audit si toglie apposta: con l'audit i tipi arrivano da la', e questo
        scenario proverebbe un'altra cosa.  Senza, l'elenco deve dedurre ogni
        tipo dal solo nome — cioe' passare davvero da `tipo_file`.
        """

        cartella = self.compilazione("2026-08-12_1435", creato_il="2026-08-12T14:35:00+02:00")
        (cartella / NOME_DI_UN_ALTRO_TEMPO).write_bytes(b"{}")
        (cartella / consegna.NOME_AUDIT).unlink()

        self.assertEqual(consegna.tipo_file(NOME_DI_UN_ALTRO_TEMPO), "altro")

        voci = consegna.elenco(self.radice)
        self.assertEqual([voce["cartella"] for voce in voci], ["2026-08-12_1435"])
        tipi = {riga["nome"]: riga["tipo"] for riga in voci[0]["file"]}
        self.assertEqual(tipi[NOME_DI_UN_ALTRO_TEMPO], "altro")
        self.assertEqual(voci[0]["listini"], 1)

    def test_un_tipo_che_il_programma_non_conosce_piu_arriva_com_e_scritto(self) -> None:
        """L'audit di allora lo dichiarava con un `tipo` che oggi non esiste piu'.

        Il tipo dell'audit passa tale e quale: la voce e' il racconto di quella
        compilazione, non una riscrittura fatta oggi.  Quello che conta e' che
        la cartella resti nell'elenco, che il conteggio dei listini non cambi e
        che il documento resti scaricabile.  La pagina, per un tipo che la sua
        tabella non conosce, non stampa nessuna etichetta.
        """

        cartella = self.radice / "2026-08-12_1435"
        cartella.mkdir()
        (cartella / NOME_LISTINO_LARICE).write_bytes(b"finto xlsx")
        (cartella / consegna.NOME_PIANO).write_bytes(b"{}")
        (cartella / NOME_DI_UN_ALTRO_TEMPO).write_bytes(b"{}")
        consegna.scrivi_audit(cartella, audit_finto(
            "2026-08-12_1435",
            creato_il="2026-08-12T14:35:00+02:00",
            file=(
                {"nome": NOME_LISTINO_LARICE, "tipo": "listino", "fornitore": "larice", "byte": 12},
                {"nome": consegna.NOME_PIANO, "tipo": "piano", "byte": 3},
                {"nome": NOME_DI_UN_ALTRO_TEMPO, "tipo": "piano_di_un_altro_tempo", "byte": 2},
            ),
        ))

        voci = consegna.elenco(self.radice)

        self.assertEqual([voce["cartella"] for voce in voci], ["2026-08-12_1435"])
        self.assertTrue(voci[0]["completa"])
        self.assertEqual(voci[0]["listini"], 1)
        self.assertEqual(voci[0]["mancanti"], [])
        riga = next(riga for riga in voci[0]["file"] if riga["nome"] == NOME_DI_UN_ALTRO_TEMPO)
        self.assertEqual(riga["tipo"], "piano_di_un_altro_tempo")
        self.assertEqual(riga["url"], f"/ordini/2026-08-12_1435/{NOME_DI_UN_ALTRO_TEMPO}")

    def test_elenco_di_una_radice_inesistente(self) -> None:
        self.assertEqual(consegna.elenco(self.base / "mai-vista"), [])

    def test_elenco_salta_i_file_e_le_cartelle_nascoste(self) -> None:
        self.compilazione("2026-08-12_1435")
        (self.radice / ".ordine-temporaneo-abc").mkdir()
        (self.radice / "un-file.txt").write_bytes(b"x")
        self.assertEqual([v["cartella"] for v in consegna.elenco(self.radice)], ["2026-08-12_1435"])

    @unittest.skipUnless(os.name == "nt", "la giunzione NTFS esiste solo su Windows")
    def test_elenco_non_mostra_una_giunzione_che_punta_fuori(self) -> None:
        """Si elenca solo quello che le rotte sanno servire.

        Una giunzione dentro `ordini` — un `mklink /J` verso una cartella di
        rete, un backup ripristinato male — comparirebbe come una compilazione,
        con dentro i nomi dei file di fuori e collegamenti che rispondono 404.
        """

        self.compilazione("2026-08-12_1435")
        fuori = self.base / "fuori"
        fuori.mkdir()
        (fuori / "segreto.txt").write_bytes(CONTENUTO_SEGRETO)
        giunzione = self.radice / "scorciatoia"
        esito = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(giunzione), str(fuori)],
            capture_output=True, text=True,
        )
        if esito.returncode != 0:  # pragma: no cover - dipende dal file system
            self.skipTest(f"mklink /J non disponibile: {esito.stdout} {esito.stderr}")
        self.addCleanup(lambda: os.rmdir(giunzione) if giunzione.exists() else None)
        # Senza questa riga il test passerebbe anche se la giunzione non fosse
        # nata: e' la prova che c'e' qualcosa da fermare.
        self.assertEqual((giunzione / "segreto.txt").read_bytes(), CONTENUTO_SEGRETO)

        voci = consegna.elenco(self.radice)

        self.assertEqual([voce["cartella"] for voce in voci], ["2026-08-12_1435"])
        self.assertNotIn("segreto.txt", json.dumps(voci, ensure_ascii=False))

    def test_elenco_tiene_la_cartella_con_l_audit_troncato(self) -> None:
        buona = self.compilazione("2026-08-12_1435", creato_il="2026-08-12T14:35:00+02:00")
        rotta = self.compilazione("2026-08-11_1000", creato_il="2026-08-11T10:00:00+02:00")
        (rotta / consegna.NOME_AUDIT).write_bytes(b"{")
        # Senza audit leggibile la chiave d'ordine e' l'mtime della cartella,
        # che altrimenti sarebbe "adesso" e la metterebbe per prima.
        vecchio = datetime(2026, 8, 11, 10, 0, tzinfo=timezone(timedelta(hours=2))).timestamp()
        os.utime(rotta, (vecchio, vecchio))

        voci = consegna.elenco(self.radice)  # non solleva
        self.assertEqual([v["cartella"] for v in voci], [buona.name, rotta.name])
        incompleta = voci[1]
        self.assertFalse(incompleta["completa"])
        self.assertEqual(incompleta["stato"], "SCONOSCIUTO")
        self.assertEqual(incompleta["fornitori"], [])
        self.assertIsNone(incompleta["totaleNetto"])
        self.assertIsNone(incompleta["righe"])
        self.assertEqual(incompleta["etichetta"], "11 agosto 2026, 10:00")
        # Il tipo si deduce dall'estensione e lo zip resta possibile: i listini
        # ci sono anche se l'audit che li descriveva non si legge.
        nomi = {riga["nome"]: riga["tipo"] for riga in incompleta["file"]}
        self.assertEqual(nomi[NOME_LISTINO_LARICE], "listino")
        self.assertEqual(nomi[consegna.NOME_PIANO], "piano")
        # L'audit descrive la cartella: non e' merce da consegnare e non compare
        # fra i documenti, nemmeno quando e' proprio lui a essersi rotto.
        self.assertNotIn(consegna.NOME_AUDIT, nomi)
        self.assertEqual(incompleta["listini"], 1)
        self.assertEqual(incompleta["zipUrl"], "/ordini/2026-08-11_1000/zip")


class DerivaDelDiscoTests(CasoConCartella):
    """L'audit descrive la cartella nell'istante in cui viene scritto.

    Da li' in poi l'utente vive dentro quella cartella: allega un listino a una
    mail, lo sposta sul desktop, apre l'altro in Excel per ricontrollarlo. Una
    voce costruita dall'audit continuerebbe a contare documenti che non ci sono
    piu' e offrirebbe collegamenti che rispondono 404 — e lo zip, che chiede
    ogni nome dichiarato, si rifiuterebbe di consegnare anche i listini rimasti,
    dicendo all'utente che di listini non ce n'e' nessuno mentre ce n'e' uno che
    deve ancora spedire.
    """

    def setUp(self) -> None:
        super().setUp()
        self.cartella = self.compilazione("2026-08-12_1435")
        self.secondo = f"Ordine BETULLA {EM_DASH} 12 agosto 2026.xlsx"
        (self.cartella / self.secondo).write_bytes(b"finto xlsx betulla")
        consegna.scrivi_audit(self.cartella, audit_finto(
            "2026-08-12_1435",
            creato_il="2026-08-12T14:35:07.421+02:00",
            fornitori=("betulla", "larice"),
            file=(
                {"nome": NOME_LISTINO_LARICE, "tipo": "listino", "fornitore": "larice", "byte": 10},
                {"nome": self.secondo, "tipo": "listino", "fornitore": "betulla", "byte": 16},
                {"nome": consegna.NOME_PIANO, "tipo": "piano", "byte": 2},
            ),
        ))

    def test_di_partenza_i_due_listini_ci_sono(self) -> None:
        """Senza questa, le prove qui sotto passerebbero anche a difesa spenta."""

        letta = consegna.voce(self.cartella)
        self.assertEqual(letta["listini"], 2)
        self.assertEqual(letta["mancanti"], [])

    def test_un_listino_spostato_non_si_conta_piu_e_viene_dichiarato(self) -> None:
        (self.cartella / self.secondo).rename(self.base / self.secondo)

        letta = consegna.voce(self.cartella)

        self.assertEqual(letta["listini"], 1)
        self.assertNotIn(self.secondo, [riga["nome"] for riga in letta["file"]])
        self.assertEqual(letta["mancanti"], [self.secondo])
        # Il listino rimasto si consegna lo stesso: e' quello che l'utente deve
        # ancora mandare, e dirgli che non c'e' niente lo manderebbe a
        # ricompilare per nulla.
        self.assertEqual(letta["zipUrl"], "/ordini/2026-08-12_1435/zip")
        dentro = zipfile.ZipFile(io.BytesIO(consegna.zip_in_memoria(
            self.cartella, [riga["nome"] for riga in letta["file"] if riga["tipo"] == "listino"]
        ))).namelist()
        self.assertEqual(dentro, [NOME_LISTINO_LARICE])

    def test_un_documento_comparso_dopo_l_audit_non_e_un_listino(self) -> None:
        """Non sappiamo di chi sia né da dove venga: al fornitore non ci va."""

        (self.cartella / "Ordine di qualcun altro.xlsx").write_bytes(b"?")

        letta = consegna.voce(self.cartella)

        tipi = {riga["nome"]: riga["tipo"] for riga in letta["file"]}
        self.assertEqual(tipi["Ordine di qualcun altro.xlsx"], "altro")
        self.assertEqual(letta["listini"], 2)

    def test_il_file_di_proprieta_di_excel_non_entra_nella_consegna(self) -> None:
        """Basta aprire una copia per controllarla perche' Excel lo depositi.

        Senza il filtro finirebbe nello zip diretto al fornitore, e nella
        cartella senza audit verrebbe pure contato come un listino.
        """

        (self.cartella / f"~$Ordine LARICE {EM_DASH} 12 agosto 2026.xlsx").write_bytes(b"lock")
        (self.cartella / consegna.NOME_AUDIT).unlink()

        letta = consegna.voce(self.cartella)

        self.assertFalse(letta["completa"])
        self.assertEqual(letta["listini"], 2)
        self.assertNotIn(
            f"~$Ordine LARICE {EM_DASH} 12 agosto 2026.xlsx",
            [riga["nome"] for riga in letta["file"]],
        )

    def test_una_scrittura_atomica_colta_a_meta_non_e_un_documento(self) -> None:
        (self.cartella / f"{consegna.NOME_PIANO}.tmp").write_bytes(b"{}")
        self.assertNotIn(
            f"{consegna.NOME_PIANO}.tmp",
            [riga["nome"] for riga in consegna.voce(self.cartella)["file"]],
        )


class VoceTests(CasoConCartella):
    def test_voce_completa(self) -> None:
        cartella = self.compilazione("2026-08-12_1435", creato_il="2026-08-12T14:35:07.421+02:00")
        letta = consegna.voce(cartella)
        self.assertTrue(letta["completa"])
        self.assertEqual(letta["cartella"], "2026-08-12_1435")
        self.assertEqual(letta["etichetta"], "12 agosto 2026, 14:35")
        self.assertEqual(letta["creatoIl"], "2026-08-12T14:35:07.421+02:00")
        self.assertEqual(letta["stato"], "FILES_READY")
        self.assertEqual(letta["fornitori"], [{"id": "larice", "nome": "LARICE", "totaleNetto": 100.0}])
        self.assertEqual(letta["totaleNetto"], 100.0)
        self.assertEqual(letta["righe"], 42)
        self.assertEqual(letta["listini"], 1)
        self.assertEqual(letta["zipUrl"], "/ordini/2026-08-12_1435/zip")
        self.assertEqual(letta["zipNome"], "Listini pronti per invio 2026-08-12 14-35.zip")

    def test_voce_costruisce_gli_url_con_quote(self) -> None:
        cartella = self.compilazione("2026-08-12_1435")
        listino = [riga for riga in consegna.voce(cartella)["file"] if riga["tipo"] == "listino"][0]
        self.assertEqual(
            listino["url"],
            "/ordini/2026-08-12_1435/Ordine%20LARICE%20%E2%80%94%2012%20agosto%202026.xlsx",
        )
        self.assertEqual(sorted(listino), ["nome", "tipo", "url"])

    def test_voce_usa_la_funzione_per_il_nome_del_fornitore(self) -> None:
        cartella = self.compilazione("2026-08-12_1435")
        letta = consegna.voce(cartella, etichetta_fornitore=lambda identificativo: f"Ditta {identificativo.title()}")
        self.assertEqual(letta["fornitori"][0]["nome"], "Ditta Larice")

    def test_voce_senza_listini_non_offre_lo_zip(self) -> None:
        # Writer non configurato: solo il piano.  Senza listini non c'e' niente
        # da consegnare e il pulsante non deve comparire.
        cartella = self.radice / "2026-08-12_1435"
        cartella.mkdir()
        (cartella / consegna.NOME_PIANO).write_bytes(b"{}")
        consegna.scrivi_audit(
            cartella,
            audit_finto(
                "2026-08-12_1435",
                creato_il="2026-08-12T14:35:00+02:00",
                file=({"nome": consegna.NOME_PIANO, "tipo": "piano", "byte": 2},),
            ),
        )
        letta = consegna.voce(cartella)
        self.assertEqual(letta["listini"], 0)
        self.assertIsNone(letta["zipUrl"])

    def test_voce_regge_un_audit_con_i_campi_del_tipo_sbagliato(self) -> None:
        cartella = self.radice / "2026-08-12_1435"
        cartella.mkdir()
        (cartella / NOME_LISTINO_LARICE).write_bytes(b"finto xlsx")
        consegna.scrivi_audit(cartella, {
            "creato_il": 12,
            "stato": None,
            "fornitori": ["larice", 7, ""],
            "totali_netti": "no",
            "totale_netto": "tanti",
            "righe": True,
            "file": ["non un oggetto", {"senza": "nome"}, {"nome": NOME_LISTINO_LARICE},
                     {"nome": "Ordine mai scritto.xlsx", "tipo": "listino"}],
        })
        letta = consegna.voce(cartella)
        self.assertTrue(letta["completa"])
        self.assertIsNone(letta["creatoIl"])
        self.assertEqual(letta["stato"], "SCONOSCIUTO")
        self.assertEqual(letta["fornitori"], [{"id": "larice", "nome": "LARICE", "totaleNetto": None}])
        self.assertIsNone(letta["totaleNetto"])
        self.assertIsNone(letta["righe"])  # True e' un bool, non un conteggio
        # La riga senza `tipo` prende quello dedotto dal nome; quella che nomina
        # un documento mai scritto non diventa un collegamento morto.
        self.assertEqual([riga["nome"] for riga in letta["file"]], [NOME_LISTINO_LARICE])
        self.assertEqual(letta["file"][0]["tipo"], "listino")
        self.assertEqual(letta["mancanti"], ["Ordine mai scritto.xlsx"])
        self.assertEqual(letta["listini"], 1)


class DaReperireTests(CasoConCartella):
    """L'elenco dei prodotti che nessun fornitore puo' dare (punto 5).

    Due modi di sbagliare, tutti e due silenziosi:

    1. **Scambiarlo per un listino.**  E' un `.xlsx` come le copie compilate, e
       il ripiego sull'estensione lo classificherebbe `listino`: finirebbe
       nello zip diretto al fornitore, cioe' gli si manderebbe il foglio di
       quello che *non* ci ha venduto.
    2. **Non contarlo affatto.**  Una compilazione in cui non si e' potuto
       ordinare niente da nessuno produce **solo** questo file: se il conteggio
       che decide `zipUrl` guarda i soli listini, l'unico documento prodotto
       resta li' senza nessun modo di scaricarlo, e la specifica prevede
       esattamente quel caso.

    La cartella si rilegge anche **senza audit** — e' una lezione gia' pagata su
    questo progetto — quindi il tipo si deve dedurre dal solo nome.
    """

    def test_tipo_file_riconosce_l_elenco_dal_solo_nome(self) -> None:
        self.assertEqual(consegna.tipo_file(NOME_DA_REPERIRE), "da_reperire")
        self.assertEqual(consegna.tipo_file(NOME_DA_REPERIRE), consegna.TIPO_DA_REPERIRE)
        # Il ripiego sull'estensione resta quello di prima per tutto il resto.
        self.assertEqual(consegna.tipo_file(NOME_LISTINO_LARICE), "listino")
        self.assertEqual(consegna.tipo_file(consegna.NOME_PIANO), "piano")
        self.assertEqual(consegna.tipo_file(NOME_DI_UN_ALTRO_TEMPO), "altro")

    def test_tipo_file_non_scambia_per_elenco_un_documento_qualsiasi(self) -> None:
        # Il prefisso da solo non basta: quello che conta e' il documento nostro.
        for nome in (
            "Prodotti da reperire.txt",
            "Prodotti da reperire — 12 agosto 2026.pdf",
            "Elenco prodotti da reperire — 12 agosto 2026.xlsx",
        ):
            self.assertNotEqual(consegna.tipo_file(nome), "da_reperire", nome)

    def test_e_documento_lo_accetta(self) -> None:
        # Non comincia per `.`, non e' un `~$` di Excel, non e' un `.tmp`.
        self.assertTrue(consegna.e_documento(NOME_DA_REPERIRE))
        # E le tre famiglie che restano fuori restano fuori anche per lui: il
        # file di proprieta' che Excel deposita aprendolo non deve entrare nella
        # consegna, e il temporaneo della scrittura atomica nemmeno.
        self.assertFalse(consegna.e_documento(f"~${NOME_DA_REPERIRE}"))
        self.assertFalse(consegna.e_documento(f"{NOME_DA_REPERIRE}.tmp"))

    def test_una_compilazione_di_soli_prodotti_da_reperire_si_scarica(self) -> None:
        """Il caso che la specifica prevede: non si e' potuto ordinare niente.

        Nella cartella c'e' il piano e c'e' l'elenco, e basta.  Con il conteggio
        dei soli listini `zipUrl` sarebbe `None` e quel foglio non si potrebbe
        portare via in nessun modo.
        """

        cartella = self.radice / "2026-08-12_1435"
        cartella.mkdir()
        (cartella / NOME_DA_REPERIRE).write_bytes(b"finto xlsx da reperire")
        (cartella / consegna.NOME_PIANO).write_bytes(b"{}")
        consegna.scrivi_audit(cartella, audit_finto(
            "2026-08-12_1435",
            creato_il="2026-08-12T14:35:00+02:00",
            fornitori=(),
            file=(
                {"nome": NOME_DA_REPERIRE, "tipo": "da_reperire", "byte": 22},
                {"nome": consegna.NOME_PIANO, "tipo": "piano", "byte": 2},
            ),
        ))

        letta = consegna.voce(cartella)

        tipi = {riga["nome"]: riga["tipo"] for riga in letta["file"]}
        self.assertEqual(tipi[NOME_DA_REPERIRE], "da_reperire")
        # ⚠ `listini` resta zero, ed è giusto: la pagina scrive «N listini» con
        # quel numero, e questo elenco un listino non è. A decidere il pulsante
        # «Scarica» è invece che ci sia qualcosa da consegnare, e qui c'è.
        self.assertEqual(letta["listini"], 0)
        self.assertEqual(letta["zipUrl"], "/ordini/2026-08-12_1435/zip")

    def test_si_conta_e_si_consegna_anche_senza_audit(self) -> None:
        """La cartella si rilegge dal disco: senza audit il tipo viene dal nome."""

        cartella = self.radice / "2026-08-12_1435"
        cartella.mkdir()
        (cartella / NOME_DA_REPERIRE).write_bytes(b"finto xlsx da reperire")
        (cartella / consegna.NOME_PIANO).write_bytes(b"{}")

        letta = consegna.voce(cartella)

        self.assertFalse(letta["completa"])  # l'audit non c'e' davvero
        tipi = {riga["nome"]: riga["tipo"] for riga in letta["file"]}
        self.assertEqual(tipi[NOME_DA_REPERIRE], "da_reperire")
        self.assertEqual(letta["listini"], 0)
        self.assertEqual(letta["zipUrl"], "/ordini/2026-08-12_1435/zip")

    def test_insieme_ai_listini_si_contano_tutti_e_due(self) -> None:
        cartella = self.compilazione("2026-08-12_1435")
        (cartella / NOME_DA_REPERIRE).write_bytes(b"finto xlsx da reperire")
        (cartella / consegna.NOME_AUDIT).unlink()

        letta = consegna.voce(cartella)

        # Il listino e' uno solo: l'altro documento e' l'elenco di quello che
        # nessuno ha, e nello zip ci va, ma fra i listini no.
        self.assertEqual(letta["listini"], 1)
        self.assertIsNotNone(letta["zipUrl"])
        tipi = {riga["nome"]: riga["tipo"] for riga in letta["file"]}
        self.assertEqual(tipi[NOME_LISTINO_LARICE], "listino")
        self.assertEqual(tipi[NOME_DA_REPERIRE], "da_reperire")
        # I due tipi restano distinti: la pagina deve poter dire quale delle due
        # cose e' ciascun documento, e il conteggio non li fonde.
        self.assertEqual(sorted(consegna.TIPI_DA_CONSEGNARE), ["da_reperire", "listino"])

    def test_l_elenco_entra_nello_zip_con_il_suo_nome(self) -> None:
        """`zip_in_memoria` non cambia: e' il chiamante che sceglie i nomi.

        Qui si verifica invece di darlo per buono — l'em dash del nome nuovo
        deve reggere nell'archivio come regge quello dei listini.
        """

        cartella = self.compilazione("2026-08-12_1435")
        (cartella / NOME_DA_REPERIRE).write_bytes(b"finto xlsx da reperire")
        consegna.scrivi_audit(cartella, audit_finto(
            "2026-08-12_1435",
            creato_il="2026-08-12T14:35:00+02:00",
            file=(
                {"nome": NOME_LISTINO_LARICE, "tipo": "listino", "fornitore": "larice", "byte": 10},
                {"nome": NOME_DA_REPERIRE, "tipo": "da_reperire", "byte": 22},
                {"nome": consegna.NOME_PIANO, "tipo": "piano", "byte": 2},
            ),
        ))

        letta = consegna.voce(cartella)
        nomi = [riga["nome"] for riga in letta["file"] if riga["tipo"] in consegna.TIPI_DA_CONSEGNARE]
        self.assertIn(NOME_DA_REPERIRE, nomi)

        with zipfile.ZipFile(io.BytesIO(consegna.zip_in_memoria(cartella, nomi))) as archivio:
            self.assertIn(NOME_DA_REPERIRE, archivio.namelist())
            self.assertEqual(archivio.read(NOME_DA_REPERIRE), b"finto xlsx da reperire")
            # 0x800: senza, Esplora risorse leggerebbe il nome con la tabella
            # locale e l'em dash diventerebbe due scarabocchi.
            self.assertTrue(archivio.getinfo(NOME_DA_REPERIRE).flag_bits & 0x800)

    def test_il_nome_si_scarica_senza_uccidere_l_intestazione(self) -> None:
        consegna.intestazione_allegato(NOME_DA_REPERIRE).encode("latin-1")


# ---------------------------------------------------------------------------
# Le difese sul percorso
# ---------------------------------------------------------------------------

class PercorsoSicuroTests(CasoConCartella):
    def setUp(self) -> None:
        super().setUp()
        self.cartella = self.compilazione("2026-08-12_1435")
        (self.cartella / "sotto").mkdir()
        (self.radice / "un-file.txt").write_bytes(b"x")
        self.segreto = self.base / "secrets.json"
        self.segreto.write_bytes(CONTENUTO_SEGRETO)

    def test_il_bersaglio_fuori_esiste_davvero(self) -> None:
        # Senza questa verifica le prove qui sotto passerebbero anche con la
        # difesa spenta: non ci sarebbe niente da raggiungere.
        self.assertEqual((self.radice / ".." / "secrets.json").read_bytes(), CONTENUTO_SEGRETO)
        self.assertEqual(
            (self.cartella / ".." / ".." / "secrets.json").read_bytes(),
            CONTENUTO_SEGRETO,
        )

    def test_cartella_sicura_accetta_la_cartella_vera(self) -> None:
        trovata = consegna.cartella_sicura(self.radice, "2026-08-12_1435")
        self.assertIsNotNone(trovata)
        self.assertEqual(trovata.resolve(), self.cartella.resolve())

    def test_file_sicuro_accetta_il_file_vero(self) -> None:
        trovato = consegna.file_sicuro(self.cartella, NOME_LISTINO_LARICE)
        self.assertIsNotNone(trovato)
        self.assertEqual(trovato.read_bytes(), b"finto xlsx")

    def test_rifiuti_comuni(self) -> None:
        # Chi chiama arriva da unquote(): `..%2f..%2fsecrets.json` a questo punto
        # e' gia' `../../secrets.json`.
        cattivi = [
            "..", "../..", "../../secrets.json", "..\\..\\secrets.json", ".",
            "", "foo/bar", "foo\\bar", "/etc/passwd", "C:\\Windows\\win.ini",
            "mai-vista", "cartel\x00la", None, 12,
        ]
        for nome in cattivi:
            self.assertIsNone(consegna.cartella_sicura(self.radice, nome), repr(nome))
            self.assertIsNone(consegna.file_sicuro(self.cartella, nome), repr(nome))

    def test_i_controlli_sul_nome_reggono_anche_se_la_scansione_mente(self) -> None:
        # Onesta': con un `os.scandir` che dice la verita' il primo passo non
        # serve a niente -- la lista bianca (passo 2) rifiuta gia' tutto quello
        # che il primo passo rifiuterebbe, e infatti spegnendo solo il primo
        # passo nessuna prova diventa rossa.  Qui la scansione viene fatta
        # mentire di proposito, cosi' il primo passo si prova da solo: e' la
        # rete che regge se un domani il passo 2 venisse allentato.
        class TrovataFinta:
            def __init__(self, nome: str) -> None:
                self.name = nome

            def is_dir(self, follow_symlinks: bool = True) -> bool:
                return True

            def is_file(self, follow_symlinks: bool = True) -> bool:
                return True

        class ScansioneBugiarda:
            def __init__(self, nomi):
                self.nomi = nomi

            def __enter__(self):
                return iter(TrovataFinta(nome) for nome in self.nomi)

            def __exit__(self, *args):
                return False

        # `sotto/../compilazione.json` si risolve *dentro* la cartella: né il
        # passo 2 (qui bugiardo) né il passo 3 lo fermerebbero.
        bugiardi = ["..", ".", "sotto/../compilazione.json", "sotto\\..\\compilazione.json", ""]
        with mock.patch.object(consegna.os, "scandir", lambda _: ScansioneBugiarda(bugiardi)):
            for nome in bugiardi:
                self.assertIsNone(consegna.file_sicuro(self.cartella, nome), repr(nome))
                self.assertIsNone(consegna.cartella_sicura(self.radice, nome), repr(nome))

    def test_una_cartella_non_e_un_file_e_viceversa(self) -> None:
        self.assertIsNone(consegna.file_sicuro(self.cartella, "sotto"))
        self.assertIsNone(consegna.cartella_sicura(self.radice, "un-file.txt"))
        self.assertIsNotNone(consegna.cartella_sicura(self.cartella, "sotto"))
        self.assertIsNotNone(consegna.file_sicuro(self.radice, "un-file.txt"))

    def test_file_sicuro_rifiuta_il_flusso_alternativo_ntfs(self) -> None:
        # Misurato: `compilazione.json::$DATA` restituisce il contenuto del file
        # e nessuna lista nera di caratteri lo prevede.  Lo ferma la lista
        # bianca: un flusso alternativo non compare mai in una scansione.
        vero = self.cartella / consegna.NOME_AUDIT
        self.assertTrue(vero.is_file())
        try:
            with open(f"{vero}::$DATA", "rb") as flusso:
                leggibile = flusso.read()
        except OSError:  # pragma: no cover - dipende dal file system
            leggibile = None
        if leggibile is not None:
            self.assertEqual(leggibile, vero.read_bytes())
            # Il contenimento da solo non basterebbe: il percorso col flusso si
            # risolve *dentro* la cartella.
            percorso = self.cartella / f"{consegna.NOME_AUDIT}::$DATA"
            self.assertTrue(consegna._dentro(percorso.resolve(), self.cartella.resolve()))

        for flusso in (f"{consegna.NOME_AUDIT}:$DATA", f"{consegna.NOME_AUDIT}::$DATA",
                       f"{NOME_LISTINO_LARICE}::$DATA"):
            self.assertIsNone(consegna.file_sicuro(self.cartella, flusso), flusso)
        self.assertIsNone(consegna.cartella_sicura(self.radice, "2026-08-12_1435::$DATA"))

    @unittest.skipUnless(os.name == "nt", "la giunzione NTFS esiste solo su Windows")
    def test_una_giunzione_che_punta_fuori_viene_rifiutata(self) -> None:
        # E' il ramo del contenimento (resolve + relative_to): la giunzione
        # compare nella scansione ed e' una cartella vera, quindi la lista
        # bianca la lascia passare.  A fermarla e' solo il terzo passo.
        # La giunzione non chiede privilegi, il collegamento simbolico si'.
        fuori = self.base / "fuori"
        fuori.mkdir()
        (fuori / "segreto.txt").write_bytes(CONTENUTO_SEGRETO)
        giunzione = self.radice / "scorciatoia"
        esito = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(giunzione), str(fuori)],
            capture_output=True, text=True,
        )
        if esito.returncode != 0:  # pragma: no cover - dipende dal file system
            self.skipTest(f"mklink /J non disponibile: {esito.stdout} {esito.stderr}")
        # Va tolta prima della pulizia della cartella temporanea.
        self.addCleanup(lambda: os.rmdir(giunzione) if giunzione.exists() else None)

        trovata = [voce for voce in os.scandir(self.radice) if voce.name == "scorciatoia"][0]
        self.assertTrue(trovata.is_dir())  # la lista bianca da sola direbbe di si'
        self.assertEqual((giunzione / "segreto.txt").read_bytes(), CONTENUTO_SEGRETO)

        self.assertIsNone(consegna.cartella_sicura(self.radice, "scorciatoia"))

    def test_dentro(self) -> None:
        self.assertTrue(consegna._dentro(self.cartella.resolve(), self.radice.resolve()))
        self.assertFalse(consegna._dentro(self.segreto.resolve(), self.radice.resolve()))


# ---------------------------------------------------------------------------
# Lo zip
# ---------------------------------------------------------------------------

class ZipTests(CasoConCartella):
    def setUp(self) -> None:
        super().setUp()
        self.cartella = self.compilazione("2026-08-12_1435")
        self.secondo = f"Ordine BETULLA {EM_DASH} 12 agosto 2026.xlsx"
        (self.cartella / self.secondo).write_bytes(b"finto xlsx betulla")
        self.segreto = self.base / "secrets.json"
        self.segreto.write_bytes(CONTENUTO_SEGRETO)

    def test_zip_in_memoria_rilegge_i_nomi_giusti_con_la_bandiera_utf8(self) -> None:
        crudo = consegna.zip_in_memoria(self.cartella, [NOME_LISTINO_LARICE, self.secondo])
        with zipfile.ZipFile(io.BytesIO(crudo)) as archivio:
            self.assertEqual(archivio.namelist(), [NOME_LISTINO_LARICE, self.secondo])
            informazioni = archivio.getinfo(NOME_LISTINO_LARICE)
            # 0x800: senza questa bandiera Esplora risorse leggerebbe il nome
            # con la tabella locale e l'em dash diventerebbe due scarabocchi.
            self.assertTrue(informazioni.flag_bits & 0x800)
            self.assertEqual(archivio.read(NOME_LISTINO_LARICE), b"finto xlsx")
            self.assertEqual(archivio.read(self.secondo), b"finto xlsx betulla")

    def test_zip_in_memoria_mette_solo_quello_che_gli_si_chiede(self) -> None:
        crudo = consegna.zip_in_memoria(self.cartella, [NOME_LISTINO_LARICE])
        with zipfile.ZipFile(io.BytesIO(crudo)) as archivio:
            self.assertEqual(archivio.namelist(), [NOME_LISTINO_LARICE])

    def test_zip_in_memoria_non_scrive_su_disco(self) -> None:
        prima = sorted(p.name for p in self.cartella.iterdir())
        consegna.zip_in_memoria(self.cartella, [NOME_LISTINO_LARICE])
        self.assertEqual(sorted(p.name for p in self.cartella.iterdir()), prima)

    def test_zip_in_memoria_rifiuta_un_nome_che_esce_dalla_cartella(self) -> None:
        self.assertEqual(self.segreto.read_bytes(), CONTENUTO_SEGRETO)  # il bersaglio esiste
        for nome in ("../../secrets.json", "..\\..\\secrets.json", "sotto", "mai-visto.xlsx",
                     f"{consegna.NOME_AUDIT}::$DATA"):
            with self.assertRaises(ValueError, msg=nome):
                consegna.zip_in_memoria(self.cartella, [NOME_LISTINO_LARICE, nome])

    def test_zip_in_memoria_senza_nomi(self) -> None:
        for vuoto in ([], (), None):
            with self.assertRaises(ValueError):
                consegna.zip_in_memoria(self.cartella, vuoto)


# ---------------------------------------------------------------------------
# L'intestazione di scaricamento
# ---------------------------------------------------------------------------

class IntestazioneAllegatoTests(unittest.TestCase):
    def test_si_codifica_in_latin_1(self) -> None:
        # E' la prova che la rotta non muore: BaseHTTPRequestHandler scrive le
        # intestazioni in latin-1, e `attachment; filename="...\u2014..."` alza
        # UnicodeEncodeError a corpo gia' promesso.
        valore = consegna.intestazione_allegato(NOME_LISTINO_LARICE)
        valore.encode("latin-1")  # non deve sollevare
        with self.assertRaises(UnicodeEncodeError):
            f'attachment; filename="{NOME_LISTINO_LARICE}"'.encode("latin-1")

    def test_contiene_il_ripiego_ascii_e_la_forma_rfc_5987(self) -> None:
        valore = consegna.intestazione_allegato(NOME_LISTINO_LARICE)
        self.assertEqual(
            valore,
            'attachment; filename="Ordine LARICE - 12 agosto 2026.xlsx"; '
            "filename*=UTF-8''Ordine%20LARICE%20%E2%80%94%2012%20agosto%202026.xlsx",
        )
        self.assertIn("filename*=UTF-8''", valore)
        self.assertNotIn(EM_DASH, valore)

    def test_ogni_nome_di_zip_e_di_listino_si_codifica_in_latin_1(self) -> None:
        for nome in (
            consegna.nome_zip("2026-08-12_1435"),
            consegna.nome_listino("larice", MOMENTO),
            consegna.nome_listino("caffè", MOMENTO),
            NOME_DI_UN_ALTRO_TEMPO,
            consegna.NOME_PIANO,
        ):
            consegna.intestazione_allegato(nome).encode("latin-1")

    def test_toglie_le_virgolette_e_la_barra_rovescia(self) -> None:
        valore = consegna.intestazione_allegato('a"b\\c.xlsx')
        self.assertEqual(valore.count('"'), 2)  # solo quelle del ripiego
        self.assertIn('filename="abc.xlsx"', valore)

    def test_non_lascia_passare_un_a_capo(self) -> None:
        # Un `\r\n` nel nome sarebbe un'iniezione di intestazioni.
        valore = consegna.intestazione_allegato("buono\r\nX-Cattivo: si.xlsx")
        self.assertNotIn("\r", valore)
        self.assertNotIn("\n", valore)
        self.assertIn('filename="buonoX-Cattivo: si.xlsx"', valore)
        valore.encode("latin-1")

    def test_nome_vuoto_o_tutto_non_ascii(self) -> None:
        self.assertIn('filename="allegato"', consegna.intestazione_allegato(""))
        self.assertIn('filename="---"', consegna.intestazione_allegato("\u2014\u2014\u2014"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
