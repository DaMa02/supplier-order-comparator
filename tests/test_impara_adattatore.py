"""Lo schema confermato dall'utente entra nel registro, e niente altro.

Questo script è l'unica cosa che impedisce che la settimana prossima un
fornitore già confermato torni sconosciuto: senza, uno schema approvato resta
scritto nel manifest della run e servirebbe qualcuno a copiarlo a mano dentro
`references/adapters.json`, che è esattamente ciò che il programma finito non
avrà.

Le due cose che questi collaudi difendono, e per cui esistono:

- **una voce che l'utente non ha confermato non entra per nessuna ragione**;
- **un rifiuto non è mai silenzioso**: esce con il suo motivo scritto in
  italiano e con uscita `2`, perché un fallimento zitto qui vorrebbe dire un
  fornitore che torna sconosciuto e nessuno che sappia perché.

Il registro vero non viene mai toccato: ogni prova lavora su una copia in una
cartella temporanea, e i listini veri si copiano prima di darli in pasto alla
catena.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from openpyxl import Workbook


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
APP = SKILL_ROOT / "app"
# ⚠ Copia congelata, non `references/adapters.json`: quel file lo riscrive il
# programma quando impara uno schema confermato, e queste prove partono da un
# registro di cui conoscono il contenuto. Vedi la stessa nota in
# `tests/test_registro_impronte.py`.
ADAPTERS = SKILL_ROOT / "tests" / "fixtures" / "adapters_nativi.json"
for cartella in (SCRIPTS, APP):
    if str(cartella) not in sys.path:
        sys.path.insert(0, str(cartella))

import inspect_sources  # noqa: E402
# `self.registro` e' il percorso della copia; il motore si chiama per esteso
# per non confondere le due cose in un collaudo che le usa insieme.
import registro as motore_registro  # noqa: E402

# I listini veri stanno fuori dal progetto: si spostano con questa variabile
# d'ambiente senza toccare il codice del collaudo.
LISTINI = Path(os.environ.get("LISTINI_STORICI", str(SKILL_ROOT / "listini-storici")))

# Gli script scrivono frasi in italiano con le virgolette basse: senza questo,
# su Windows l'uscita arriverebbe in cp1252 e le prove sui motivi leggerebbero
# caratteri sostitutivi invece delle parole.
AMBIENTE = {**os.environ, "PYTHONIOENCODING": "utf-8"}

INTESTAZIONI_FORNITORE = ["Disponibilita merce", "Prezzo Netto EUR", "Nome Articolo",
                          "Pezzi Scatola", "Codice Interno", "Barcode EAN", "Aliquota IVA",
                          "Qta ordine"]

MAPPATURA_FORNITORE: dict[str, Any] = {
    "sheet": "Offerte agosto",
    "header_row": 1,
    "data_start_row": 2,
    "columns": {
        "availability": "Disponibilita merce",
        "unit_price_net": "Prezzo Netto EUR",
        "description": "Nome Articolo",
        "pieces_per_carton": "Pezzi Scatola",
        "supplier_code": "Codice Interno",
        "ean": "Barcode EAN",
        "vat": "Aliquota IVA",
    },
    "available_values": ["SI"],
    "order_column": "H",
}


def mappatura(**modifiche: Any) -> dict[str, Any]:
    """Una copia della mappatura confermata, con le modifiche chieste."""

    copia = json.loads(json.dumps(MAPPATURA_FORNITORE))
    copia.update(modifiche)
    return copia


def decisione(**modifiche: Any) -> dict[str, Any]:
    """La decisione che l'AI propone e l'utente conferma."""

    proposta = {
        "state": "NUOVO_FORNITORE",
        "role": "supplier",
        "supplier_id": "fittizio",
        "adapter_id": None,
        "confidence": 0.8,
        "rationale": "Nessun adattatore compatibile: colonne dichiarate a mano.",
        "field_mapping": mappatura(),
    }
    proposta.update(modifiche)
    return proposta


def scrivi_foglio(percorso: Path, righe: list[list[Any]], nome: str = "Offerte agosto") -> Path:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = nome
    for riga in righe:
        sheet.append(riga)
    workbook.save(percorso)
    workbook.close()
    return percorso


def listino_fornitore(percorso: Path, *, intestazioni: list[Any] | None = None,
                      nome_foglio: str = "Offerte agosto", preambolo: int = 0) -> Path:
    """Un listino di un fornitore che nessun adattatore conosce."""

    righe: list[list[Any]] = [["Listino promozionale"] for _ in range(preambolo)]
    righe.append(list(intestazioni if intestazioni is not None else INTESTAZIONI_FORNITORE))
    for numero in range(3):
        righe.append(["SI", 1.25 + numero, f"Prodotto {numero}", 6, f"FIT-00{numero}",
                      f"800000000000{numero}", 22, None])
    return scrivi_foglio(percorso, righe, nome_foglio)


def voce_di_manifest(percorso: Path, proposta: dict[str, Any], stato_conferma: str) -> dict[str, Any]:
    """La voce del manifest come la scrive `apply_preflight_decisions`.

    Il profilo è quello vero dell'inspector: costruirlo a mano renderebbe il
    collaudo indipendente dal documento che la catena produce davvero, cioè
    inutile.
    """

    profilo = inspect_sources.profile_file(percorso)
    return {**profilo, "ai_preflight": proposta,
            "user_confirmation": {"required": True, "status": stato_conferma}}


class BaseImparaTests(unittest.TestCase):
    """Ogni prova ha la sua copia del registro: quello vero non si tocca."""

    maxDiff = None

    def setUp(self) -> None:
        self.radice = Path(tempfile.mkdtemp(prefix="collaudo_impara_"))
        self.addCleanup(shutil.rmtree, self.radice, True)
        self.registro = self.radice / "adapters.json"
        shutil.copyfile(ADAPTERS, self.registro)
        self.registro_di_partenza = self.registro.read_bytes()
        # ⚠ Dal 19 agosto 2026 quello che si impara non torna nel registro
        # spedito — che sta sotto git e l'avvio del negozio riporta indietro —
        # ma in un file suo, fuori da git. Il programma li legge fusi.
        self.imparato = motore_registro.percorso_imparato(self.registro)

    def manifest(self, voci: list[dict[str, Any]]) -> Path:
        percorso = self.radice / "input_manifest.json"
        percorso.write_bytes(json.dumps({"schema_version": 1, "files": voci},
                                        ensure_ascii=False, indent=2).encode("utf-8"))
        return percorso

    def manifest_di_un_file(self, percorso: Path, proposta: dict[str, Any] | None = None,
                            conferma: str = "CONFIRMED") -> Path:
        return self.manifest([voce_di_manifest(percorso, proposta or decisione(), conferma)])

    def impara(self, manifest: Path, *argomenti: object) -> tuple[int, dict[str, Any]]:
        esito = subprocess.run(
            [sys.executable, str(SCRIPTS / "impara_adattatore.py"),
             "--manifest", str(manifest), "--adapters", str(self.registro),
             *(str(valore) for valore in argomenti)],
            cwd=SKILL_ROOT, capture_output=True, env=AMBIENTE,
        )
        uscita = esito.stdout.decode("utf-8", errors="replace")
        try:
            return esito.returncode, json.loads(uscita)
        except ValueError as errore:  # pragma: no cover - serve solo a leggere il guasto
            raise AssertionError(
                f"impara_adattatore.py non ha stampato un rapporto JSON.\n"
                f"stdout:\n{uscita}\nstderr:\n{esito.stderr.decode('utf-8', errors='replace')}"
            ) from errore

    def documento(self) -> dict[str, Any]:
        """Il registro come lo vede il programma: spedito più imparato."""

        return {"adapters": motore_registro.adattatori(self.registro)}

    def voce(self, identificativo: str) -> dict[str, Any]:
        return next((voce for voce in self.documento()["adapters"]
                     if voce["id"] == identificativo), {})

    def assert_registro_intatto(self) -> None:
        """Niente è stato imparato: né il file spedito né quello imparato."""

        self.assertEqual(self.registro.read_bytes(), self.registro_di_partenza)
        self.assertFalse(self.imparato.exists())

    def assert_spedito_intatto(self) -> None:
        """Qualcosa è stato imparato, ma non nel file che l'avvio riporta indietro."""

        self.assertEqual(self.registro.read_bytes(), self.registro_di_partenza)

    def motivo(self, rapporto: dict[str, Any], nome: str) -> str:
        return next(saltata["motivo"] for saltata in rapporto["saltati"] if saltata["file"] == nome)


