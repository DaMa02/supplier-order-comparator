"""Delivery folder: dated folders, listing, path safety, zip, download header.

Covers the invariants a plausible-but-wrong implementation would silently break:

1. Folder creation is atomic. `if not exists(): mkdir()` is a race: two runs in
   the same minute could land in the same folder and overwrite each other.
   The race is exercised both for real (two calls with the same `momento`)
   and simulated (`mkdir` loses the race and raises `FileExistsError`).
2. Path safety is allowlist-based. The target file outside the folder is
   created for real and read first, so the test would fail if the defense
   were disabled; same for the NTFS alternate data stream
   `compilazione.json::$DATA`, which on this machine returns the file content
   (measured) and that no denylist of characters would catch.
3. The download header is encoded in latin-1. `BaseHTTPRequestHandler` writes
   headers in latin-1, so a raw em dash breaks the response after the body is
   already promised. The test asserts `.encode("latin-1")` directly.

No network, no `app/data/`: everything runs in `tempfile.TemporaryDirectory()`.
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

# Name of the "items to source" file. Written here by hand rather
# than imported from `da_reperire`: `consegna` must recognize it from the name
# alone when a folder is re-read without an audit, and a test that took the
# name from the producing module would still pass if both changed together.
NOME_DA_REPERIRE = f"Prodotti da reperire {EM_DASH} 12 agosto 2026.xlsx"

# A file the app doesn't produce (a per-supplier cart export), as found in
# order folders written by earlier versions. Exercises the generic fallback
# in `tipo_file`.
NOME_DI_UN_ALTRO_TEMPO = "PIANO_CARRELLO_NOCE.json"


def audit_finto(
    nome_cartella: str,
    *,
    creato_il: str,
    fornitori: tuple[str, ...] = ("larice",),
    file: tuple[dict, ...] = (),
) -> dict:
    """Build a plausible `compilazione.json`, shaped like the writer's audit."""
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
    """Shared fixture: an order root inside a temporary directory."""

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
# Names
# ---------------------------------------------------------------------------

