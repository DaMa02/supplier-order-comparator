"""Only the schema the operator confirmed is written to the adapter registry.

`impara_adattatore.py` is what prevents an already-confirmed supplier from
going back to unrecognised next week: without it, an approved schema stays
in the run's manifest and someone would have to copy it by hand into
`references/adapters.json`.

Two invariants these tests defend:

- an entry the operator hasn't confirmed never gets written, for any reason;
- a rejection is never silent: it exits with a written reason and exit code
  `2`, because a silent failure here means a supplier goes back to
  unrecognised with no record of why.

The real registry is never touched: every test works on a copy in a
temporary directory, and real price lists are copied before being fed
through the pipeline.
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
# A frozen fixture, not `references/adapters.json`: the program rewrites that
# file when it learns a confirmed schema, and these tests need a registry
# whose exact contents they know in advance. Same note in
# `tests/test_registro_impronte.py`.
ADAPTERS = SKILL_ROOT / "tests" / "fixtures" / "adapters_nativi.json"
for cartella in (SCRIPTS, APP):
    if str(cartella) not in sys.path:
        sys.path.insert(0, str(cartella))

import inspect_sources  # noqa: E402
# Aliased to avoid confusing `self.registro` (a file path) with the module
# that reads and writes it, in tests that use both together.
import registro as motore_registro  # noqa: E402

# Real price lists live outside the repo; this env var points at them without
# touching test code.
LISTINI = Path(os.environ.get("LISTINI_STORICI", str(SKILL_ROOT / "listini-storici")))

# The scripts print Italian text with accented characters. Without this, on
# Windows the output would decode as cp1252 and the reason-matching
# assertions would see replacement characters instead of the actual words.
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
    """Return a copy of the confirmed mapping, with the given overrides."""

    copia = json.loads(json.dumps(MAPPATURA_FORNITORE))
    copia.update(modifiche)
    return copia


def decisione(**modifiche: Any) -> dict[str, Any]:
    """Return a confirmed field-mapping decision, as the guided UI writes it."""

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
    """Build a price list from a supplier no adapter recognises."""

    righe: list[list[Any]] = [["Listino promozionale"] for _ in range(preambolo)]
    righe.append(list(intestazioni if intestazioni is not None else INTESTAZIONI_FORNITORE))
    for numero in range(3):
        righe.append(["SI", 1.25 + numero, f"Prodotto {numero}", 6, f"FIT-00{numero}",
                      f"800000000000{numero}", 22, None])
    return scrivi_foglio(percorso, righe, nome_foglio)


def voce_di_manifest(percorso: Path, proposta: dict[str, Any], stato_conferma: str) -> dict[str, Any]:
    """Build a manifest entry shaped like the one `apply_preflight_decisions` writes.

    The profile is the real one from the inspector; building it by hand would
    decouple the test from what the pipeline actually produces.
    """

    profilo = inspect_sources.profile_file(percorso)
    return {**profilo, "ai_preflight": proposta,
            "user_confirmation": {"required": True, "status": stato_conferma}}


class BaseImparaTests(unittest.TestCase):
    """Each test works on its own copy of the registry; the real one is never touched."""

    maxDiff = None

    def setUp(self) -> None:
        self.radice = Path(tempfile.mkdtemp(prefix="collaudo_impara_"))
        self.addCleanup(shutil.rmtree, self.radice, True)
        self.registro = self.radice / "adapters.json"
        shutil.copyfile(ADAPTERS, self.registro)
        self.registro_di_partenza = self.registro.read_bytes()
        # What gets learned is not written back into the shipped registry
        # (tracked by git; a fresh checkout would revert it) but into a
        # separate, untracked file. The program reads both merged together.
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
        except ValueError as errore:  # pragma: no cover - only reached to report a broken run
            raise AssertionError(
                f"impara_adattatore.py non ha stampato un rapporto JSON.\n"
                f"stdout:\n{uscita}\nstderr:\n{esito.stderr.decode('utf-8', errors='replace')}"
            ) from errore

    def documento(self) -> dict[str, Any]:
        """Return the registry as the program sees it: shipped entries plus learned ones."""

        return {"adapters": motore_registro.adattatori(self.registro)}

    def voce(self, identificativo: str) -> dict[str, Any]:
        return next((voce for voce in self.documento()["adapters"]
                     if voce["id"] == identificativo), {})

    def assert_registro_intatto(self) -> None:
        """Assert nothing was learned: neither the shipped file nor the learned one changed."""

        self.assertEqual(self.registro.read_bytes(), self.registro_di_partenza)
        self.assertFalse(self.imparato.exists())

    def assert_spedito_intatto(self) -> None:
        """Assert something was learned, but not into the file a fresh checkout reverts."""

        self.assertEqual(self.registro.read_bytes(), self.registro_di_partenza)

    def motivo(self, rapporto: dict[str, Any], nome: str) -> str:
        return next(saltata["motivo"] for saltata in rapporto["saltati"] if saltata["file"] == nome)


class SoloCioCheLUtenteHaConfermatoTests(BaseImparaTests):
    """Only the confirmed entry gets written; the rest is skipped with a reason."""

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
        # None of the other three left a trace: betulla_v1 is still the entry
        # the registry started with.
        self.assertEqual(self.voce("betulla_v1")["schema_version"], 1)
        self.assertNotIn("learned_at", self.voce("betulla_v1"))

    def test_una_variazione_non_confermata_non_entra_e_non_e_un_errore(self) -> None:
        """A missing confirmation is the normal case of the operator not having
        decided yet, not a failure. The registry stays identical, byte for byte."""

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
        """`REJECTED`, `PENDING`, or a missing field all count as "not approved"."""

        listino = listino_fornitore(self.radice / "fornitore.xlsx")
        for stato in ("REJECTED", "NOT_REQUIRED", "confirmed", ""):
            with self.subTest(conferma=stato):
                shutil.copyfile(ADAPTERS, self.registro)
                uscita, rapporto = self.impara(self.manifest_di_un_file(listino, conferma=stato))

                self.assertEqual(uscita, 0)
                self.assertEqual(rapporto["imparati"], [])
                self.assert_registro_intatto()


class RifiutiTests(BaseImparaTests):
    """What should have been learned but couldn't: exit 2, with the reason written out."""

    def test_una_mappatura_incompleta_e_un_rifiuto_che_dice_che_cosa_manca(self) -> None:
        """The checks reuse the manifest validator's own rules, so there's no
        second copy of them that could drift from the first."""

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
        """A positional (headerless) fingerprint, like Larice's, is written by a
        person directly into the registry; deriving its thresholds from a
        document profile would mean inventing them."""

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
        """Writing it anyway would produce an adapter that fails the first time
        it's actually used, on the column that isn't there."""

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
        """The document is never reopened: the fingerprint is computed from the
        manifest, which is what the operator actually saw when confirming."""

        listino = listino_fornitore(self.radice / "fornitore.xlsx")
        manifest = self.manifest_di_un_file(listino, decisione(field_mapping=mappatura(header_row=97)))

        uscita, rapporto = self.impara(manifest)

        self.assertEqual(uscita, 2)
        self.assertIn("riga di intestazione 97 non è fra quelle che il profilo riporta",
                      self.motivo(rapporto, "fornitore.xlsx"))
        self.assert_registro_intatto()

    def test_il_foglio_dichiarato_deve_essere_nel_documento(self) -> None:
        """Checked before writing: a fingerprint measured on the wrong sheet
        would describe a document the reader will never actually open."""

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
        """The adapter id is present; what's missing is the supplier id, which
        is the key the rest of the program uses to look the adapter up."""

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
        """Guard against writing an adapter no document would ever match.

        The confirmed mapping here names six columns out of seven; the new
        adapter would be written, but on this same document `cipresso_v1`
        would keep winning since it declares one more column. An adapter that
        can't even recognise the document it was learned from is worse than
        nothing: next week the supplier is unrecognised again and the
        registry carries an entry nobody knows the purpose of.
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
        # This is the rejection itself: no validation check failed, but another
        # adapter still wins on this document.
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
    """What a learned entry looks like, and what must never come out of it."""

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
        # Required headers are only the columns the mapping actually uses: the
        # order column, empty in this document, isn't one to require.
        self.assertEqual(firma["required"], ["aliquotaiva", "barcodeean", "codiceinterno",
                                             "disponibilitamerce", "nomearticolo",
                                             "pezziscatola", "prezzonettoeur"])
        self.assertIn("qtaordine", firma["known"])
        self.assertNotIn("qtaordine", firma["required"])
        self.assertEqual(rapporto["registro"], str(self.registro.resolve()))
        self.assertEqual(rapporto["imparati"][0]["scritto"], True)

    def test_una_variazione_confermata_non_perde_i_codici_di_riga_del_fornitore(self) -> None:
        """The test that matters when a supplier's schema changes.

        `larice_v1` declares that a `SM` row marks a bonus row, not
        purchasable stock. Rewriting the adapter from scratch with only the
        new mapping would silently drop that rule, and bonus rows would
        become orderable again as soon as the supplier writes a price on them.

        The learned entry is written next to the shipped one, under the id
        `larice_v1__locale`, not on top of it: the row rules, the merged
        `column_map`, and the previous version are all preserved on the new
        entry, while the shipped entry stays untouched.
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
        # `column_map` records WHERE each column sits, positionally, and some
        # readers (the free-goods-threshold reader; Larice has no header row to
        # resolve names against) only consult that. It must be kept in sync
        # with `field_mapping`, which is the source of truth for the mapping
        # that was actually confirmed.
        self.assertEqual(voce["column_map"]["ean"], "J")
        self.assertEqual(voce["column_map"]["description"], "F")
        self.assertEqual(voce["field_mapping"]["columns"]["ean"], "COD.EAN")
        # Entries the confirmed mapping doesn't name are kept too: `column_map`
        # also carries unmapped columns, and dropping them would break whatever
        # else reads them.
        self.assertEqual(voce["column_map"]["previous_price_note"], "M")
        self.assertEqual(voce["column_map"]["group_label"], "A")
        self.assertEqual(voce["header_signature"]["required"],
                         ["codart", "codean", "descrizione", "iva", "prezzo", "pzct", "sconto"])
        # The version the program started with is fully preserved.
        self.assertEqual(len(voce["previous_versions"]), 1)
        precedente = voce["previous_versions"][0]
        self.assertEqual(precedente["schema_version"], 1)
        self.assertNotIn("header_signature", precedente)
        self.assertEqual(rapporto["imparati"][0]["schema_version"], 2)
        self.assertEqual(rapporto["imparati"][0]["previous_versions"], 1)
        self.assertEqual(rapporto["imparati"][0]["adapter_id"], "larice_v1__locale")
        # The shipped entry is still there, unchanged: this is what separates
        # learning a schema variation from losing what the program already knew.
        spedita = next(voce for voce in json.loads(self.registro.read_text(encoding="utf-8"))["adapters"]
                       if voce["id"] == "larice_v1")
        self.assertEqual(spedita["row_markers"]["codes"]["SM"]["orderable"], False)
        self.assertNotIn("derivato_da", spedita)

    def test_un_fornitore_nuovo_continua_a_scrivere_la_sua_voce(self) -> None:
        """The `__locale` split only applies when writing over a shipped entry.

        A supplier absent from the shipped registry has nothing underneath to
        protect: its entry updates in place as usual, and naming it
        `..._v1__locale` would add an id to parse without protecting anything.
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
        """The shipped and learned registries get diffed side by side when
        something looks wrong; CRLF line endings would make every line look
        different even where the content agrees."""

        listino = listino_fornitore(self.radice / "fornitore.xlsx")

        self.impara(self.manifest_di_un_file(listino))

        contenuto = self.imparato.read_bytes()
        self.assertNotIn(b"\r\n", contenuto)
        self.assertTrue(contenuto.endswith(b"\n"))

    def test_quello_che_si_impara_non_entra_nel_registro_spedito(self) -> None:
        """The reason there are two files: the shipped registry is tracked by
        git, and the deployment on the store PC resets it to the tracked
        state. Anything learned into that file would be lost on the next reset."""

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
        """The header isn't always the first row: a supplier can put the price
        list's title and validity dates above it."""

        listino = listino_fornitore(self.radice / "fornitore.xlsx", preambolo=4)
        manifest = self.manifest_di_un_file(
            listino, decisione(field_mapping=mappatura(header_row=5, data_start_row=6)))

        uscita, _rapporto = self.impara(manifest)

        self.assertEqual(uscita, 0)
        firma = self.voce("fittizio_v1")["header_signature"]
        self.assertEqual((firma["header_row"], firma["data_start_row"]), (5, 6))