class SoloCioCheLUtenteHaConfermatoTests(BaseImparaTests):
    """L'AI propone, l'utente approva, e nel registro entra solo l'approvato."""

    def test_entra_solo_lo_schema_confermato_e_gli_altri_escono_col_motivo(self) -> None:
        noto = listino_fornitore(self.radice / "noto.xlsx")
        variato = listino_fornitore(self.radice / "variato.xlsx")
        ambiguo = listino_fornitore(self.radice / "ambiguo.xlsx")
        nuovo = listino_fornitore(self.radice / "nuovo.xlsx")
        manifest = self.manifest([
            voce_di_manifest(noto, decisione(state="SCHEMA_NOTO", adapter_id="betulla_v1"), "CONFIRMED"),
            voce_di_manifest(variato, decisione(state="SCHEMA_VARIATO", adapter_id="betulla_v1"), "PENDING"),
            voce_di_manifest(ambiguo, decisione(state="AMBIGUO", role="ignore"), "CONFIRMED"),
            voce_di_manifest(nuovo, decisione(), "CONFIRMED"),
        ])

        uscita, rapporto = self.impara(manifest)

        self.assertEqual(uscita, 0)
        self.assertEqual([imparato["file"] for imparato in rapporto["imparati"]], ["nuovo.xlsx"])
        self.assertEqual([saltata["file"] for saltata in rapporto["saltati"]],
                         ["noto.xlsx", "variato.xlsx", "ambiguo.xlsx"])
        self.assertIn("SCHEMA_NOTO", self.motivo(rapporto, "noto.xlsx"))
        self.assertIn("PENDING", self.motivo(rapporto, "variato.xlsx"))
        self.assertIn("AMBIGUO", self.motivo(rapporto, "ambiguo.xlsx"))
        self.assertEqual(self.voce("fittizio_v1")["supplier_id"], "fittizio")
        # Nessuno degli altri tre ha lasciato traccia: betulla_v1 è ancora quello
        # con cui il programma è partito.
        self.assertEqual(self.voce("betulla_v1")["schema_version"], 1)
        self.assertNotIn("learned_at", self.voce("betulla_v1"))

    def test_una_variazione_non_confermata_non_entra_e_non_e_un_errore(self) -> None:
        """La conferma manca: è il caso normale in cui l'utente non ha ancora
        deciso, non un guasto. Il registro resta identico, byte per byte."""

        listino = listino_fornitore(self.radice / "fornitore.xlsx")
        manifest = self.manifest_di_un_file(listino, conferma="PENDING")

        uscita, rapporto = self.impara(manifest)

        self.assertEqual(uscita, 0)
        self.assertEqual(rapporto["imparati"], [])
        self.assertIn("l'utente non ha confermato", self.motivo(rapporto, "fornitore.xlsx"))
        self.assert_registro_intatto()

    def test_la_colonna_ordine_vuota_confermata_rende_compilabile_un_fornitore_nuovo(self) -> None:
        intestazioni = list(INTESTAZIONI_FORNITORE)
        intestazioni[-1] = None
        listino = listino_fornitore(self.radice / "fornitore.xlsx", intestazioni=intestazioni)
        confermata = mappatura()
        confermata["order_header_blank_confirmed"] = True
        manifest = self.manifest_di_un_file(
            listino, decisione(field_mapping=confermata)
        )

        uscita, _rapporto = self.impara(manifest)

        self.assertEqual(uscita, 0)
        scrittura = self.voce("fittizio_v1")["order_write"]
        self.assertTrue(scrittura["from_field_mapping"])
        self.assertEqual(scrittura["order_column"], "H")
        self.assertTrue(scrittura["allow_blank_header_if_confirmed"])
        self.assertNotIn("expected_header", scrittura)

    def test_una_conferma_scritta_a_meta_non_vale_come_conferma(self) -> None:
        """`REJECTED`, `PENDING` o un campo mancante sono tutti «non approvato»."""

        listino = listino_fornitore(self.radice / "fornitore.xlsx")
        for stato in ("REJECTED", "NOT_REQUIRED", "confirmed", ""):
            with self.subTest(conferma=stato):
                shutil.copyfile(ADAPTERS, self.registro)
                uscita, rapporto = self.impara(self.manifest_di_un_file(listino, conferma=stato))

                self.assertEqual(uscita, 0)
                self.assertEqual(rapporto["imparati"], [])
                self.assert_registro_intatto()