class NomiTests(unittest.TestCase):
    def test_nome_cartella_non_contiene_mai_i_due_punti(self) -> None:
        # Windows rejects `:` in a name: the time is rendered as `1435`.
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
        # Windows strips trailing dots/spaces silently: `larice.` and `larice`
        # would collide into the same file without anyone noticing.
        self.assertEqual(
            consegna.nome_listino("larice. ", MOMENTO),
            f"Ordine LARICE__ {EM_DASH} 12 agosto 2026.xlsx",
        )

    def test_data_leggibile_non_dipende_dalla_localizzazione(self) -> None:
        try:
            precedente = locale.setlocale(locale.LC_TIME)
            locale.setlocale(locale.LC_TIME, "C")
        except (locale.Error, ValueError):  # pragma: no cover - depends on the system
            self.skipTest("la localizzazione 'C' non è disponibile su questo sistema")
        self.addCleanup(locale.setlocale, locale.LC_TIME, precedente)
        # With LC_TIME=C, strftime("%B") would say "August": proof that month
        # names come from `MESI`, not from the system locale.
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
        # A manually created folder must not crash the listing.
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
# Folder creation
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
        # No overwrite: what's in the first folder stays there.
        (prima / "listino.xlsx").write_bytes(b"prima")
        terza = consegna.crea_cartella(self.radice, MOMENTO)
        self.assertEqual(terza.name, "2026-08-12_1435_3")
        self.assertEqual((prima / "listino.xlsx").read_bytes(), b"prima")

    def test_crea_cartella_si_fida_di_file_exists_error_e_non_di_exists(self) -> None:
        # The real race: two processes request the same minute and our
        # `mkdir` arrives second. `exist_ok=True` would never raise, so both
        # runs would write into the same folder; `if not exists(): mkdir()`
        # would still see the folder as absent at check time and land there too.
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
        """`ordini` is the user's own archive folder, not exclusively ours.

        Any folder they create inside it — "da mandare a Larice", or a stray
        "Nuova cartella" — becomes the most recent by mtime. If `ultima()`
        picked it, a user who just ran a comparison would be told none exists.
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
# Audit
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
        # Proves the write is atomic: without a temp file + os.replace, an
        # interrupted write would leave a truncated audit in place — a
        # document that lies about the run.
        cartella = self.radice / "2026-08-12_1435"
        cartella.mkdir()
        buono = audit_finto("2026-08-12_1435", creato_il="2026-08-12T14:35:00+02:00")
        consegna.scrivi_audit(cartella, buono)

        write_bytes_reale = Path.write_bytes

        def write_bytes_che_muore(self, dati):
            write_bytes_reale(self, dati[:5])  # write dies halfway
            raise OSError(28, "disco pieno")

        with mock.patch.object(Path, "write_bytes", write_bytes_che_muore):
            with self.assertRaises(OSError):
                consegna.scrivi_audit(cartella, {"schema_audit": 1, "nuovo": True})

        self.assertEqual(consegna.leggi_audit(cartella), buono)

    def test_leggi_audit_non_solleva_mai(self) -> None:
        cartella = self.radice / "2026-08-12_1435"
        cartella.mkdir()
        self.assertIsNone(consegna.leggi_audit(cartella))  # no audit file
        self.assertIsNone(consegna.leggi_audit(self.base / "mai-vista"))  # folder doesn't exist

        percorso = cartella / consegna.NOME_AUDIT
        for crudo in (b"{", b"[]", b'"testo"', b"", b"\xff\xfe{\x00", b"null"):
            percorso.write_bytes(crudo)
            self.assertIsNone(consegna.leggi_audit(cartella), crudo)

        percorso.write_bytes(b'{"schema_audit": 1}')
        self.assertEqual(consegna.leggi_audit(cartella), {"schema_audit": 1})


# ---------------------------------------------------------------------------
# Listing
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
        # Names and audits can disagree; the audit's `creato_il` wins.
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
        """An unrecognized file from an older folder is "altro" and keeps it listed.

        `tipo_file` must fall back to the generic classification ("altro")
        without counting it as a price list, without crashing the entry, and
        without dropping a folder that exists on disk from the listing.

        The audit is removed on purpose: with an audit, types come from it,
        which would test something else. Without one, the listing must infer
        each type from the name alone, i.e. actually go through `tipo_file`.
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
        """An audit can declare a `tipo` the app doesn't recognize.

        The audit's type passes through unchanged: the entry reports that run
        as it happened, not as reinterpreted today. What matters is that the
        folder stays listed, the price-list count is unaffected, and the file
        remains downloadable. The frontend prints no label for an unknown type.
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
        """Only list what the routes can actually serve.

        A junction inside `ordini` — an `mklink /J` to a network folder, a
        badly restored backup — would otherwise show up as a run, listing
        file names from outside with links that 404.
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
        # Without this line the test would pass even if the junction never
        # existed; it proves there's something for the defense to stop.
        self.assertEqual((giunzione / "segreto.txt").read_bytes(), CONTENUTO_SEGRETO)

        voci = consegna.elenco(self.radice)

        self.assertEqual([voce["cartella"] for voce in voci], ["2026-08-12_1435"])
        self.assertNotIn("segreto.txt", json.dumps(voci, ensure_ascii=False))

    def test_elenco_tiene_la_cartella_con_l_audit_troncato(self) -> None:
        buona = self.compilazione("2026-08-12_1435", creato_il="2026-08-12T14:35:00+02:00")
        rotta = self.compilazione("2026-08-11_1000", creato_il="2026-08-11T10:00:00+02:00")
        (rotta / consegna.NOME_AUDIT).write_bytes(b"{")
        # Without a readable audit the sort key is the folder's mtime, which
        # would otherwise be "now" and put it first.
        vecchio = datetime(2026, 8, 11, 10, 0, tzinfo=timezone(timedelta(hours=2))).timestamp()
        os.utime(rotta, (vecchio, vecchio))

        voci = consegna.elenco(self.radice)  # must not raise
        self.assertEqual([v["cartella"] for v in voci], [buona.name, rotta.name])
        incompleta = voci[1]
        self.assertFalse(incompleta["completa"])
        self.assertEqual(incompleta["stato"], "SCONOSCIUTO")
        self.assertEqual(incompleta["fornitori"], [])
        self.assertIsNone(incompleta["totaleNetto"])
        self.assertIsNone(incompleta["righe"])
        self.assertEqual(incompleta["etichetta"], "11 agosto 2026, 10:00")
        # The type is inferred from the extension and the zip stays possible:
        # the price lists are still there even if the audit describing them
        # can't be parsed.
        nomi = {riga["nome"]: riga["tipo"] for riga in incompleta["file"]}
        self.assertEqual(nomi[NOME_LISTINO_LARICE], "listino")
        self.assertEqual(nomi[consegna.NOME_PIANO], "piano")
        # The audit describes the folder; it isn't something to deliver and
        # never shows up among the documents, even when it's the broken one.
        self.assertNotIn(consegna.NOME_AUDIT, nomi)
        self.assertEqual(incompleta["listini"], 1)
        self.assertEqual(incompleta["zipUrl"], "/ordini/2026-08-11_1000/zip")