class LAdattatoreImparatoNasceConLeDifeseTests(BaseImparaTests):
    """A learned adapter's fingerprint must include column positions, not just names.

    Without positions, `posizioni_intestazioni` came back "not applicable" and
    the safeguard that catches a shifted header row (an extra leading column,
    so prices are read one column off) didn't exist for any learned schema.
    This isn't a nice-to-have: a learned adapter reads by position whenever a
    column is declared by index, and writes the order quantity into a column
    given by letter.
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
        """Written in a different shape, it wouldn't compare against anything."""

        firma = self._impara_e_leggi_firma()
        nativa = next(voce for voce in json.loads(ADAPTERS.read_text(encoding="utf-8"))["adapters"]
                      if voce["id"] == "betulla_v1")["header_signature"]

        for colonne in (firma["columns"], nativa["columns"]):
            for token, posizione in colonne.items():
                self.assertEqual(motore_registro.normalizza(token), token)
                self.assertIsInstance(posizione, int)

    def test_una_colonna_in_piu_in_testa_declassa_lo_schema_imparato(self) -> None:
        """A shifted header row, on a learned adapter.

        The headers are the same set of names, nothing new, just all shifted
        one column over. Without the positional fingerprint, the document
        would keep matching with high confidence and the order quantity would
        land one column off.
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
        """A badly written positional fingerprint would make the adapter useless."""

        listino = listino_fornitore(self.radice / "fornitore.xlsx")
        uscita, _rapporto = self.impara(self.manifest_di_un_file(listino))
        self.assertEqual(uscita, 0)

        esito = motore_registro.riconosci(inspect_sources.profile_file(listino)["details"], self.registro)

        self.assertEqual((esito["state"], esito["adapter_id"]), ("SCHEMA_NOTO", "fittizio_v1"))
        self.assertTrue(all(verifica["ok"] for verifica in esito["checks"]), esito["checks"])

    def test_un_intestazione_ripetuta_prende_una_posizione_sola(self) -> None:
        """A duplicated header must resolve to a single position, consistently.

        One real supplier's price list repeats a header text in two columns;
        whoever writes the fingerprint and whoever reads it back must count
        occurrences the same way, or the adapter would mismatch on the very
        document it was learned from.
        """

        ripetute = [*INTESTAZIONI_FORNITORE, "Prezzo Netto EUR"]
        listino = listino_fornitore(self.radice / "fornitore.xlsx", intestazioni=ripetute)
        uscita, rapporto = self.impara(self.manifest_di_un_file(listino))

        self.assertEqual(uscita, 0, rapporto)
        self.assertEqual(self.voce("fittizio_v1")["header_signature"]["columns"]["prezzonettoeur"], 9)
        esito = motore_registro.riconosci(inspect_sources.profile_file(listino)["details"], self.registro)
        self.assertEqual(esito["state"], "SCHEMA_NOTO")


def albero_di_prova(cartella: Path) -> Path:
    """Build a copy of the project tree, to run the real scripts against a fake registry.

    `registro.py` locates the registry relative to itself (`../references`),
    so the only way to run the real scripts against a test registry is to
    give them a whole copied tree. The real registry must never be reachable
    from a test: it's the document a person reviews before a commit.
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
    """From profiling to recognition, running the real scripts end to end.

    This is the only test that proves the adapter-learning loop actually
    closes: without it, this would just be a JSON file nothing reads back.
    """

    maxDiff = None

    def setUp(self) -> None:
        self.cartella = Path(tempfile.mkdtemp(prefix="collaudo_catena_"))
        self.addCleanup(shutil.rmtree, self.cartella, True)
        self.radice = albero_di_prova(self.cartella)
        self.registro = self.radice / "references" / "adapters.json"
        # A snapshot of the real registry before the test, to prove nothing
        # touched it.
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
        """Set up the test's own precondition instead of relying on the shared registry's state.

        "This document is unrecognised today" held only as long as the real
        registry didn't yet contain a matching entry; the moment someone
        learned one — using the feature this test exercises — the precondition
        would break and the test would fail with nothing actually wrong. It's
        established here by removing that entry from the test's own copy.
        """

        documento = json.loads(self.registro.read_text(encoding="utf-8"))
        documento["adapters"] = [voce for voce in documento["adapters"]
                                 if voce.get("id") != identificativo]
        with self.registro.open("w", encoding="utf-8", newline="\n") as flusso:
            json.dump(documento, flusso, ensure_ascii=False, indent=2)
            flusso.write("\n")

    def catena(self, listino: Path, proposta: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Profile, apply the confirmed decision, learn, and profile again."""

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
        """A second, alternate schema from a supplier already known under a
        different layout: unrecognised until confirmed by the operator. Here
        the confirmation is simulated by a decisions file, the registry is a
        copy, and the real price list is treated as read-only: the pipeline
        works on a copy of it, not the original.
        """

        originale = LISTINI / "Listino3_33.xlsx"
        prima_del_listino = originale.stat()
        listino = self.cartella / originale.name
        shutil.copyfile(originale, listino)
        # If this adapter has already been learned elsewhere, the precondition
        # is re-established on the test's own registry copy: the real registry
        # is untouched, and this test must not fail just because the program
        # already did its job.
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
        # Nothing opened the real price list for writing.
        dopo_del_listino = originale.stat()
        self.assertEqual((prima_del_listino.st_size, prima_del_listino.st_mtime_ns),
                         (dopo_del_listino.st_size, dopo_del_listino.st_mtime_ns))
        # Nor the real registry: the one rewritten is the copy in the test tree.
        # The property to check is "the real registry wasn't touched", proved by
        # comparing it against itself before and after, rather than by
        # asserting its exact list of adapters (which would break the moment
        # anyone actually learned a new one).
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
    """A confirmed data-start marker must survive being learned into the registry.

    If it were dropped in the process, the supplier would go back to being
    cut off at a fixed row number next week — exactly the problem the marker
    exists to solve, and it would happen in the very step meant to preserve it.
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
        # The resolved row number is kept alongside the marker, and the
        # fingerprint states what it's for; without that note, someone could
        # mistake it for the authoritative rule.
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
        """The mapping validator is the same one the real reader uses."""

        listino = self.listino_con_separatore()
        proposta = decisione(field_mapping=mappatura(
            data_start_row=3, data_start_marker={"column": "A"}))

        uscita, rapporto = self.impara(self.manifest_di_un_file(listino, proposta))

        self.assertEqual(uscita, 2)
        self.assertIn("equals", self.motivo(rapporto, "fornitore.xlsx"))
        self.assert_registro_intatto()


if __name__ == "__main__":
    unittest.main()