class RifiutiTests(BaseImparaTests):
    """Quello che doveva essere imparato e non si è potuto: uscita 2, motivo scritto."""

    def test_una_mappatura_incompleta_e_un_rifiuto_che_dice_che_cosa_manca(self) -> None:
        """Le verifiche sono quelle del validatore del manifest: qui non c'è
        una seconda copia che può allontanarsi dalla prima."""

        listino = listino_fornitore(self.radice / "fornitore.xlsx")
        senza_ordine = mappatura()
        del senza_ordine["order_column"]
        senza_ordine["columns"].pop("ean")
        manifest = self.manifest_di_un_file(listino, decisione(field_mapping=senza_ordine))

        uscita, rapporto = self.impara(manifest)

        motivo = self.motivo(rapporto, "fornitore.xlsx")
        self.assertEqual(uscita, 2)
        self.assertTrue(motivo.startswith("Rifiutato:"))
        self.assertIn("order_column", motivo)
        self.assertIn("columns.ean oppure ean_unavailable=true", motivo)
        self.assert_registro_intatto()

    def test_una_colonna_ordine_dichiarata_vuota_ma_nominata_viene_rifiutata(self) -> None:
        listino = listino_fornitore(self.radice / "fornitore.xlsx")
        falsa = mappatura()
        falsa["order_header_blank_confirmed"] = True

        uscita, rapporto = self.impara(
            self.manifest_di_un_file(listino, decisione(field_mapping=falsa))
        )

        self.assertEqual(uscita, 2)
        self.assertIn("la conferma vuota", self.motivo(rapporto, "fornitore.xlsx"))
        self.assert_registro_intatto()

    def test_un_documento_senza_riga_di_intestazione_non_si_impara_da_solo(self) -> None:
        """Un'impronta per forma delle colonne — come quella di Larice — la
        scrive una persona nel registro: dedurne le soglie da un profilo
        vorrebbe dire inventarle."""

        listino = listino_fornitore(self.radice / "fornitore.xlsx")
        senza_intestazione = mappatura(header_row=0, assume_available=True)
        senza_intestazione["columns"] = {"description": "C", "unit_price_net": "B",
                                         "pieces_per_carton": "D", "ean": "F",
                                         "supplier_code": "E", "vat": "G"}
        manifest = self.manifest_di_un_file(listino, decisione(field_mapping=senza_intestazione))

        uscita, rapporto = self.impara(manifest)

        self.assertEqual(uscita, 2)
        self.assertIn("non ha una riga di intestazione", self.motivo(rapporto, "fornitore.xlsx"))
        self.assert_registro_intatto()

    def test_una_colonna_dichiarata_e_assente_dal_documento_e_un_rifiuto(self) -> None:
        """Scriverla lo stesso vorrebbe dire un adattatore che, la prima volta
        che serve, si ferma sulla colonna che non c'è."""

        intestazioni = list(INTESTAZIONI_FORNITORE)
        intestazioni[5] = "Codice a barre"
        listino = listino_fornitore(self.radice / "fornitore.xlsx", intestazioni=intestazioni)
        manifest = self.manifest_di_un_file(listino)

        uscita, rapporto = self.impara(manifest)

        motivo = self.motivo(rapporto, "fornitore.xlsx")
        self.assertEqual(uscita, 2)
        self.assertIn("barcodeean", motivo)
        self.assert_registro_intatto()

    def test_la_riga_di_intestazione_deve_essere_nel_profilo(self) -> None:
        """Il documento non si riapre: l'impronta si calcola dal manifest, che
        è quello che l'utente ha avuto davanti quando ha confermato."""

        listino = listino_fornitore(self.radice / "fornitore.xlsx")
        manifest = self.manifest_di_un_file(listino, decisione(field_mapping=mappatura(header_row=97)))

        uscita, rapporto = self.impara(manifest)

        self.assertEqual(uscita, 2)
        self.assertIn("riga di intestazione 97 non è fra quelle che il profilo riporta",
                      self.motivo(rapporto, "fornitore.xlsx"))
        self.assert_registro_intatto()

    def test_il_foglio_dichiarato_deve_essere_nel_documento(self) -> None:
        """E lo si dice prima di scrivere: un'impronta misurata sul foglio
        sbagliato descriverebbe un documento che il lettore non aprirà mai."""

        listino = listino_fornitore(self.radice / "fornitore.xlsx")
        manifest = self.manifest_di_un_file(listino, decisione(field_mapping=mappatura(sheet="Listino")))

        uscita, rapporto = self.impara(manifest)

        motivo = self.motivo(rapporto, "fornitore.xlsx")
        self.assertEqual(uscita, 2)
        self.assertIn("il foglio «Listino» dichiarato dalla mappatura non è nel documento", motivo)
        self.assertIn("«Offerte agosto»", motivo)
        self.assert_registro_intatto()

    def test_un_fornitore_che_si_mangia_l_adattatore_di_un_altro_e_un_rifiuto(self) -> None:
        listino = listino_fornitore(self.radice / "fornitore.xlsx")
        manifest = self.manifest_di_un_file(
            listino, decisione(state="SCHEMA_VARIATO", adapter_id="cipresso_v1", supplier_id="fittizio"))

        uscita, rapporto = self.impara(manifest)

        self.assertEqual(uscita, 2)
        self.assertIn("cipresso", self.motivo(rapporto, "fornitore.xlsx"))
        self.assert_registro_intatto()

    def test_senza_fornitore_l_adattatore_non_lo_cercherebbe_nessuno(self) -> None:
        """L'identificativo qui c'è: manca il fornitore, che è la chiave con cui
        il resto del programma chiama l'adattatore."""

        listino = listino_fornitore(self.radice / "fornitore.xlsx")
        manifest = self.manifest_di_un_file(
            listino, decisione(supplier_id=None, adapter_id="fittizio_v1"))

        uscita, rapporto = self.impara(manifest)

        self.assertEqual(uscita, 2)
        self.assertIn("non dichiara il fornitore", self.motivo(rapporto, "fornitore.xlsx"))
        self.assertEqual(self.voce("fittizio_v1"), {})
        self.assert_registro_intatto()

    def test_due_documenti_con_lo_stesso_identificativo_non_si_sovrascrivono(self) -> None:
        primo = listino_fornitore(self.radice / "primo.xlsx")
        secondo = listino_fornitore(self.radice / "secondo.xlsx")
        manifest = self.manifest([
            voce_di_manifest(primo, decisione(), "CONFIRMED"),
            voce_di_manifest(secondo, decisione(), "CONFIRMED"),
        ])

        uscita, rapporto = self.impara(manifest)

        self.assertEqual(uscita, 2)
        self.assertEqual([imparato["file"] for imparato in rapporto["imparati"]], ["primo.xlsx"])
        self.assertIn("primo.xlsx", self.motivo(rapporto, "secondo.xlsx"))
        self.assertEqual(self.voce("fittizio_v1")["schema_version"], 1)

    def test_uno_schema_che_non_si_riconoscerebbe_non_entra_nel_registro(self) -> None:
        """La prova che questo script non scrive un file che nessuno rilegge.

        Qui il documento è un listino CIPRESSO, e la mappatura confermata nomina
        sei colonne su sette: l'adattatore nuovo sarebbe scritto, ma su questo
        stesso documento continuerebbe a vincere `cipresso_v1`, che ne dichiara
        una in più. Un adattatore che non riconosce nemmeno il documento da cui
        è stato imparato è peggio di niente: la settimana prossima il fornitore
        torna sconosciuto e nel registro c'è una voce che nessuno sa a che cosa
        serva.
        """

        listino = scrivi_foglio(self.radice / "cipresso.xlsx", [
            ["COD.ART.", "DES.ARTICOLO", "UM", "QT", "LISTINO", "COD.EAN", "ORDINE"],
            *[[f"01965{numero}", f"SPAZZOLA {numero}", "PZ", 6, 4.9 + numero,
               f"800097816180{numero}", None] for numero in range(4)],
        ], "Listino al 10-08-2026")
        confermata = {
            "sheet": "Listino al 10-08-2026", "header_row": 1, "data_start_row": 2,
            "columns": {"supplier_code": "COD.ART.", "description": "DES.ARTICOLO",
                        "unit": "UM", "pieces_per_carton": "QT", "unit_price_net": "LISTINO",
                        "ean": "COD.EAN"},
            "assume_available": True, "vat_unavailable": True, "order_column": "G",
        }
        manifest = self.manifest_di_un_file(
            listino, decisione(supplier_id="nuovo_fornitore", field_mapping=confermata))

        uscita, rapporto = self.impara(manifest)

        motivo = self.motivo(rapporto, "cipresso.xlsx")
        self.assertEqual(uscita, 2)
        # Il rifiuto è proprio questo: nessuna verifica è fallita, ma su questo
        # documento vince un altro adattatore.
        self.assertIn("SCHEMA_NOTO «cipresso_v1»", motivo)
        self.assertIn("nuovo_fornitore_v1", motivo)
        self.assertEqual(self.voce("nuovo_fornitore_v1"), {})
        self.assert_registro_intatto()

    def test_un_manifest_illeggibile_esce_col_motivo_e_non_con_una_traccia(self) -> None:
        rotto = self.radice / "input_manifest.json"
        rotto.write_bytes(b"{questo non e' JSON")

        uscita, rapporto = self.impara(rotto)

        self.assertEqual(uscita, 2)
        self.assertIn("il manifest non si legge", self.motivo(rapporto, "input_manifest.json"))
        self.assert_registro_intatto()