class DerivaDelDiscoTests(CasoConCartella):
    """The audit describes the folder at the instant it's written.

    From then on the user lives in that folder: attaches a price list to an
    email, moves it to the desktop, reopens another one in Excel to double
    check it. An entry built purely from the audit would keep counting files
    that no longer exist and offer links that 404 — and the zip, which
    requires every declared name to be present, would refuse to deliver even
    the remaining price lists, telling the user there's nothing to send when
    one file is still waiting to go out.
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
        """Baseline: without this, the tests below would pass even with the defense disabled."""

        letta = consegna.voce(self.cartella)
        self.assertEqual(letta["listini"], 2)
        self.assertEqual(letta["mancanti"], [])

    def test_un_listino_spostato_non_si_conta_piu_e_viene_dichiarato(self) -> None:
        (self.cartella / self.secondo).rename(self.base / self.secondo)

        letta = consegna.voce(self.cartella)

        self.assertEqual(letta["listini"], 1)
        self.assertNotIn(self.secondo, [riga["nome"] for riga in letta["file"]])
        self.assertEqual(letta["mancanti"], [self.secondo])
        # The remaining price list is still deliverable: it's the one the user
        # still needs to send, and reporting nothing would send them back to
        # rerun the comparison for no reason.
        self.assertEqual(letta["zipUrl"], "/ordini/2026-08-12_1435/zip")
        dentro = zipfile.ZipFile(io.BytesIO(consegna.zip_in_memoria(
            self.cartella, [riga["nome"] for riga in letta["file"] if riga["tipo"] == "listino"]
        ))).namelist()
        self.assertEqual(dentro, [NOME_LISTINO_LARICE])

    def test_un_documento_comparso_dopo_l_audit_non_e_un_listino(self) -> None:
        """Unknown origin, unknown owner: it must not go out to a supplier."""

        (self.cartella / "Ordine di qualcun altro.xlsx").write_bytes(b"?")

        letta = consegna.voce(self.cartella)

        tipi = {riga["nome"]: riga["tipo"] for riga in letta["file"]}
        self.assertEqual(tipi["Ordine di qualcun altro.xlsx"], "altro")
        self.assertEqual(letta["listini"], 2)

    def test_il_file_di_proprieta_di_excel_non_entra_nella_consegna(self) -> None:
        """Excel drops this lock file just by opening a copy to check it.

        Without the filter it would end up in the zip sent to the supplier,
        and in a folder without an audit it would even be counted as a price
        list.
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
        # No price lists written, only the plan. With nothing to deliver, the
        # download button must not appear.
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
        self.assertIsNone(letta["righe"])  # `True` is a bool, not a count
        # The entry with no `tipo` gets one inferred from the name; the entry
        # naming a file that was never written doesn't become a dead link.
        self.assertEqual([riga["nome"] for riga in letta["file"]], [NOME_LISTINO_LARICE])
        self.assertEqual(letta["file"][0]["tipo"], "listino")
        self.assertEqual(letta["mancanti"], ["Ordine mai scritto.xlsx"])
        self.assertEqual(letta["listini"], 1)


class DaReperireTests(CasoConCartella):
    """The list of items no supplier can provide.

    Two silent ways this can go wrong:

    1. Mistaking it for a price list. It's a `.xlsx` like the compiled ones,
       and the extension-based fallback would classify it as `listino`,
       shipping it in the zip sent to the supplier — the sheet of what they
       *didn't* sell us.
    2. Not counting it at all. A run where nothing could be ordered from
       anyone produces only this file: if the count that decides `zipUrl`
       looks only at price lists, the one document produced stays with no way
       to download it, even though the spec covers exactly this case.

    The folder is also re-read without an audit, so the type must be inferred
    from the name alone.
    """

    def test_tipo_file_riconosce_l_elenco_dal_solo_nome(self) -> None:
        self.assertEqual(consegna.tipo_file(NOME_DA_REPERIRE), "da_reperire")
        self.assertEqual(consegna.tipo_file(NOME_DA_REPERIRE), consegna.TIPO_DA_REPERIRE)
        # The extension-based fallback is unchanged for everything else.
        self.assertEqual(consegna.tipo_file(NOME_LISTINO_LARICE), "listino")
        self.assertEqual(consegna.tipo_file(consegna.NOME_PIANO), "piano")
        self.assertEqual(consegna.tipo_file(NOME_DI_UN_ALTRO_TEMPO), "altro")

    def test_tipo_file_non_scambia_per_elenco_un_documento_qualsiasi(self) -> None:
        # The prefix alone isn't enough; it must match our exact file.
        for nome in (
            "Prodotti da reperire.txt",
            "Prodotti da reperire — 12 agosto 2026.pdf",
            "Elenco prodotti da reperire — 12 agosto 2026.xlsx",
        ):
            self.assertNotEqual(consegna.tipo_file(nome), "da_reperire", nome)

    def test_e_documento_lo_accetta(self) -> None:
        # Doesn't start with `.`, isn't an Excel `~$` lock file, isn't a `.tmp`.
        self.assertTrue(consegna.e_documento(NOME_DA_REPERIRE))
        # The same exclusions apply to it too: the lock file Excel drops when
        # opening a copy must not enter the delivery, nor must the temp file
        # from an atomic write.
        self.assertFalse(consegna.e_documento(f"~${NOME_DA_REPERIRE}"))
        self.assertFalse(consegna.e_documento(f"{NOME_DA_REPERIRE}.tmp"))

    def test_una_compilazione_di_soli_prodotti_da_reperire_si_scarica(self) -> None:
        """The spec's own case: nothing could be ordered at all.

        The folder holds only the plan and the "to be sourced" list. With a
        count limited to price lists, `zipUrl` would be `None` and that sheet
        could never be downloaded.
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
        # `listini` staying zero is correct: the page prints "N listini" with
        # that count, and this file isn't a price list. The "Scarica" button
        # is instead decided by whether there's anything to deliver, and here
        # there is.
        self.assertEqual(letta["listini"], 0)
        self.assertEqual(letta["zipUrl"], "/ordini/2026-08-12_1435/zip")

    def test_si_conta_e_si_consegna_anche_senza_audit(self) -> None:
        """The folder is re-read from disk: without an audit, the type comes from the name."""

        cartella = self.radice / "2026-08-12_1435"
        cartella.mkdir()
        (cartella / NOME_DA_REPERIRE).write_bytes(b"finto xlsx da reperire")
        (cartella / consegna.NOME_PIANO).write_bytes(b"{}")

        letta = consegna.voce(cartella)

        self.assertFalse(letta["completa"])  # there really is no audit
        tipi = {riga["nome"]: riga["tipo"] for riga in letta["file"]}
        self.assertEqual(tipi[NOME_DA_REPERIRE], "da_reperire")
        self.assertEqual(letta["listini"], 0)
        self.assertEqual(letta["zipUrl"], "/ordini/2026-08-12_1435/zip")

    def test_insieme_ai_listini_si_contano_tutti_e_due(self) -> None:
        cartella = self.compilazione("2026-08-12_1435")
        (cartella / NOME_DA_REPERIRE).write_bytes(b"finto xlsx da reperire")
        (cartella / consegna.NOME_AUDIT).unlink()

        letta = consegna.voce(cartella)

        # Only one price list: the other file is the "to be sourced" list,
        # which goes in the zip but doesn't count as a price list.
        self.assertEqual(letta["listini"], 1)
        self.assertIsNotNone(letta["zipUrl"])
        tipi = {riga["nome"]: riga["tipo"] for riga in letta["file"]}
        self.assertEqual(tipi[NOME_LISTINO_LARICE], "listino")
        self.assertEqual(tipi[NOME_DA_REPERIRE], "da_reperire")
        # The two types stay distinct: the page must be able to tell which is
        # which, and the count doesn't merge them.
        self.assertEqual(sorted(consegna.TIPI_DA_CONSEGNARE), ["da_reperire", "listino"])

    def test_l_elenco_entra_nello_zip_con_il_suo_nome(self) -> None:
        """`zip_in_memoria` is unchanged: the caller picks which names go in.

        Verified rather than assumed — the em dash in the new file name must
        survive in the archive the same way it does for price lists.
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
            # 0x800: without it, Windows Explorer would decode the name with
            # the local code page and turn the em dash into garbled characters.
            self.assertTrue(archivio.getinfo(NOME_DA_REPERIRE).flag_bits & 0x800)

    def test_il_nome_si_scarica_senza_uccidere_l_intestazione(self) -> None:
        consegna.intestazione_allegato(NOME_DA_REPERIRE).encode("latin-1")