class ScritturaNelRegistroTests(BaseImparaTests):
    """Che cosa entra nel registro, e che cosa non deve uscirne."""

    def test_l_adattatore_imparato_porta_le_sue_tracce(self) -> None:
        listino = listino_fornitore(self.radice / "fornitore.xlsx")
        profilo = inspect_sources.profile_file(listino)
        manifest = self.manifest_di_un_file(listino)

        uscita, rapporto = self.impara(manifest)

        self.assertEqual(uscita, 0)
        voce = self.voce("fittizio_v1")
        self.assertEqual(voce["schema_version"], 1)
        self.assertEqual(voce["kind"], "supplier")
        self.assertEqual(voce["supplier_id"], "fittizio")
        self.assertEqual(voce["confirmed_by"], "utente")
        self.assertEqual(voce["learned_from"]["file_name"], "fornitore.xlsx")
        self.assertEqual(voce["learned_from"]["sha256"], profilo["sha256"])
        self.assertTrue(voce["learned_at"].endswith("+00:00"))
        self.assertEqual(voce["field_mapping"], MAPPATURA_FORNITORE)
        firma = voce["header_signature"]
        self.assertEqual(firma["kind"], "headers")
        self.assertEqual((firma["sheet"], firma["header_row"], firma["data_start_row"]),
                         ("Offerte agosto", 1, 2))
        # Le obbligatorie sono le colonne che la mappatura usa davvero: la
        # colonna d'ordine, che nel documento è vuota, non è un'intestazione da
        # pretendere.
        self.assertEqual(firma["required"], ["aliquotaiva", "barcodeean", "codiceinterno",
                                             "disponibilitamerce", "nomearticolo",
                                             "pezziscatola", "prezzonettoeur"])
        self.assertIn("qtaordine", firma["known"])
        self.assertNotIn("qtaordine", firma["required"])
        self.assertEqual(rapporto["registro"], str(self.registro.resolve()))
        self.assertEqual(rapporto["imparati"][0]["scritto"], True)

    def test_una_variazione_confermata_non_perde_i_codici_di_riga_del_fornitore(self) -> None:
        """La prova che conta quando un fornitore cambia schema.

        `larice_v1` dichiara che `SM` marca una riga premio e non merce
        acquistabile. Riscrivere l'adattatore da zero con la sola mappatura
        nuova farebbe sparire quella regola in silenzio, e le righe omaggio
        tornerebbero ordinabili appena il fornitore ci scrive un prezzo.

        ⚠ Dal 22 agosto 2026 la voce non si scrive piu' ADDOSSO alla spedita ma
        accanto, con l'id `larice_v1__locale`: quello che questa prova tiene in
        piedi — le regole di riga, `column_map` fuso, la versione di prima
        conservata — vale sulla voce nuova, e in piu' la spedita resta intatta.
        """

        listino = scrivi_foglio(self.radice / "larice.xlsx", [
            ["EX", "IND", "COD.ART.", "ORD", "PZ CT", "DESCRIZIONE", "PREZZO", "SCONTO", "IVA", "COD.EAN"],
            [None, "I", "C00001", None, 6, "BAGNO VIDOR 500 ML", 2.50, 0.05, 22, "8000000000001"],
            [None, "I", "C00002", None, 12, "SAPONE 300 ML", 1.80, "TP", 22, "8000000000002"],
        ], "Canvass 99 01-05set")
        confermata = {
            "sheet": "Canvass 99 01-05set", "header_row": 1, "data_start_row": 2,
            "columns": {"supplier_code": "COD.ART.", "description": "DESCRIZIONE",
                        "pieces_per_carton": "PZ CT", "unit_price_pre_discount": "PREZZO",
                        "discount": "SCONTO", "vat": "IVA", "ean": "COD.EAN"},
            "assume_available": True, "order_column": "D",
        }
        manifest = self.manifest_di_un_file(listino, decisione(
            state="SCHEMA_VARIATO", adapter_id="larice_v1", supplier_id="larice",
            rationale="Il listino ha una riga di intestazione: le colonne si leggono per nome.",
            field_mapping=confermata))

        uscita, rapporto = self.impara(manifest)

        self.assertEqual(uscita, 0)
        voce = self.voce("larice_v1__locale")
        self.assertEqual(voce["derivato_da"], "larice_v1")
        self.assertEqual(voce["supplier_id"], "larice")
        self.assertEqual(voce["schema_version"], 2)
        self.assertEqual(voce["row_markers"]["codes"]["SM"]["orderable"], False)
        # ⚠ `column_map` dice DOVE stanno le colonne, e c'è chi legge solo
        # quello (il lettore delle soglie con omaggio: il listino Larice non ha
        # intestazioni, non c'è un nome da risolvere). Prima restava fermo alla
        # settimana precedente — `ean: "R"` — mentre `field_mapping` diceva già
        # che l'EAN sta sotto «COD.EAN», cioè in colonna J di questo documento:
        # due parti dello stesso programma leggevano due colonne diverse, e
        # nessuna lo diceva.
        self.assertEqual(voce["column_map"]["ean"], "J")
        self.assertEqual(voce["column_map"]["description"], "F")
        self.assertEqual(voce["field_mapping"]["columns"]["ean"], "COD.EAN")
        # Le voci che la mappatura confermata non nomina restano: `column_map`
        # ne porta anche di non mappate, e cancellarle spegnerebbe chi le usa.
        self.assertEqual(voce["column_map"]["previous_price_note"], "M")
        self.assertEqual(voce["column_map"]["group_label"], "A")
        self.assertEqual(voce["header_signature"]["required"],
                         ["codart", "codean", "descrizione", "iva", "prezzo", "pzct", "sconto"])
        # La versione con cui il programma è partito è ancora tutta lì.
        self.assertEqual(len(voce["previous_versions"]), 1)
        precedente = voce["previous_versions"][0]
        self.assertEqual(precedente["schema_version"], 1)
        self.assertNotIn("header_signature", precedente)
        self.assertEqual(rapporto["imparati"][0]["schema_version"], 2)
        self.assertEqual(rapporto["imparati"][0]["previous_versions"], 1)
        self.assertEqual(rapporto["imparati"][0]["adapter_id"], "larice_v1__locale")
        # E la voce spedita e' ancora dov'era, intera: e' la differenza fra
        # «imparare una variazione» e «perdere quello che il programma sa».
        spedita = next(voce for voce in json.loads(self.registro.read_text(encoding="utf-8"))["adapters"]
                       if voce["id"] == "larice_v1")
        self.assertEqual(spedita["row_markers"]["codes"]["SM"]["orderable"], False)
        self.assertNotIn("derivato_da", spedita)

    def test_un_fornitore_nuovo_continua_a_scrivere_la_sua_voce(self) -> None:
        """La separazione riguarda solo chi sta scrivendo sopra uno spedito.

        Un fornitore che nello spedito non c'e' non ha niente sotto da salvare:
        la sua voce si aggiorna come sempre, e chiamarla `..._v1__locale`
        aggiungerebbe un nome da capire senza proteggere niente.
        """

        listino = listino_fornitore(self.radice / "fornitore.xlsx")
        manifest = self.manifest_di_un_file(listino)

        uscita, rapporto = self.impara(manifest)

        self.assertEqual(uscita, 0)
        self.assertEqual(rapporto["imparati"][0]["adapter_id"], "fittizio_v1")
        self.assertNotIn("derivato_da", self.voce("fittizio_v1"))

    def test_la_prova_calcola_tutto_e_non_scrive_niente(self) -> None:
        listino = listino_fornitore(self.radice / "fornitore.xlsx")
        manifest = self.manifest_di_un_file(listino)

        uscita, rapporto = self.impara(manifest, "--prova")

        self.assertEqual(uscita, 0)
        imparato = rapporto["imparati"][0]
        self.assertEqual(imparato["adapter_id"], "fittizio_v1")
        self.assertEqual(imparato["schema_version"], 1)
        self.assertEqual(imparato["created"], True)
        self.assertEqual(imparato["scritto"], False)
        self.assertEqual(len(imparato["hash"]), 64)
        self.assert_registro_intatto()

    def test_il_rapporto_salvato_e_a_fine_riga_lf(self) -> None:
        listino = listino_fornitore(self.radice / "fornitore.xlsx")
        manifest = self.manifest_di_un_file(listino)
        percorso = self.radice / "rapporti" / "imparati.json"

        uscita, rapporto = self.impara(manifest, "--output", percorso)

        self.assertEqual(uscita, 0)
        contenuto = percorso.read_bytes()
        self.assertNotIn(b"\r\n", contenuto)
        self.assertTrue(contenuto.endswith(b"\n"))
        self.assertEqual(json.loads(contenuto.decode("utf-8")), rapporto)

    def test_il_registro_riscritto_resta_a_fine_riga_lf(self) -> None:
        """I due registri si leggono affiancati quando qualcosa non torna: uno
        scritto in CRLF li farebbe sembrare diversi riga per riga anche dove
        dicono la stessa cosa."""

        listino = listino_fornitore(self.radice / "fornitore.xlsx")

        self.impara(self.manifest_di_un_file(listino))

        contenuto = self.imparato.read_bytes()
        self.assertNotIn(b"\r\n", contenuto)
        self.assertTrue(contenuto.endswith(b"\n"))

    def test_quello_che_si_impara_non_entra_nel_registro_spedito(self) -> None:
        """La ragione per cui i file sono due: il registro spedito sta sotto
        git, e a ogni avvio il PC del negozio lo riporta a com'e' su GitHub.
        Quello che ci fosse finito dentro sparirebbe al doppio clic dopo."""

        listino = listino_fornitore(self.radice / "fornitore.xlsx")

        codice, rapporto = self.impara(self.manifest_di_un_file(listino))

        self.assertEqual(codice, 0, rapporto)
        self.assert_spedito_intatto()
        self.assertTrue(self.imparato.exists())
        self.assertEqual(
            [voce["id"] for voce in json.loads(self.imparato.read_text(encoding="utf-8"))["adapters"]],
            ["fittizio_v1"],
        )

    def test_un_documento_con_un_preambolo_si_impara_dalla_sua_riga(self) -> None:
        """L'intestazione non è sempre la prima riga: il fornitore ci mette
        sopra il titolo del listino e la settimana di validità."""

        listino = listino_fornitore(self.radice / "fornitore.xlsx", preambolo=4)
        manifest = self.manifest_di_un_file(
            listino, decisione(field_mapping=mappatura(header_row=5, data_start_row=6)))

        uscita, _rapporto = self.impara(manifest)

        self.assertEqual(uscita, 0)
        firma = self.voce("fittizio_v1")["header_signature"]
        self.assertEqual((firma["header_row"], firma["data_start_row"]), (5, 6))