# ---------------------------------------------------------------------------
# Path safety
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
        # Without this check, the tests below would pass even with the
        # defense disabled: there'd be nothing to reach.
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
        # Callers arrive already unquoted: `..%2f..%2fsecrets.json` is
        # `../../secrets.json` by the time it gets here.
        cattivi = [
            "..", "../..", "../../secrets.json", "..\\..\\secrets.json", ".",
            "", "foo/bar", "foo\\bar", "/etc/passwd", "C:\\Windows\\win.ini",
            "mai-vista", "cartel\x00la", None, 12,
        ]
        for nome in cattivi:
            self.assertIsNone(consegna.cartella_sicura(self.radice, nome), repr(nome))
            self.assertIsNone(consegna.file_sicuro(self.cartella, nome), repr(nome))

    def test_i_controlli_sul_nome_reggono_anche_se_la_scansione_mente(self) -> None:
        # With a truthful `os.scandir`, this first check is redundant: the
        # allowlist (step 2) already rejects everything it would reject, so
        # disabling only this step alone doesn't turn any test red. The scan
        # is made to lie on purpose here so this step is exercised in
        # isolation — it's the safety net if step 2 is ever loosened.
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

        # `sotto/../compilazione.json` resolves *inside* the folder: neither
        # step 2 (lying here) nor step 3 would stop it.
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
        # Measured: `compilazione.json::$DATA` returns the file's content, and
        # no character denylist accounts for it. The allowlist stops it: an
        # alternate stream never shows up in a directory scan.
        vero = self.cartella / consegna.NOME_AUDIT
        self.assertTrue(vero.is_file())
        try:
            with open(f"{vero}::$DATA", "rb") as flusso:
                leggibile = flusso.read()
        except OSError:  # pragma: no cover - dipende dal file system
            leggibile = None
        if leggibile is not None:
            self.assertEqual(leggibile, vero.read_bytes())
            # Containment alone wouldn't be enough: the path with the stream
            # still resolves *inside* the folder.
            percorso = self.cartella / f"{consegna.NOME_AUDIT}::$DATA"
            self.assertTrue(consegna._dentro(percorso.resolve(), self.cartella.resolve()))

        for flusso in (f"{consegna.NOME_AUDIT}:$DATA", f"{consegna.NOME_AUDIT}::$DATA",
                       f"{NOME_LISTINO_LARICE}::$DATA"):
            self.assertIsNone(consegna.file_sicuro(self.cartella, flusso), flusso)
        self.assertIsNone(consegna.cartella_sicura(self.radice, "2026-08-12_1435::$DATA"))

    @unittest.skipUnless(os.name == "nt", "la giunzione NTFS esiste solo su Windows")
    def test_una_giunzione_che_punta_fuori_viene_rifiutata(self) -> None:
        # Exercises the containment branch (resolve + relative_to): the
        # junction shows up in the scan as a real folder, so the allowlist
        # lets it through. Only the third step stops it. A junction needs no
        # special privilege to create, unlike a symlink.
        fuori = self.base / "fuori"
        fuori.mkdir()
        (fuori / "segreto.txt").write_bytes(CONTENUTO_SEGRETO)
        giunzione = self.radice / "scorciatoia"
        esito = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(giunzione), str(fuori)],
            capture_output=True, text=True,
        )
        if esito.returncode != 0:  # pragma: no cover - depends on the file system
            self.skipTest(f"mklink /J non disponibile: {esito.stdout} {esito.stderr}")
        # Must be removed before the temp folder cleanup runs.
        self.addCleanup(lambda: os.rmdir(giunzione) if giunzione.exists() else None)

        trovata = [voce for voce in os.scandir(self.radice) if voce.name == "scorciatoia"][0]
        self.assertTrue(trovata.is_dir())  # the allowlist alone would say yes
        self.assertEqual((giunzione / "segreto.txt").read_bytes(), CONTENUTO_SEGRETO)

        self.assertIsNone(consegna.cartella_sicura(self.radice, "scorciatoia"))

    def test_dentro(self) -> None:
        self.assertTrue(consegna._dentro(self.cartella.resolve(), self.radice.resolve()))
        self.assertFalse(consegna._dentro(self.segreto.resolve(), self.radice.resolve()))


# ---------------------------------------------------------------------------
# Zip archive
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
            # 0x800: without this flag, Windows Explorer would decode the name
            # with the local code page and turn the em dash into garbage.
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
        self.assertEqual(self.segreto.read_bytes(), CONTENUTO_SEGRETO)  # target exists
        for nome in ("../../secrets.json", "..\\..\\secrets.json", "sotto", "mai-visto.xlsx",
                     f"{consegna.NOME_AUDIT}::$DATA"):
            with self.assertRaises(ValueError, msg=nome):
                consegna.zip_in_memoria(self.cartella, [NOME_LISTINO_LARICE, nome])

    def test_zip_in_memoria_senza_nomi(self) -> None:
        for vuoto in ([], (), None):
            with self.assertRaises(ValueError):
                consegna.zip_in_memoria(self.cartella, vuoto)


# ---------------------------------------------------------------------------
# Download header
# ---------------------------------------------------------------------------

class IntestazioneAllegatoTests(unittest.TestCase):
    def test_si_codifica_in_latin_1(self) -> None:
        # Proves the route doesn't crash: `BaseHTTPRequestHandler` writes
        # headers in latin-1, and `attachment; filename="...\u2014..."` would
        # raise `UnicodeEncodeError` after the body is already promised.
        valore = consegna.intestazione_allegato(NOME_LISTINO_LARICE)
        valore.encode("latin-1")  # must not raise
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
        self.assertEqual(valore.count('"'), 2)  # only the ones from the ASCII fallback
        self.assertIn('filename="abc.xlsx"', valore)

    def test_non_lascia_passare_un_a_capo(self) -> None:
        # A `\r\n` in the name would be a header injection.
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