class LAdattatoreImparatoNasceConLeDifeseTests(BaseImparaTests):
    """Bloccante 4 della verifica del 12 agosto 2026.

    La firma imparata portava i **nomi** delle intestazioni e non le loro
    posizioni: `posizioni_intestazioni` usciva «non applicabile» e la difesa
    che aveva fermato la trappola BETULLA — una colonna in più in testa, prezzi
    12,00 invece di 3,98 con SCHEMA_NOTO 0.99 — non esisteva per nessuno
    schema imparato. Non è un di più: un adattatore imparato legge per
    posizione ogni volta che una colonna è dichiarata per numero, e scrive
    l'ordine in una colonna indicata per lettera.
    """

    def _impara_e_leggi_firma(self) -> dict[str, Any]:
        listino = listino_fornitore(self.radice / "fornitore.xlsx")
        uscita, rapporto = self.impara(self.manifest_di_un_file(listino))
        self.assertEqual(uscita, 0, rapporto)
        return self.voce("fittizio_v1")["header_signature"]

    def test_la_firma_imparata_dice_dove_sta_ogni_intestazione(self) -> None:
        firma = self._impara_e_leggi_firma()

        self.assertEqual(firma.get("columns"), {
            "disponibilitamerce": 1, "prezzonettoeur": 2, "nomearticolo": 3,
            "pezziscatola": 4, "codiceinterno": 5, "barcodeean": 6,
            "aliquotaiva": 7, "qtaordine": 8,
        })

    def test_la_firma_imparata_ha_la_stessa_forma_di_quella_nativa(self) -> None:
        """Scritta in un'altra forma non verrebbe confrontata con niente."""

        firma = self._impara_e_leggi_firma()
        nativa = next(voce for voce in json.loads(ADAPTERS.read_text(encoding="utf-8"))["adapters"]
                      if voce["id"] == "betulla_v1")["header_signature"]

        for colonne in (firma["columns"], nativa["columns"]):
            for token, posizione in colonne.items():
                self.assertEqual(motore_registro.normalizza(token), token)
                self.assertIsInstance(posizione, int)

    def test_una_colonna_in_piu_in_testa_declassa_lo_schema_imparato(self) -> None:
        """La trappola BETULLA, su un adattatore imparato.

        Le intestazioni sono le stesse — stesso insieme di nomi, nessuna
        novità — e sono tutte spostate di uno. Senza la firma posizionale il
        documento restava SCHEMA_NOTO 0.99 e l'ordine finiva una colonna più
        in là.
        """

        listino = listino_fornitore(self.radice / "fornitore.xlsx")
        uscita, _rapporto = self.impara(self.manifest_di_un_file(listino))
        self.assertEqual(uscita, 0)

        spostato = listino_fornitore(
            self.radice / "spostato.xlsx",
            intestazioni=[None, *INTESTAZIONI_FORNITORE])
        esito = motore_registro.riconosci(inspect_sources.profile_file(spostato)["details"], self.registro)

        self.assertEqual(esito["adapter_id"], "fittizio_v1")
        self.assertEqual(esito["state"], "SCHEMA_VARIATO")
        posizioni = next(v for v in esito["checks"] if v["name"] == "posizioni_intestazioni")
        self.assertFalse(posizioni["ok"])
        self.assertIn("legge per posizione", posizioni["detail"])

    def test_il_listino_da_cui_e_stato_imparato_resta_riconosciuto(self) -> None:
        """Una firma posizionale scritta male renderebbe l'adattatore inutile."""

        listino = listino_fornitore(self.radice / "fornitore.xlsx")
        uscita, _rapporto = self.impara(self.manifest_di_un_file(listino))
        self.assertEqual(uscita, 0)

        esito = motore_registro.riconosci(inspect_sources.profile_file(listino)["details"], self.registro)

        self.assertEqual((esito["state"], esito["adapter_id"]), ("SCHEMA_NOTO", "fittizio_v1"))
        self.assertTrue(all(verifica["ok"] for verifica in esito["checks"]), esito["checks"])

    def test_un_intestazione_ripetuta_prende_una_posizione_sola(self) -> None:
        """Su ACERO «COSTO IMPON.» compare in colonna 9 e in colonna 15.

        Le due parti — chi scrive la firma e chi la rilegge — devono contare
        allo stesso modo, altrimenti l'adattatore risulterebbe fuori posto
        proprio sul documento da cui è stato imparato.
        """

        ripetute = [*INTESTAZIONI_FORNITORE, "Prezzo Netto EUR"]
        listino = listino_fornitore(self.radice / "fornitore.xlsx", intestazioni=ripetute)
        uscita, rapporto = self.impara(self.manifest_di_un_file(listino))

        self.assertEqual(uscita, 0, rapporto)
        self.assertEqual(self.voce("fittizio_v1")["header_signature"]["columns"]["prezzonettoeur"], 9)
        esito = motore_registro.riconosci(inspect_sources.profile_file(listino)["details"], self.registro)
        self.assertEqual(esito["state"], "SCHEMA_NOTO")


def albero_di_prova(cartella: Path) -> Path:
    """Una copia dell'albero della skill, per far girare la catena su un registro finto.

    Il registro lo trova `registro.py` accanto a sé (`../references`), quindi
    l'unico modo di eseguire gli script veri contro un registro di prova è
    dare loro un albero di prova. Il registro vero non deve poter essere
    toccato da un collaudo: è il documento che una persona legge prima del
    commit.
    """

    radice = cartella / "skill"
    for parte in ("scripts", "app", "references"):
        (radice / parte).mkdir(parents=True, exist_ok=True)
    for sorgente in SCRIPTS.glob("*.py"):
        shutil.copyfile(sorgente, radice / "scripts" / sorgente.name)
    for sorgente in APP.glob("*.py"):
        shutil.copyfile(sorgente, radice / "app" / sorgente.name)
    shutil.copyfile(ADAPTERS, radice / "references" / "adapters.json")
    return radice


class CatenaInteraTests(unittest.TestCase):
    """Dalla profilazione al riconoscimento, con gli script veri.

    È la sola prova che dice se la memoria degli schemi funziona davvero:
    senza, si è scritto un file JSON che nessuno rilegge.
    """

    maxDiff = None

    def setUp(self) -> None:
        self.cartella = Path(tempfile.mkdtemp(prefix="collaudo_catena_"))
        self.addCleanup(shutil.rmtree, self.cartella, True)
        self.radice = albero_di_prova(self.cartella)
        self.registro = self.radice / "references" / "adapters.json"
        # Il registro vero com'era prima del collaudo: è il termine di paragone
        # con cui si prova che nessuno l'ha toccato.
        self.registro_vero_prima = ADAPTERS.read_bytes()

    def esegui(self, nome: str, *argomenti: object) -> subprocess.CompletedProcess[bytes]:
        esito = subprocess.run(
            [sys.executable, str(self.radice / "scripts" / nome), *(str(v) for v in argomenti)],
            cwd=self.radice, capture_output=True, env=AMBIENTE,
        )
        if esito.returncode != 0:
            raise AssertionError(
                f"{nome} non è riuscito ({esito.returncode}).\n"
                f"stdout:\n{esito.stdout.decode('utf-8', errors='replace')}\n"
                f"stderr:\n{esito.stderr.decode('utf-8', errors='replace')}"
            )
        return esito

    def togli_dal_registro_di_prova(self, identificativo: str) -> None:
        """La premessa del collaudo se la costruisce il collaudo.

        «Questo documento oggi non lo conosce nessuno» era vero perché il
        registro vero conteneva sei voci e basta: bastava che l'utente ne
        imparasse una — cioè che usasse la funzione per cui il registro
        esiste — perché la premessa cadesse e il collaudo diventasse rosso
        senza che niente fosse rotto. Adesso la premessa la si stabilisce
        togliendo quella voce dalla **copia** su cui il collaudo lavora.
        """

        documento = json.loads(self.registro.read_text(encoding="utf-8"))
        documento["adapters"] = [voce for voce in documento["adapters"]
                                 if voce.get("id") != identificativo]
        with self.registro.open("w", encoding="utf-8", newline="\n") as flusso:
            json.dump(documento, flusso, ensure_ascii=False, indent=2)
            flusso.write("\n")

    def catena(self, listino: Path, proposta: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Profila, applica la decisione confermata, impara, riprofila."""

        profili = self.cartella / "input_profiles.json"
        self.esegui("inspect_sources.py", listino, "--output", profili)
        prima = json.loads(profili.read_text(encoding="utf-8"))["profiles"][0]["deterministic_hint"]

        decisioni = self.cartella / "decisioni.json"
        decisioni.write_bytes(json.dumps(
            {"decisions": [{"file_name": listino.name, **proposta,
                            "user_confirmation": {"required": True, "status": "CONFIRMED"}}]},
            ensure_ascii=False).encode("utf-8"))
        manifest = self.cartella / "input_manifest.json"
        self.esegui("apply_preflight_decisions.py", "--profiles", profili,
                    "--decisions", decisioni, "--output", manifest)

        self.esegui("impara_adattatore.py", "--manifest", manifest, "--adapters", self.registro)

        dopo_profili = self.cartella / "input_profiles_2.json"
        self.esegui("inspect_sources.py", listino, "--output", dopo_profili)
        dopo = json.loads(dopo_profili.read_text(encoding="utf-8"))["profiles"][0]["deterministic_hint"]
        return prima, dopo

    def test_un_fornitore_confermato_oggi_e_noto_domani(self) -> None:
        listino = listino_fornitore(self.cartella / "fornitore_fittizio.xlsx")

        prima, dopo = self.catena(listino, {
            "state": "NUOVO_FORNITORE", "role": "supplier", "supplier_id": "fittizio",
            "rationale": "Nessun adattatore compatibile.", "field_mapping": mappatura(),
        })

        self.assertEqual(prima["state"], "AMBIGUO")
        self.assertIsNone(prima["adapter_id"])
        self.assertEqual(dopo["state"], "SCHEMA_NOTO")
        self.assertEqual(dopo["adapter_id"], "fittizio_v1")
        self.assertEqual(dopo["confidence"], 0.99)
        self.assertTrue(all(verifica["ok"] for verifica in dopo["checks"]))

    @unittest.skipUnless(
        (LISTINI / "Listino3_33.xlsx").is_file(),
        f"Manca il listino vero in {LISTINI}. "
        "Si può indicare un'altra cartella con la variabile d'ambiente LISTINI_STORICI.",
    )
    def test_la_catena_intera_su_un_listino_vero(self) -> None:
        """Il secondo schema CIPRESSO: oggi resta AMBIGUO, e lo imparerà la
        Fase 5 con la conferma dell'utente. Qui la conferma è simulata dal file
        di decisioni, il registro è una copia e il listino vero è di sola
        lettura: si lavora su una copia, perché è già successo di rovinarlo.
        """

        originale = LISTINI / "Listino3_33.xlsx"
        prima_del_listino = originale.stat()
        listino = self.cartella / originale.name
        shutil.copyfile(originale, listino)
        # Se qualcuno l'ha già imparato, la premessa la ristabilisce il
        # collaudo sulla propria copia del registro: il registro vero non si
        # tocca, e questo file non deve tornare rosso perché il programma ha
        # fatto il suo mestiere.
        self.togli_dal_registro_di_prova("cipresso_listino_v1")

        prima, dopo = self.catena(listino, {
            "state": "NUOVO_FORNITORE", "role": "supplier", "supplier_id": "cipresso_listino",
            "adapter_id": "cipresso_listino_v1",
            "rationale": "Secondo schema CIPRESSO: intestazione alla riga 2, nessuna colonna d'ordine.",
            "field_mapping": {
                "sheet": "Listino", "header_row": 2, "data_start_row": 3,
                "columns": {"supplier_code": "COD.ART.", "description": "DES.ARTICOLO",
                            "unit": "UM", "pieces_per_carton": "QT",
                            "unit_price_net": "LISTINO", "ean": "COD.EAN"},
                "assume_available": True, "vat_unavailable": True, "order_column": "H",
            },
        })

        self.assertEqual(prima["state"], "AMBIGUO")
        self.assertEqual(dopo["state"], "SCHEMA_NOTO")
        self.assertEqual(dopo["adapter_id"], "cipresso_listino_v1")
        self.assertEqual(dopo["signature"]["sheet"], "Listino")
        self.assertEqual(dopo["signature"]["header_row"], 2)
        self.assertEqual(dopo["missing_headers"], [])
        self.assertTrue(all(verifica["ok"] for verifica in dopo["checks"]))
        # Il listino vero non è stato aperto in scrittura da nessuno.
        dopo_del_listino = originale.stat()
        self.assertEqual((prima_del_listino.st_size, prima_del_listino.st_mtime_ns),
                         (dopo_del_listino.st_size, dopo_del_listino.st_mtime_ns))
        # E il registro vero nemmeno: quello riscritto è la copia nell'albero di prova.
        # ⚠ La proprietà è «il registro vero non è stato toccato», e si prova
        # confrontandolo con se stesso prima e dopo. Prima si fissava l'elenco
        # esatto dei suoi adattatori: bastava impararne uno — cioè usare la
        # funzione per cui il registro esiste — perché questo collaudo
        # diventasse rosso senza che niente fosse rotto.
        imparato = motore_registro.percorso_imparato(self.registro)
        self.assertTrue(imparato.is_file(), "l'adattatore imparato deve stare in un file suo")
        imparati = [voce["id"] for voce in json.loads(imparato.read_text(encoding="utf-8"))["adapters"]]
        spediti = [voce["id"] for voce in json.loads(ADAPTERS.read_text(encoding="utf-8"))["adapters"]]
        self.assertTrue(imparati, "la catena deve aver imparato qualcosa")
        self.assertEqual([voce for voce in imparati if voce in spediti], [],
                         "quello che si impara non deve entrare nel registro spedito")
        self.assertEqual(ADAPTERS.read_bytes(), self.registro_vero_prima,
                         "il registro vero non deve cambiare di un byte durante un collaudo")


class LaRegolaDellInizioDeiDatiSiImparaTests(BaseImparaTests):
    """Un marcatore confermato deve sopravvivere all'apprendimento.

    Se si perdesse per strada, il fornitore tornerebbe a essere tagliato da un
    numero di riga congelato la settimana prossima: e' il difetto che la regola
    chiude, e sparirebbe proprio nel passaggio che dovrebbe conservarla.
    """

    MARCATORE = {"column": "A", "equals": "LISTINO", "offset": 1}

    def listino_con_separatore(self) -> Path:
        righe: list[list[Any]] = [list(INTESTAZIONI_FORNITORE),
                                  ["LISTINO", None, None, None, None, None, None, None]]
        for numero in range(3):
            righe.append(["SI", 1.25 + numero, f"Prodotto {numero}", 6, f"FIT-00{numero}",
                          f"800000000000{numero}", 22, None])
        return scrivi_foglio(self.radice / "fornitore.xlsx", righe)

    def impara_con_marcatore(self) -> dict[str, Any]:
        listino = self.listino_con_separatore()
        proposta = decisione(field_mapping=mappatura(
            data_start_row=3, data_start_marker=self.MARCATORE))
        uscita, rapporto = self.impara(self.manifest_di_un_file(listino, proposta))
        self.assertEqual(uscita, 0, rapporto)
        return self.voce("fittizio_v1")

    def test_la_regola_entra_nel_registro_insieme_alla_mappatura(self) -> None:
        voce = self.impara_con_marcatore()

        self.assertEqual(voce["field_mapping"]["data_start_marker"], self.MARCATORE)
        self.assertEqual(voce["header_signature"]["data_start_marker"], self.MARCATORE)
        # Il numero resta accanto alla regola, e l'impronta dice a che cosa
        # serve: senza quella frase qualcuno lo leggerebbe come la regola.
        self.assertEqual(voce["header_signature"]["data_start_row"], 3)
        self.assertIn("ricalcolato a ogni lettura", voce["header_signature"]["data_start_note"])

    def test_uno_schema_senza_regola_non_ne_inventa_una(self) -> None:
        listino = listino_fornitore(self.radice / "fornitore.xlsx")
        uscita, rapporto = self.impara(self.manifest_di_un_file(listino))
        self.assertEqual(uscita, 0, rapporto)

        voce = self.voce("fittizio_v1")
        self.assertNotIn("data_start_marker", voce["field_mapping"])
        self.assertNotIn("data_start_marker", voce["header_signature"])
        self.assertNotIn("data_start_note", voce["header_signature"])

    def test_una_regola_scritta_male_non_entra_nel_registro(self) -> None:
        """Il validatore della mappatura è lo stesso di chi legge davvero."""

        listino = self.listino_con_separatore()
        proposta = decisione(field_mapping=mappatura(
            data_start_row=3, data_start_marker={"column": "A"}))

        uscita, rapporto = self.impara(self.manifest_di_un_file(listino, proposta))

        self.assertEqual(uscita, 2)
        self.assertIn("equals", self.motivo(rapporto, "fornitore.xlsx"))
        self.assert_registro_intatto()


if __name__ == "__main__":
    unittest.main()
