"""Tests for the pipeline orchestrator (`pipeline_jobs`).

Four properties matter most, because a plausible implementation gets each
one wrong silently:

1. If a step fails, the previous comparison stays untouched. This is the
   promise the "Ricalcola" button rests on: pressing it must never cost the
   week's work. Checked here per individual step.
2. A step that claims to have written an artifact, but didn't, stops the
   pipeline. The defense is a registry of file hashes, not a promise.
3. Activation has numeric preconditions: zero products, zero suppliers, or
   a case count that doesn't add up must never activate a comparison.
4. The comparison against the previous run warns but never stops. A price
   list that loses half its rows or doubles its prices produces a warning
   at the top of the page, and the run still completes.

No network calls and no real subprocesses: the executor is injected and
simulates the nine pipeline commands by writing the artifacts they would
really write. Testing against the real scripts is a separate acceptance test.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
for cartella in (SKILL_ROOT / "app", SKILL_ROOT / "scripts"):
    if str(cartella) not in sys.path:
        sys.path.insert(0, str(cartella))

import pipeline_jobs  # noqa: E402
from pipeline_jobs import (  # noqa: E402
    ConfigurazionePipeline,
    LavoroGiaInCorso,
    PipelineJobManager,
    RisultatoComando,
)


# The barcode is not decoration: it decides whether row 1 of the new
# comparison is the same item as row 1 of the previous one. A fixture that
# renames a product without changing its EAN would describe a replaced
# product, and decisions made on the old one would be discarded.
CONFRONTO_PRECEDENTE = {
    "run": {"id": "vecchia", "status": "ready"},
    "files": [],
    "suppliers": [{"id": "betulla", "name": "BETULLA"}],
    "products": [{"id": "product:1", "name": "Vecchio", "ean": "8000000000001", "offers": []}],
    "warnings": [],
}


def profilo(nome: str, adattatore: str, *, stato: str = "SCHEMA_NOTO", quando: str = "2026-08-12T10:00:00+00:00") -> dict:
    return {
        "profile_id": nome.replace(".", "-"),
        "path": f"C:/uploads/{nome}",
        "file_name": nome,
        "sha256": "0" * 64,
        "modified_at": quando,
        "details": {},
        "deterministic_hint": {"state": stato, "adapter_id": adattatore, "confidence": 0.99},
    }


class EsecutoreFinto:
    """Simulates the nine pipeline commands by writing real artifacts, with no subprocesses.

    Each command's behavior can be overridden per test: `uscite` sets its
    exit code, `salta` stops it from writing its own output files, `esplode`
    makes it raise. This is how failure modes get tested without having to
    break the real scripts.
    """

    def __init__(self, **impostazioni) -> None:
        self.uscite: dict[str, int] = impostazioni.get("uscite") or {}
        self.salta: set[str] = set(impostazioni.get("salta") or ())
        self.profili: list[dict] = list(impostazioni.get("profili") or [
            profilo("gestionale.xlsx", "gestionale_v1"),
            profilo("betulla.xlsx", "betulla_v1"),
        ])
        self.errori_profilazione: list[dict] = list(impostazioni.get("errori_profilazione") or [])
        self.audit: dict = impostazioni.get("audit") or {
            "master": {"rows": 3},
            "sources": {"betulla": {"rows": 100}},
            "price_summary": {"betulla": {"usable": 100, "median": "2.0000"}},
            "inputs": [],
            "warnings": [],
        }
        self.shortlist: list = list(impostazioni.get("shortlist") or [{"gestionale_source_row": 2, "supplier": "betulla"}])
        self.rapporto: dict = impostazioni.get("rapporto") or {
            "casi_ricevuti": 1,
            "casi_valutabili": 1,
            "casi_decisi": 1,
            "chiamate": 1,
            "costo_usd": 0.0002,
            "model": "openai/gpt-5.6-luna",
            "versione_prompt": "v3",
            "versione_avversario": "v1",
            "degradato": False,
        }
        self.decisioni_ai: list = list(impostazioni.get("decisioni_ai") or [{
            "gestionale_source_row": 2,
            "supplier": "betulla",
            "action": "ACCEPT",
            "ai_modello": "openai/gpt-5.6-luna",
            "ai_versione_prompt": "v3",
            "ai_versione_avversario": "v1",
        }])
        self.riepilogo_merge: dict = impostazioni.get("riepilogo_merge") or {
            "products": 3,
            "decisioni_con_riscontro": 1,
            "coppie_senza_shortlist": 0,
        }
        self.confronto: dict = impostazioni.get("confronto") or {
            "run": {"id": "nuova", "status": "ready"},
            "files": [],
            "suppliers": [{"id": "betulla", "name": "BETULLA"}],
            "products": [
                {"id": "product:1", "name": "Uno", "offers": [{"supplierId": "betulla", "available": True}]},
                {"id": "product:2", "name": "Due", "offers": [{"supplierId": "betulla", "available": True}]},
            ],
            "warnings": [],
        }
        self.validazione: dict = impostazioni.get("validazione") or {"errors": [], "warnings": []}
        self.chiamati: list[str] = []
        self.tetti: dict[str, float | None] = {}
        self.argomenti: dict[str, list[str]] = {}

    @staticmethod
    def _valore(argomenti: list[str], chiave: str) -> str:
        return argomenti[argomenti.index(chiave) + 1]

    def _scrivi(self, percorso: Path, valore) -> None:
        percorso.parent.mkdir(parents=True, exist_ok=True)
        percorso.write_bytes((json.dumps(valore, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    def __call__(self, comando, cartella, avanzamento, *, timeout_secondi=None) -> RisultatoComando:
        argomenti = list(comando)
        script = Path(argomenti[1]).name
        self.chiamati.append(script)
        # The per-step timeout: unused by this fake executor, but recording
        # it lets a test catch the orchestrator silently dropping it.
        self.tetti[script] = timeout_secondi
        # The real arguments passed to each step: `chiamati` only records
        # that a command ran, not what it was called with, so an argument
        # the orchestrator forgets to pass would otherwise go unnoticed.
        self.argomenti[script] = argomenti
        riepilogo: dict = {}
        se_scrive = script not in self.salta
        if script == "inspect_sources.py" and se_scrive:
            self._scrivi(Path(self._valore(argomenti, "--output")), {
                "profiles": self.profili,
                "errors": self.errori_profilazione,
            })
        elif script == "apply_preflight_decisions.py" and se_scrive:
            decisioni = json.loads(Path(self._valore(argomenti, "--decisions")).read_bytes())
            self._scrivi(Path(self._valore(argomenti, "--output")), {
                "files": decisioni["decisions"],
                "postcheck": {"status": "PENDING"},
            })
        elif script == "validate_input_manifest.py" and se_scrive:
            self._scrivi(Path(self._valore(argomenti, "--output")), self.validazione)
        elif script == "prepare_manifest_sources.py" and se_scrive:
            uscita = Path(self._valore(argomenti, "--output"))
            self._scrivi(uscita / "normalized_sources.json", {"gestionale": []})
            self._scrivi(uscita / "display_offers.json", {})
            self._scrivi(uscita / "matching_result.json", [])
            self._scrivi(uscita / "semantic_queue.json", [])
            self._scrivi(uscita / "audit.json", self.audit)
        elif script == "build_semantic_shortlists.py" and se_scrive:
            self._scrivi(Path(self._valore(argomenti, "--output")), self.shortlist)
        elif script == "valuta_shortlist.py" and se_scrive:
            avanzamento({"fatti": 1, "totali": 1, "chiamate": 1, "costo_usd": 0.0002})
            self._scrivi(Path(self._valore(argomenti, "--output")), self.decisioni_ai)
            self._scrivi(Path(self._valore(argomenti, "--rapporto")), self.rapporto)
            riepilogo = self.rapporto
        elif script == "merge_match_decisions.py" and se_scrive:
            self._scrivi(Path(self._valore(argomenti, "--output")), [])
            riepilogo = self.riepilogo_merge
        elif script == "build_review_data.py" and se_scrive:
            confronto = dict(self.confronto)
            confronto["run"] = {**confronto.get("run", {}), "id": self._valore(argomenti, "--run-id")}
            self._scrivi(Path(self._valore(argomenti, "--output")), confronto)
        elif script == "impara_adattatore.py" and se_scrive:
            manifest = json.loads(Path(self._valore(argomenti, "--manifest")).read_bytes())
            imparati = [
                {"file": voce.get("file_name"), "adapter_id": (voce.get("adapter_id") or "nuovo_v1")}
                for voce in (manifest.get("files") or [])
                if voce.get("state") in {"SCHEMA_VARIATO", "NUOVO_FORNITORE"}
                or (voce.get("ai_preflight") or {}).get("state") in {"SCHEMA_VARIATO", "NUOVO_FORNITORE"}
            ]
            self._scrivi(Path(self._valore(argomenti, "--output")), {"imparati": imparati, "saltati": []})
        return RisultatoComando(
            uscita=self.uscite.get(script, 0),
            stdout=json.dumps(riepilogo) if riepilogo else "",
            stderr="",
        )


class BancoPipeline(unittest.TestCase):
    def setUp(self) -> None:
        self.temporanea = tempfile.TemporaryDirectory()
        self.radice = Path(self.temporanea.name)
        self.addCleanup(self.temporanea.cleanup)
        self.dati = self.radice / "current"
        self.uploads = self.dati / "uploads"
        self.uploads.mkdir(parents=True)
        (self.uploads / "gestionale.xlsx").write_bytes(b"finto")
        (self.uploads / "betulla.xlsx").write_bytes(b"finto")
        self.review = self.dati / "review_data.json"
        self.stato = self.dati / "state.json"
        self._scrivi(self.review, CONFRONTO_PRECEDENTE)
        self._scrivi(self.stato, {
            "schemaVersion": 1,
            "runId": "vecchia",
            "products": [{"id": "product:1", "quantity": 4, "selectedSupplierId": "betulla"}],
        })

    @staticmethod
    def _scrivi(percorso: Path, valore) -> None:
        percorso.parent.mkdir(parents=True, exist_ok=True)
        percorso.write_bytes((json.dumps(valore, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    def configurazione(self) -> ConfigurazionePipeline:
        return ConfigurazionePipeline(
            data_dir=self.dati,
            uploads_dir=self.uploads,
            review_path=self.review,
            state_path=self.stato,
        )

    def gestore(self, esecutore: EsecutoreFinto, **extra) -> PipelineJobManager:
        return PipelineJobManager(self.configurazione(), esecutore=esecutore, **extra)

    def esegui(self, esecutore: EsecutoreFinto, **extra) -> dict:
        gestore = self.gestore(esecutore, **extra)
        gestore.avvia()
        return gestore.attendi(timeout=30)


class RunCompleta(BancoPipeline):
    def test_un_cambio_dei_documenti_scarta_la_run_ferma_e_la_sua_anteprima(self) -> None:
        stato_vecchio = {
            "ok": False,
            "stato": pipeline_jobs.ERRORE,
            "runId": "run-con-betulla",
            "cartella": str(self.dati / "esecuzioni" / "run-con-betulla"),
            "fermata": {"code": "SCHEMA_SCONOSCIUTO", "documenti": ["betulla.xlsx"]},
            "fasi": [],
        }
        self._scrivi(self.dati / pipeline_jobs.NOME_STATO, stato_vecchio)
        gestore = self.gestore(EsecutoreFinto())

        esito = gestore.input_modificato()

        self.assertEqual(esito["stato"], pipeline_jobs.IN_ATTESA)
        self.assertIsNone(esito["runId"])
        self.assertIsNone(esito["fermata"])
        salvato = json.loads((self.dati / pipeline_jobs.NOME_STATO).read_text(encoding="utf-8"))
        self.assertEqual(salvato["stato"], pipeline_jobs.IN_ATTESA)

    def test_al_riavvio_un_file_gia_eliminato_non_riappare_nell_anteprima(self) -> None:
        self._scrivi(self.dati / pipeline_jobs.NOME_STATO, {
            "ok": False,
            "stato": pipeline_jobs.ERRORE,
            "runId": "run-con-file-eliminato",
            "fermata": {"code": "SCHEMA_SCONOSCIUTO", "documenti": ["eliminato.xlsx"]},
        })

        gestore = self.gestore(EsecutoreFinto())

        self.assertEqual(gestore.stato()["stato"], pipeline_jobs.IN_ATTESA)
        self.assertIsNone(gestore.stato()["fermata"])

    def test_la_run_buona_attiva_il_confronto_nuovo(self) -> None:
        esito = self.esegui(EsecutoreFinto())
        self.assertEqual(esito["stato"], pipeline_jobs.COMPLETATO, esito.get("messaggio"))
        confronto = json.loads(self.review.read_bytes())
        self.assertEqual([p["id"] for p in confronto["products"]], ["product:1", "product:2"])
        self.assertEqual(len(esito["fasi"]), len(pipeline_jobs.FASI))
        self.assertTrue(all(fase["stato"] == pipeline_jobs.COMPLETATO for fase in esito["fasi"]))

    def test_esegue_gli_otto_comandi_nell_ordine(self) -> None:
        # There are nine phases: activation isn't a command, it's the one
        # step the orchestrator does itself, since it's the one that
        # touches the live comparison.
        esecutore = EsecutoreFinto()
        self.esegui(esecutore)
        self.assertEqual(esecutore.chiamati, [
            "inspect_sources.py",
            "apply_preflight_decisions.py",
            "validate_input_manifest.py",
            "prepare_manifest_sources.py",
            "build_semantic_shortlists.py",
            "valuta_shortlist.py",
            "merge_match_decisions.py",
            "build_review_data.py",
        ])

    def test_la_run_lascia_la_sua_cartella_datata_con_l_audit(self) -> None:
        esito = self.esegui(EsecutoreFinto())
        cartella = Path(esito["cartella"])
        self.assertTrue(cartella.is_dir())
        audit = json.loads((cartella / pipeline_jobs.NOME_AUDIT_ESECUZIONE).read_bytes())
        self.assertEqual(audit["stato"], pipeline_jobs.COMPLETATO)
        # The artifact hash registry: it's what makes "I wrote it" a
        # verifiable fact instead of a claim.
        self.assertIn("dati/audit.json", audit["artefatti"])
        self.assertEqual(len(audit["artefatti"]["dati/audit.json"]["sha256"]), 64)

    def test_l_avanzamento_della_fase_ai_arriva_nei_numeri(self) -> None:
        esito = self.esegui(EsecutoreFinto())
        self.assertEqual(esito["numeri"]["casiValutati"], 1)
        self.assertEqual(esito["numeri"]["chiamateAi"], 1)

    def test_il_riconoscimento_genera_le_decisioni_senza_chiedere_niente(self) -> None:
        esito = self.esegui(EsecutoreFinto())
        decisioni = json.loads((Path(esito["cartella"]) / "preflight_decisions.json").read_bytes())
        per_nome = {voce["file_name"]: voce for voce in decisioni["decisions"]}
        self.assertEqual(per_nome["gestionale.xlsx"]["role"], "master")
        self.assertEqual(per_nome["betulla.xlsx"]["supplier_id"], "betulla")
        self.assertTrue(all(v["state"] == "SCHEMA_NOTO" for v in decisioni["decisions"]))

    def test_la_mappatura_del_registro_entra_nella_decisione(self) -> None:
        """Without this, order compilation would report a missing column mapping.

        The auto-generated decision for a known adapter must carry
        everything a hand-written one would, including the order column,
        for the schema-recognition stop to actually resolve itself.
        """

        esecutore = EsecutoreFinto(profili=[
            profilo("gestionale.xlsx", "gestionale_v1"),
            profilo("cipresso.xlsx", "cipresso_v1"),
        ])
        esito = self.esegui(esecutore)
        decisioni = json.loads((Path(esito["cartella"]) / "preflight_decisions.json").read_bytes())
        cipresso = next(v for v in decisioni["decisions"] if v["file_name"] == "cipresso.xlsx")
        self.assertEqual(cipresso["field_mapping"]["order_column"], "G")


class IlConfrontoPrecedenteRestaIntatto(BancoPipeline):
    """The button's promise: a failed run never costs the live comparison."""

    def _prova_fase(self, script: str) -> dict:
        esito = self.esegui(EsecutoreFinto(uscite={script: 9}))
        self.assertEqual(esito["stato"], pipeline_jobs.ERRORE)
        self.assertEqual(json.loads(self.review.read_bytes()), CONFRONTO_PRECEDENTE)
        return esito

    def test_ogni_fase_che_fallisce_lascia_il_confronto_dov_era(self) -> None:
        for script in (
            "inspect_sources.py",
            "apply_preflight_decisions.py",
            "validate_input_manifest.py",
            "prepare_manifest_sources.py",
            "build_semantic_shortlists.py",
            "valuta_shortlist.py",
            "merge_match_decisions.py",
            "build_review_data.py",
        ):
            with self.subTest(script=script):
                self.setUp()
                self._prova_fase(script)

    def test_un_artefatto_promesso_e_non_scritto_ferma_la_catena(self) -> None:
        esito = self.esegui(EsecutoreFinto(salta={"prepare_manifest_sources.py"}))
        self.assertEqual(esito["stato"], pipeline_jobs.ERRORE)
        self.assertEqual(esito["fermata"]["code"], "ARTEFATTO_MANCANTE")
        self.assertEqual(json.loads(self.review.read_bytes()), CONFRONTO_PRECEDENTE)

    def test_lo_stato_non_viene_toccato_da_una_run_fallita(self) -> None:
        prima = self.stato.read_bytes()
        self.esegui(EsecutoreFinto(uscite={"build_review_data.py": 1}))
        self.assertEqual(self.stato.read_bytes(), prima)


class LeDueFermate(BancoPipeline):
    def test_uno_schema_che_il_registro_non_conosce_ferma_e_dice_quale(self) -> None:
        esecutore = EsecutoreFinto(profili=[
            profilo("gestionale.xlsx", "gestionale_v1"),
            profilo("misterioso.xlsx", "", stato="AMBIGUO"),
        ])
        esito = self.esegui(esecutore)
        self.assertEqual(esito["fermata"]["code"], "SCHEMA_SCONOSCIUTO")
        self.assertEqual(esito["fermata"]["documenti"], ["misterioso.xlsx"])
        self.assertIn("anteprima qui sotto", esito["fermata"]["message"])
        self.assertNotIn("decisioni_schemi.json", esito["fermata"]["message"])
        self.assertNotIn("impara_adattatore", esito["fermata"]["message"])

    def test_una_decisione_scritta_a_mano_toglie_la_fermata(self) -> None:
        self._scrivi(self.dati / pipeline_jobs.NOME_DECISIONI_MANUALI, {"decisions": [{
            "file_name": "misterioso.xlsx",
            "state": "FILE_NON_PERTINENTE",
            "role": None,
            "user_confirmation": {"required": False, "status": "NOT_REQUIRED"},
        }]})
        esecutore = EsecutoreFinto(profili=[
            profilo("gestionale.xlsx", "gestionale_v1"),
            profilo("betulla.xlsx", "betulla_v1"),
            profilo("misterioso.xlsx", "", stato="AMBIGUO"),
        ])
        esito = self.esegui(esecutore)
        self.assertEqual(esito["stato"], pipeline_jobs.COMPLETATO, esito.get("messaggio"))

    def _decisione_confermata(self, *, impronta: str | None) -> None:
        """A mapping confirmed on the page, optionally tied to a document's hash."""

        voce = {
            "file_name": "misterioso.xlsx",
            "profile_id": "misterioso-xlsx",
            "state": "FILE_NON_PERTINENTE",
            "role": None,
            "user_confirmation": {"required": False, "status": "NOT_REQUIRED"},
        }
        if impronta is not None:
            voce["file_sha256"] = impronta
        self._scrivi(self.dati / pipeline_jobs.NOME_DECISIONI_MANUALI, {"decisions": [voce]})

    def _con_misterioso(self) -> "EsecutoreFinto":
        return EsecutoreFinto(profili=[
            profilo("gestionale.xlsx", "gestionale_v1"),
            profilo("betulla.xlsx", "betulla_v1"),
            profilo("misterioso.xlsx", "", stato="AMBIGUO"),
        ])

    def test_la_decisione_vale_finche_il_documento_e_lo_stesso(self) -> None:
        # `profilo()` builds the hash "000...0": that's the one confirmed.
        self._decisione_confermata(impronta="0" * 64)

        esito = self.esegui(self._con_misterioso())

        self.assertEqual(esito["stato"], pipeline_jobs.COMPLETATO, esito.get("messaggio"))
        self.assertNotIn("DECISIONE_MANUALE_SCARTATA", {avviso["code"] for avviso in esito["avvisi"]})

    def test_un_altro_documento_con_lo_stesso_nome_non_eredita_le_colonne(self) -> None:
        """Real-world case: a misconfigured price list gets deleted and re-uploaded.

        The new file has the same name as the old one. Reapplying the old
        column mapping to it means reading the wrong columns — writing
        quantities on the wrong rows — with nothing to say so.
        """

        self._decisione_confermata(impronta="a" * 64)

        esito = self.esegui(self._con_misterioso())

        self.assertIn("DECISIONE_MANUALE_SCARTATA", {avviso["code"] for avviso in esito["avvisi"]})
        avviso = next(
            voce for voce in esito["avvisi"] if voce["code"] == "DECISIONE_MANUALE_SCARTATA"
        )
        self.assertIn("misterioso.xlsx", avviso["title"])
        self.assertIn("un altro documento con lo stesso nome", avviso["message"])
        # The decision stops covering the document: the pipeline halts
        # to ask for the columns instead of guessing them.
        self.assertEqual(esito["fermata"]["code"], "SCHEMA_SCONOSCIUTO")
        self.assertEqual(esito["fermata"]["documenti"], ["misterioso.xlsx"])

    def test_una_decisione_scritta_a_mano_senza_impronta_continua_a_valere(self) -> None:
        """The documented escape hatch: removing it would remove the remedy."""

        self._decisione_confermata(impronta=None)

        esito = self.esegui(self._con_misterioso())

        self.assertEqual(esito["stato"], pipeline_jobs.COMPLETATO, esito.get("messaggio"))
        self.assertNotIn("DECISIONE_MANUALE_SCARTATA", {avviso["code"] for avviso in esito["avvisi"]})

    def test_un_documento_che_non_si_legge_ferma_e_dice_quale(self) -> None:
        esecutore = EsecutoreFinto(errori_profilazione=[
            {"path": "C:/uploads/rovinato.xlsx", "error": "BadZipFile: rotto"}
        ])
        esito = self.esegui(esecutore)
        self.assertEqual(esito["fermata"]["code"], "DOCUMENTO_NON_LETTO")
        self.assertIn("rovinato.xlsx", esito["fermata"]["documenti"])

    def test_senza_documenti_caricati_lo_dice_invece_di_provarci(self) -> None:
        for documento in self.uploads.glob("*"):
            documento.unlink()
        esito = self.esegui(EsecutoreFinto())
        self.assertEqual(esito["fermata"]["code"], "NESSUN_DOCUMENTO")

    def test_un_manifest_bocciato_ferma_la_catena(self) -> None:
        """`prepare_manifest_sources` would run even on a rejected manifest.

        It never reads `manifest_validation.json` itself: without an
        upstream stop, two suppliers with the same id or a missing master
        turn into a wrong comparison instead of an error message.
        """

        esecutore = EsecutoreFinto(validazione={"errors": ["Due master nel manifest"], "warnings": []})
        esito = self.esegui(esecutore)
        self.assertEqual(esito["fermata"]["code"], "MANIFEST_NON_VALIDO")
        self.assertNotIn("prepare_manifest_sources.py", esecutore.chiamati)

    def test_un_fornitore_letto_senza_righe_ferma_la_catena(self) -> None:
        esecutore = EsecutoreFinto(audit={
            "master": {"rows": 3},
            "sources": {"betulla": {"rows": 0}},
            "inputs": [],
        })
        esito = self.esegui(esecutore)
        self.assertEqual(esito["fermata"]["code"], "FORNITORE_SENZA_RIGHE")

    def test_la_fermata_senza_righe_indica_il_rimedio(self) -> None:
        """Like the pipeline's other stop conditions, the message must carry the remedy, not just the diagnosis.

        A supplier the user deliberately uploaded can't silently drop out of
        the comparison with a mere warning — orders would go out without
        them.
        """

        esecutore = EsecutoreFinto(audit={
            "master": {"rows": 3},
            "sources": {"betulla": {"rows": 0}},
            "inputs": [],
        })
        esito = self.esegui(esecutore)
        messaggio = esito["fermata"]["message"]
        self.assertIn("confronto precedente è rimasto attivo", messaggio)
        self.assertIn("rifai il confronto", messaggio)
        self.assertIn("references/schema-routing.md", messaggio)


class LePrecondizioniDellAttivazione(BancoPipeline):
    def test_zero_prodotti_non_attivano_niente(self) -> None:
        esecutore = EsecutoreFinto(confronto={
            "run": {"id": "nuova"}, "files": [], "suppliers": [{"id": "betulla"}], "products": [], "warnings": [],
        })
        esito = self.esegui(esecutore)
        self.assertEqual(esito["fermata"]["code"], "ATTIVAZIONE_RIFIUTATA")
        self.assertEqual(json.loads(self.review.read_bytes()), CONFRONTO_PRECEDENTE)

    def test_zero_fornitori_non_attivano_niente(self) -> None:
        esecutore = EsecutoreFinto(confronto={
            "run": {"id": "nuova"}, "files": [], "suppliers": [],
            "products": [{"id": "product:1", "offers": []}], "warnings": [],
        })
        esito = self.esegui(esecutore)
        self.assertEqual(esito["fermata"]["code"], "ATTIVAZIONE_RIFIUTATA")

    def test_i_casi_ricevuti_devono_tornare_con_i_candidati_prodotti(self) -> None:
        """The AI step's received-case count must match the shortlist it was given.

        If the shortlist has a thousand candidates and the AI step reports
        receiving five hundred, five hundred products silently dropped out
        of evaluation while every individual step still exits cleanly. This
        check is what catches it.
        """

        esecutore = EsecutoreFinto(
            shortlist=[{"gestionale_source_row": 2}, {"gestionale_source_row": 3}],
            rapporto={
                "casi_ricevuti": 1, "casi_valutabili": 1, "casi_decisi": 1,
                "chiamate": 1, "costo_usd": 0.0, "model": "m", "versione_prompt": "v3",
                "versione_avversario": "v1", "degradato": False,
            },
        )
        esito = self.esegui(esecutore)
        self.assertEqual(esito["fermata"]["code"], "ATTIVAZIONE_RIFIUTATA")
        self.assertIn("casi ricevuti", esito["fermata"]["message"])

    def test_le_coppie_senza_shortlist_non_attivano_niente(self) -> None:
        esecutore = EsecutoreFinto(riepilogo_merge={
            "products": 3, "decisioni_con_riscontro": 1, "coppie_senza_shortlist": 448,
        })
        esito = self.esegui(esecutore)
        self.assertEqual(esito["fermata"]["code"], "ATTIVAZIONE_RIFIUTATA")


class IlDegradoNonFerma(BancoPipeline):
    def test_la_fase_ai_degradata_avvisa_e_la_run_arriva_in_fondo(self) -> None:
        esecutore = EsecutoreFinto(
            uscite={"valuta_shortlist.py": 5},
            rapporto={
                "casi_ricevuti": 1, "casi_valutabili": 1, "casi_decisi": 0,
                "chiamate": 0, "costo_usd": 0.0, "model": "m", "versione_prompt": "v3",
                "versione_avversario": "v1", "degradato": True,
                "motivo_degrado": "chiave assente",
            },
        )
        esito = self.esegui(esecutore)
        self.assertEqual(esito["stato"], pipeline_jobs.COMPLETATO, esito.get("messaggio"))
        codici = {avviso["code"] for avviso in esito["avvisi"]}
        self.assertIn("FASE_AI_DEGRADATA", codici)

    def test_le_decisioni_scartate_avvisano_e_non_fermano(self) -> None:
        esecutore = EsecutoreFinto(
            uscite={"merge_match_decisions.py": 4},
            riepilogo_merge={
                "products": 3, "decisioni_con_riscontro": 0, "coppie_senza_shortlist": 0,
                "decisioni_scartate_per_disallineamento": 2,
                "decisioni_scartate_per_riga_inventata": 0,
                "decisioni_scartate_per_ean_non_rispettato": 0,
            },
        )
        esito = self.esegui(esecutore)
        self.assertEqual(esito["stato"], pipeline_jobs.COMPLETATO, esito.get("messaggio"))
        self.assertIn("DECISIONI_AI_SCARTATE", {avviso["code"] for avviso in esito["avvisi"]})

    def test_le_decisioni_non_riconciliate_invece_fermano(self) -> None:
        esito = self.esegui(EsecutoreFinto(uscite={"merge_match_decisions.py": 3}))
        self.assertEqual(esito["stato"], pipeline_jobs.ERRORE)

    def test_gli_avvisi_finiscono_nel_confronto_e_non_solo_nel_job(self) -> None:
        """A warning that lives only in the job's transient state won't be
        seen once the page is reopened later."""

        esecutore = EsecutoreFinto(
            uscite={"valuta_shortlist.py": 5},
            rapporto={
                "casi_ricevuti": 1, "casi_valutabili": 1, "casi_decisi": 0,
                "chiamate": 0, "costo_usd": 0.0, "model": "m", "versione_prompt": "v3",
                "versione_avversario": "v1", "degradato": True, "motivo_degrado": "rete giu'",
            },
        )
        self.esegui(esecutore)
        confronto = json.loads(self.review.read_bytes())
        self.assertIn("FASE_AI_DEGRADATA", {avviso["code"] for avviso in confronto["warnings"]})


class UnListinoSenzaPrezziSiDice(BancoPipeline):
    """A supplier with all prices at zero must produce a warning, not silence.

    A price column pointed at the wrong header can produce 242 products at
    €0.00 with CERTAIN confidence and a €0.00 order plan, while
    `dati/audit.json` already reports `usable: 0` for that supplier. Without
    this check nothing warns: comparing only against the previous run means
    the very first run checks nothing, and a missing median would fall
    through `if not prima or adesso is None`.

    A supplier at zero doesn't drop out of the comparison: it wins, on
    every row, because it's the cheapest.
    """

    @staticmethod
    def _audit(**prezzi: object) -> dict:
        return {
            "master": {"rows": 3},
            "sources": {"betulla": {"rows": 4660}},
            "price_summary": {"betulla": dict(prezzi)},
            "inputs": [],
        }

    def test_alla_prima_run_un_listino_con_righe_e_senza_prezzi_avvisa(self) -> None:
        esito = self.esegui(EsecutoreFinto(audit=self._audit(usable=0, median=None)))

        self.assertEqual(esito["stato"], pipeline_jobs.COMPLETATO, esito.get("messaggio"))
        avvisi = [voce for voce in esito["avvisi"] if voce["code"] == "PREZZI_A_ZERO"]
        self.assertEqual(len(avvisi), 1, esito["avvisi"])
        self.assertIn("BETULLA", avvisi[0]["title"])
        self.assertIn("4660", avvisi[0]["message"])
        self.assertEqual(esito["numeri"]["fornitoriSenzaPrezzi"], ["betulla"])

    def test_una_mediana_zero_vale_come_una_mediana_assente(self) -> None:
        esito = self.esegui(EsecutoreFinto(audit=self._audit(usable=4660, median="0.0000")))

        self.assertIn("PREZZI_A_ZERO", {voce["code"] for voce in esito["avvisi"]})

    def test_l_avviso_e_rosso_e_non_ferma_la_compilazione(self) -> None:
        """It reads as an error, but never blocks compilation.

        `blocking: true` would stop compilation, and pipeline stops are
        reserved for input problems; `severity: "warning"` would make it
        easy to skim past among the others.
        """

        esito = self.esegui(EsecutoreFinto(audit=self._audit(usable=0, median=None)))

        avviso = next(voce for voce in esito["avvisi"] if voce["code"] == "PREZZI_A_ZERO")
        self.assertEqual(avviso["severity"], "error")
        self.assertIs(avviso["blocking"], False)

    def test_l_avviso_entra_anche_nel_documento(self) -> None:
        """A warning that lives only in the job's transient state won't be
        seen once the page is reopened later."""

        self.esegui(EsecutoreFinto(audit=self._audit(usable=0, median=None)))

        confronto = json.loads(self.review.read_bytes())
        self.assertIn("PREZZI_A_ZERO", {voce["code"] for voce in confronto["warnings"]})

    def test_anche_alla_seconda_run_con_le_stesse_righe(self) -> None:
        """Same row count as before, prices collapsed to zero: must still warn."""

        primo = self.esegui(EsecutoreFinto())
        self.assertEqual(primo["stato"], pipeline_jobs.COMPLETATO, primo.get("messaggio"))
        esito = self.esegui(EsecutoreFinto(audit={
            "master": {"rows": 3},
            "sources": {"betulla": {"rows": 100}},
            "price_summary": {"betulla": {"usable": 0, "median": None}},
            "inputs": [],
        }))

        self.assertIn("PREZZI_A_ZERO", {voce["code"] for voce in esito["avvisi"]})

    def test_un_listino_con_i_prezzi_non_avvisa(self) -> None:
        """A warning that fires on every run is a warning nobody reads anymore."""

        esito = self.esegui(EsecutoreFinto())

        self.assertNotIn("PREZZI_A_ZERO", {voce["code"] for voce in esito["avvisi"]})
        self.assertNotIn("fornitoriSenzaPrezzi", esito["numeri"])

    def test_un_audit_senza_riepilogo_prezzi_non_grida_al_lupo(self) -> None:
        """Not measured is not the same as measured zero.

        An older-format audit with no `price_summary` must not trigger
        PREZZI_A_ZERO for every supplier in the run: a red warning per
        supplier over something nobody measured would be noise that drowns
        out the real warning.
        """

        esito = self.esegui(EsecutoreFinto(audit={
            "master": {"rows": 3},
            "sources": {"betulla": {"rows": 4660}},
            "inputs": [],
        }))

        self.assertEqual(esito["stato"], pipeline_jobs.COMPLETATO, esito.get("messaggio"))
        codici = [voce["code"] for voce in esito["avvisi"]]
        self.assertNotIn("PREZZI_A_ZERO", codici)
        self.assertEqual(codici.count("PREZZI_NON_MISURATI"), 1, esito["avvisi"])
        self.assertNotIn("fornitoriSenzaPrezzi", esito["numeri"])

    def test_un_riepilogo_prezzi_null_vale_come_assente(self) -> None:
        esito = self.esegui(EsecutoreFinto(audit={
            "master": {"rows": 3},
            "sources": {"betulla": {"rows": 4660}},
            "price_summary": None,
            "inputs": [],
        }))

        codici = [voce["code"] for voce in esito["avvisi"]]
        self.assertNotIn("PREZZI_A_ZERO", codici)
        self.assertEqual(codici.count("PREZZI_NON_MISURATI"), 1, esito["avvisi"])

    def test_la_frase_non_promette_prezzi_maggiori_di_zero_con_la_mediana_a_zero(self) -> None:
        """A "4660 prices greater than zero" message and a zero median contradict each other."""

        esito = self.esegui(EsecutoreFinto(audit=self._audit(usable=4660, median="0.0000")))

        avviso = next(voce for voce in esito["avvisi"] if voce["code"] == "PREZZI_A_ZERO")
        self.assertNotIn("maggiori di zero", avviso["message"])
        self.assertIn("prezzo mediano dichiarato", avviso["message"])


class ChiNonSiPuoCompilareSiDice(BancoPipeline):
    """A supplier in the comparison that no order file will ever be written for must be named.

    Compilability is registry-driven rather than hardcoded in the
    launcher, specifically so a supplier outside it is named instead of
    silently disappearing with its assigned products left unwarned.
    """

    CONFRONTO_CON_ACERO = {
        "run": {"id": "nuova", "status": "ready"},
        "files": [],
        "suppliers": [{"id": "betulla", "name": "BETULLA"}, {"id": "acero", "name": "ACERO"}],
        "products": [
            {"id": "product:1", "name": "Uno", "offers": [{"supplierId": "acero", "available": True}]},
        ],
        "warnings": [],
    }

    def test_un_fornitore_senza_regola_di_scrittura_avvisa(self) -> None:
        esito = self.esegui(EsecutoreFinto(confronto=self.CONFRONTO_CON_ACERO))

        self.assertEqual(esito["stato"], pipeline_jobs.COMPLETATO, esito.get("messaggio"))
        avvisi = [voce for voce in esito["avvisi"] if voce["code"] == "FORNITORE_NON_COMPILABILE"]
        self.assertEqual([voce["fornitore"] for voce in avvisi], ["acero"])
        self.assertIn("ACERO", avvisi[0]["title"])
        self.assertEqual(esito["numeri"]["fornitoriSenzaCompilazione"], ["acero"])

    def test_i_fornitori_dichiarati_nel_registro_non_avvisano(self) -> None:
        esito = self.esegui(EsecutoreFinto())

        self.assertNotIn("FORNITORE_NON_COMPILABILE", {voce["code"] for voce in esito["avvisi"]})

    def test_la_regola_viene_dal_registro_e_non_dal_codice(self) -> None:
        """Declaring `order_write` in the registry silences the warning, no code change needed.

        Proves that compilability is data-driven: a supplier learned next
        week can become compilable without anyone touching the program.
        """

        registro_di_prova = self.radice / "adapters.json"
        documento = json.loads(
            (Path(pipeline_jobs.REFERENCES_DIR) / "adapters.json").read_text(encoding="utf-8"))
        documento["adapters"].append({
            "id": "acero_v1", "schema_version": 1, "kind": "supplier",
            "supplier_id": "acero", "display_name": "ACERO",
            "header_signature": {"kind": "headers", "required": ["codean"], "known": ["codean"]},
            "order_write": {"sheet": "FIRST", "data_start_row": 7, "order_column": "B"},
        })
        registro_di_prova.write_bytes(
            (json.dumps(documento, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

        gestore = pipeline_jobs.PipelineJobManager(
            pipeline_jobs.ConfigurazionePipeline(
                data_dir=self.dati, uploads_dir=self.uploads, review_path=self.review,
                state_path=self.stato, adapters_path=registro_di_prova,
            ),
            esecutore=EsecutoreFinto(confronto=self.CONFRONTO_CON_ACERO),
        )
        gestore.avvia()
        esito = gestore.attendi(timeout=30)

        self.assertEqual(esito["stato"], pipeline_jobs.COMPLETATO, esito.get("messaggio"))
        self.assertNotIn("FORNITORE_NON_COMPILABILE", {voce["code"] for voce in esito["avvisi"]})


class LeVociImparateSuperateSiMettonoDaParteAllInizio(BancoPipeline):
    """The shipped adapter registry always wins over a locally learned one with the same id.

    The pipeline applies this rule to the registry file before profiling
    any document: it's during profiling that `registro.riconosci` picks the
    adapter, and a stale learned entry would claim the price list instead
    of the shipped one. The warning fires once, since the entry is removed
    from the file.
    """

    def test_la_voce_superata_esce_dal_file_e_l_utente_lo_sa(self) -> None:
        registro_di_prova = self.radice / "adapters.json"
        documento = json.loads(
            (Path(pipeline_jobs.REFERENCES_DIR) / "adapters.json").read_text(encoding="utf-8"))
        self._scrivi(registro_di_prova, documento)
        imparato = registro_di_prova.with_name("adattatori_imparati.json")
        self._scrivi(imparato, {"schema_version": 1, "adapters": [
            # No shipped entry with the same id covers this one: a locally
            # learned adapter for BETULLA.
            {"id": "betulla_v1__locale", "supplier_id": "betulla", "display_name": "BETULLA",
             "kind": "supplier", "order_write": {"sheet": "FIRST", "data_start_row": 2,
                                                 "order_column": "H"}},
            # Learned from scratch, no shipped counterpart: untouched.
            {"id": "acero_v1", "supplier_id": "acero", "display_name": "ACERO",
             "kind": "supplier"},
        ]})
        gestore = pipeline_jobs.PipelineJobManager(
            pipeline_jobs.ConfigurazionePipeline(
                data_dir=self.dati, uploads_dir=self.uploads, review_path=self.review,
                state_path=self.stato, adapters_path=registro_di_prova,
            ),
            esecutore=EsecutoreFinto(),
        )

        gestore._metti_da_parte_gli_adattatori_superati()

        avvisi = [voce for voce in gestore.stato().get("avvisi") or []
                  if voce.get("code") == "ADATTATORE_MESSO_DA_PARTE"]
        self.assertEqual([voce["adattatore"] for voce in avvisi], ["betulla_v1__locale"])
        self.assertIn("BETULLA", avvisi[0]["title"])
        self.assertIn("vale quella", avvisi[0]["message"])
        dopo = json.loads(imparato.read_text(encoding="utf-8"))
        self.assertEqual([voce["id"] for voce in dopo["adapters"]], ["acero_v1"])
        self.assertEqual([voce["id"] for voce in dopo["adapters_messi_da_parte"]], ["betulla_v1__locale"])
        # Second call: nothing left to move, nothing to warn about.
        gestore._metti_da_parte_gli_adattatori_superati()
        self.assertEqual(
            len([v for v in gestore.stato().get("avvisi") or [] if v.get("code") == "ADATTATORE_MESSO_DA_PARTE"]),
            1,
        )


class LaCausaDellaMancataCopiaSiDiceInCatena(BancoPipeline):
    """A supplier the registry declares can still end up with no order copy.

    The order column's header changed, the sheet was renamed, or the
    declared rows fall outside the document — that whole family of causes
    must not die inside `prepare_writer_config`'s message, which the server
    discards once writing succeeds: without a warning surfaced earlier, a
    supplier with a changed header would silently drop out of the
    configuration, discovered only at compilation time, with a message
    that never named the cause.
    """

    def _registro_con_acero(self) -> Path:
        registro_di_prova = self.radice / "adapters.json"
        documento = json.loads(
            (Path(pipeline_jobs.REFERENCES_DIR) / "adapters.json").read_text(encoding="utf-8"))
        documento["adapters"].append({
            "id": "acero_v1", "schema_version": 1, "kind": "supplier",
            "supplier_id": "acero", "display_name": "ACERO",
            "header_signature": {"kind": "headers", "required": ["codean"], "known": ["codean"]},
            "order_write": {"sheet": "FIRST", "header_row": 1, "data_start_row": 2,
                            "order_column": "C", "expected_header": "ORDINE"},
        })
        registro_di_prova.write_bytes(
            (json.dumps(documento, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        return registro_di_prova

    def _listino(self, intestazione_ordine: str) -> Path:
        from openpyxl import Workbook

        percorso = self.radice / f"acero_{intestazione_ordine.casefold()}.xlsx"
        libro = Workbook()
        foglio = libro.active
        foglio.title = "Listino"
        foglio.append(["COD.EAN", "DESCRIZIONE", intestazione_ordine])
        foglio.append(["8000000000001", "PRODOTTO", None])
        libro.save(percorso)
        libro.close()
        return percorso

    def _confronto(self, listino: Path) -> dict:
        return {
            "run": {"id": "nuova", "status": "ready"},
            "files": [{"supplierId": "acero", "adapterId": "acero_v1",
                       "role": "supplier", "sourcePath": str(listino)}],
            "suppliers": [{"id": "acero", "name": "ACERO"}],
            "products": [
                {"id": "product:1", "name": "Uno",
                 "offers": [{"supplierId": "acero", "available": True}]},
            ],
            "warnings": [],
        }

    def _esegui_con(self, registro_di_prova: Path, confronto: dict) -> dict:
        gestore = pipeline_jobs.PipelineJobManager(
            pipeline_jobs.ConfigurazionePipeline(
                data_dir=self.dati, uploads_dir=self.uploads, review_path=self.review,
                state_path=self.stato, adapters_path=registro_di_prova,
            ),
            esecutore=EsecutoreFinto(confronto=confronto),
        )
        gestore.avvia()
        return gestore.attendi(timeout=30)

    def test_l_intestazione_cambiata_si_dice_gia_in_costruzione(self) -> None:
        esito = self._esegui_con(self._registro_con_acero(),
                                 self._confronto(self._listino("ORDINI")))

        self.assertEqual(esito["stato"], pipeline_jobs.COMPLETATO, esito.get("messaggio"))
        avvisi = [voce for voce in esito["avvisi"] if voce["code"] == "FORNITORE_NON_COMPILABILE"]
        self.assertEqual([voce["fornitore"] for voce in avvisi], ["acero"], esito["avvisi"])
        self.assertIn("non contiene ORDINE", avvisi[0]["message"])
        self.assertIn("nessuna copia", avvisi[0]["message"])
        self.assertEqual(esito["numeri"]["fornitoriSenzaCompilazione"], ["acero"])

    def test_l_avviso_entra_anche_nel_documento(self) -> None:
        """A warning that lives only in the job's transient state won't be
        seen once the page is reopened later."""

        self._esegui_con(self._registro_con_acero(),
                         self._confronto(self._listino("ORDINI")))

        confronto = json.loads(self.review.read_bytes())
        avvisi = [voce for voce in confronto["warnings"]
                  if voce["code"] == "FORNITORE_NON_COMPILABILE"]
        self.assertTrue(avvisi and "non contiene ORDINE" in avvisi[0]["message"], avvisi)

    def test_col_documento_giusto_nessun_avviso(self) -> None:
        esito = self._esegui_con(self._registro_con_acero(),
                                 self._confronto(self._listino("ORDINE")))

        self.assertEqual(esito["stato"], pipeline_jobs.COMPLETATO, esito.get("messaggio"))
        self.assertNotIn("FORNITORE_NON_COMPILABILE", {voce["code"] for voce in esito["avvisi"]})

    def test_un_registro_rotto_lo_dice_una_volta_e_con_la_causa_giusta(self) -> None:
        """A broken registry JSON file must not be misdiagnosed as "the registry doesn't declare a rule"."""

        registro_rotto = self.radice / "adapters.json"
        registro_rotto.write_text("{ rotto", encoding="utf-8")

        esito = self._esegui_con(registro_rotto, self._confronto(self._listino("ORDINE")))

        self.assertEqual(esito["stato"], pipeline_jobs.COMPLETATO, esito.get("messaggio"))
        codici = [voce["code"] for voce in esito["avvisi"]]
        self.assertEqual(codici.count("REGISTRO_ILLEGGIBILE"), 1, esito["avvisi"])
        self.assertNotIn("FORNITORE_NON_COMPILABILE", codici)


class LaFermataApreLaConfigurazioneGuidata(BancoPipeline):
    """A new/unknown schema gets resolved from the page, without editing files or running commands."""

    def _fermata(self) -> dict:
        esecutore = EsecutoreFinto(profili=[
            profilo("gestionale.xlsx", "gestionale_v1"),
            profilo("sconosciuto.xlsx", None, stato="AMBIGUO"),
        ])
        esito = self.esegui(esecutore)
        self.assertEqual(esito["stato"], pipeline_jobs.ERRORE)
        return esito

    def test_il_manifest_non_esiste_ancora_ma_il_messaggio_non_chiede_json(self) -> None:
        esito = self._fermata()
        cartella = Path(esito["cartella"])

        self.assertFalse((cartella / "input_manifest.json").is_file())
        messaggio = esito["fermata"]["message"]
        self.assertIn("anteprima qui sotto", messaggio)
        self.assertIn("riparte da solo", messaggio)
        self.assertNotIn(pipeline_jobs.NOME_DECISIONI_MANUALI, messaggio)
        self.assertNotIn("impara_adattatore", messaggio)

    def test_lo_schema_variato_usa_lo_stesso_flusso_breve(self) -> None:
        esito = self._fermata()
        self.assertNotIn("references/", esito["fermata"]["message"])

    def test_una_mappatura_confermata_viene_imparata_e_il_ponte_sparisce(self) -> None:
        decisioni = self.dati / pipeline_jobs.NOME_DECISIONI_MANUALI
        self._scrivi(decisioni, {"decisions": [{
            "file_name": "misterioso.xlsx",
            "profile_id": "misterioso-xlsx",
            "state": "SCHEMA_VARIATO",
            "role": "supplier",
            "supplier_id": "betulla",
            "adapter_id": "betulla_v1",
            "rationale": "Confermato nella pagina Importa.",
            "field_mapping": {"sheet": "Foglio1", "header_row": 1, "data_start_row": 2,
                              "columns": {"description": "Nome", "unit_price_net": "Prezzo",
                                          "pieces_per_carton": "Pezzi"},
                              "ean_unavailable": True, "supplier_code_unavailable": True,
                              "assume_available": True, "vat_unavailable": True,
                              "order_column": "D"},
            "user_confirmation": {"required": True, "status": "CONFIRMED"},
        }]})
        esecutore = EsecutoreFinto(profili=[
            profilo("gestionale.xlsx", "gestionale_v1"),
            profilo("misterioso.xlsx", "", stato="AMBIGUO"),
        ])

        esito = self.esegui(esecutore)

        self.assertEqual(esito["stato"], pipeline_jobs.COMPLETATO, esito.get("messaggio"))
        self.assertIn("impara_adattatore.py", esecutore.chiamati)
        self.assertFalse(decisioni.exists())


class IlControlloConLaVoltaPrima(BancoPipeline):
    def _prima_run(self) -> None:
        esito = self.esegui(EsecutoreFinto())
        self.assertEqual(esito["stato"], pipeline_jobs.COMPLETATO)

    def test_la_prima_run_non_ha_niente_da_confrontare(self) -> None:
        esito = self.esegui(EsecutoreFinto())
        self.assertEqual(esito["numeri"]["confrontoConLaVoltaPrima"], "prima esecuzione")

    def test_un_listino_dimezzato_avvisa_e_la_run_arriva_in_fondo(self) -> None:
        self._prima_run()
        esecutore = EsecutoreFinto(audit={
            "master": {"rows": 3},
            "sources": {"betulla": {"rows": 40}},
            "price_summary": {"betulla": {"usable": 40, "median": "2.0000"}},
            "inputs": [],
        })
        esito = self.esegui(esecutore)
        self.assertEqual(esito["stato"], pipeline_jobs.COMPLETATO, esito.get("messaggio"))
        self.assertIn("RIGHE_CAMBIATE_MOLTO", {avviso["code"] for avviso in esito["avvisi"]})

    def test_i_prezzi_cambiati_in_blocco_avvisano(self) -> None:
        self._prima_run()
        esecutore = EsecutoreFinto(audit={
            "master": {"rows": 3},
            "sources": {"betulla": {"rows": 100}},
            "price_summary": {"betulla": {"usable": 100, "median": "3.0000"}},
            "inputs": [],
        })
        esito = self.esegui(esecutore)
        self.assertIn("PREZZI_CAMBIATI_IN_BLOCCO", {avviso["code"] for avviso in esito["avvisi"]})

    def test_un_fornitore_sparito_avvisa(self) -> None:
        self._prima_run()
        esecutore = EsecutoreFinto(
            profili=[profilo("gestionale.xlsx", "gestionale_v1"), profilo("larice.xlsx", "larice_v1")],
            audit={
                "master": {"rows": 3},
                "sources": {"larice": {"rows": 80}},
                "price_summary": {"larice": {"usable": 80, "median": "2.0000"}},
                "inputs": [],
            },
        )
        esito = self.esegui(esecutore)
        codici = {avviso["code"] for avviso in esito["avvisi"]}
        self.assertIn("FORNITORE_SPARITO", codici)
        self.assertIn("FORNITORE_NUOVO", codici)

    def test_un_prompt_diverso_dalla_volta_prima_avvisa(self) -> None:
        self._prima_run()
        esecutore = EsecutoreFinto(rapporto={
            "casi_ricevuti": 1, "casi_valutabili": 1, "casi_decisi": 1,
            "chiamate": 1, "costo_usd": 0.0, "model": "openai/gpt-5.6-luna",
            "versione_prompt": "v4", "versione_avversario": "v1", "degradato": False,
        })
        esito = self.esegui(esecutore)
        self.assertIn("CONFIGURAZIONE_AI_CAMBIATA", {avviso["code"] for avviso in esito["avvisi"]})


class LoStatoDopoIlRicalcolo(BancoPipeline):
    def test_le_scelte_su_un_prodotto_sparito_tornano_a_zero_e_si_dice(self) -> None:
        esecutore = EsecutoreFinto(confronto={
            "run": {"id": "nuova"}, "files": [], "suppliers": [{"id": "betulla"}],
            "products": [{"id": "product:9", "offers": [{"supplierId": "betulla", "available": True}]}],
            "warnings": [],
        })
        esito = self.esegui(esecutore)
        self.assertEqual(esito["stato"], pipeline_jobs.COMPLETATO, esito.get("messaggio"))
        stato = json.loads(self.stato.read_bytes())
        self.assertEqual(stato["products"], [])
        self.assertIn("SCELTE_NON_PIU_VALIDE", {avviso["code"] for avviso in esito["avvisi"]})
        # Not just in the job's transient state: the page rereads it from the document.
        confronto = json.loads(self.review.read_bytes())
        self.assertIn(
            "SCELTE_NON_PIU_VALIDE",
            {avviso["code"] for avviso in confronto.get("warnings") or []},
        )

    # -- confirmation is scoped to the exact item the user looked at --------

    OFFERTA_DI_PRIMA = {
        "supplierId": "betulla",
        "available": True,
        "ean": "8000000000011",
        "supplierCode": "C-11",
        "description": "PASTA MEZZE MANICHE 500 G",
        "requiresConfirmation": True,
    }

    def _stato_confermato(self, offerta: dict) -> None:
        """The state as `save_state` leaves it after a user confirmation."""

        self._scrivi(self.stato, {
            "schemaVersion": 1,
            "runId": "vecchia",
            "products": [{
                "id": "product:1",
                "quantity": 4,
                "selectedSupplierId": "betulla",
                "confirmed": True,
                "confirmedArticle": pipeline_jobs.impronta_articolo(offerta),
            }],
        })

    def _confronto_con(self, offerta: dict) -> dict:
        # Same product as the previous run (same barcode) with an offer
        # that may have changed: the case these tests cover. A replaced
        # product has its own separate test.
        return {
            "run": {"id": "nuova"}, "files": [], "suppliers": [{"id": "betulla", "name": "BETULLA"}],
            "products": [{"id": "product:1", "name": "Uno", "ean": "8000000000001", "offers": [offerta]}],
            "warnings": [],
        }

    def test_una_conferma_non_copre_un_articolo_diverso_della_settimana_dopo(self) -> None:
        """The supplier still has an offer, but it's a different row of the price list.

        Nothing would get reset — the offer is still there — and the "È lo
        stesso articolo?" (is this the same item?) checkbox would stay
        checked on an item the user never saw. Compilation would proceed
        without a word.
        """

        self._stato_confermato(self.OFFERTA_DI_PRIMA)
        altro_articolo = {
            **self.OFFERTA_DI_PRIMA,
            "ean": "8000000000099",
            "supplierCode": "C-99",
            "description": "RISO ARBORIO 1 KG",
        }

        esito = self.esegui(EsecutoreFinto(confronto=self._confronto_con(altro_articolo)))

        stato = json.loads(self.stato.read_bytes())
        self.assertFalse(stato["products"][0]["confirmed"])
        self.assertNotIn("confirmedArticle", stato["products"][0])
        # The quantity is NOT reset: the product still needs to be ordered,
        # it's the confirmation that must be redone.
        self.assertEqual(stato["products"][0]["quantity"], 4)
        self.assertIn("CONFERME_SCADUTE", {avviso["code"] for avviso in esito["avvisi"]})
        confronto = json.loads(self.review.read_bytes())
        avviso = next(
            voce for voce in confronto.get("warnings") or []
            if voce.get("code") == "CONFERME_SCADUTE"
        )
        self.assertIn("riga diversa del listino", avviso["message"])
        self.assertIn("riconferma", avviso["message"])

    def test_lo_stesso_articolo_non_fa_rifare_la_conferma(self) -> None:
        """Re-asking on every recompute would just train the user to check the box without reading."""

        self._stato_confermato(self.OFFERTA_DI_PRIMA)

        esito = self.esegui(EsecutoreFinto(confronto=self._confronto_con(dict(self.OFFERTA_DI_PRIMA))))

        stato = json.loads(self.stato.read_bytes())
        self.assertTrue(stato["products"][0]["confirmed"])
        self.assertNotIn("CONFERME_SCADUTE", {avviso["code"] for avviso in esito["avvisi"]})

    def test_un_prezzo_nuovo_sullo_stesso_articolo_non_fa_scadere_niente(self) -> None:
        """The item's fingerprint is its identity, not its current price or quantity.

        Price lists change price every week: if that alone invalidated a
        confirmation, the question would come back on every row and nobody
        would read it anymore.
        """

        self._stato_confermato(self.OFFERTA_DI_PRIMA)
        stesso_articolo_altro_prezzo = {
            **self.OFFERTA_DI_PRIMA,
            "unitPriceNet": 9.99,
            "quantityFactor": 12,
        }

        self.esegui(EsecutoreFinto(confronto=self._confronto_con(stesso_articolo_altro_prezzo)))

        stato = json.loads(self.stato.read_bytes())
        self.assertTrue(stato["products"][0]["confirmed"])

    def test_una_conferma_che_non_dice_su_cosa_non_e_una_conferma(self) -> None:
        """State saved before this rule existed: fail closed rather than trust it."""

        self._scrivi(self.stato, {
            "schemaVersion": 1,
            "runId": "vecchia",
            "products": [{
                "id": "product:1", "quantity": 4, "selectedSupplierId": "betulla", "confirmed": True,
            }],
        })

        esito = self.esegui(EsecutoreFinto(confronto=self._confronto_con(dict(self.OFFERTA_DI_PRIMA))))

        stato = json.loads(self.stato.read_bytes())
        self.assertFalse(stato["products"][0]["confirmed"])
        self.assertIn("CONFERME_SCADUTE", {avviso["code"] for avviso in esito["avvisi"]})

    # -- quantities sourced from the management-software export follow that export --
    #
    # Without this, a re-uploaded reorder list can go unused: despite
    # products having different quantities, every one shows 0, because a
    # decision saved from the previous list masks the new comparison.

    GESTIONALE_DI_PRIMA = [{"role": "master", "name": "ordine.xlsx", "sourceSha256": "a" * 64}]
    GESTIONALE_NUOVO = [{"role": "master", "name": "ordine2.xlsx", "sourceSha256": "b" * 64}]

    def _elenco_di_prima(self, documenti: list[dict], *, ean: str = "8000000000001") -> None:
        """The live comparison: what was there before this recompute."""

        self._scrivi(self.review, {
            **CONFRONTO_PRECEDENTE,
            "files": documenti,
            "products": [{"id": "product:1", "name": "Uno", "ean": ean, "quantity": 1, "offers": []}],
        })

    def _decisione(self, **campi) -> None:
        self._scrivi(self.stato, {
            "schemaVersion": 1,
            "runId": "vecchia",
            "products": [{
                "id": "product:1",
                "quantity": 0,
                "selectedSupplierId": "betulla",
                "quantitySource": "gestionale",
                **campi,
            }],
        })

    def _confronto_del_gestionale(
        self, documenti: list[dict], quantita: int, *, ean: str = "8000000000001",
    ) -> dict:
        return {
            "run": {"id": "nuova"},
            "files": documenti,
            "suppliers": [{"id": "betulla", "name": "BETULLA"}],
            "products": [{
                "id": "product:1",
                "name": "Uno",
                "ean": ean,
                "quantity": quantita,
                "offers": [{"supplierId": "betulla", "available": True}],
            }],
            "warnings": [],
        }

    def test_un_elenco_nuovo_riporta_le_sue_quantita(self) -> None:
        """A new reorder list is a different document: its quantities are its own."""

        self._elenco_di_prima(self.GESTIONALE_DI_PRIMA)
        self._decisione(quantity=0)

        esito = self.esegui(EsecutoreFinto(
            confronto=self._confronto_del_gestionale(self.GESTIONALE_NUOVO, 3),
        ))

        stato = json.loads(self.stato.read_bytes())
        self.assertEqual(stato["products"][0]["quantity"], 3)
        # And no warning: a quantity sourced from the reorder list is the
        # normal case, not something worth flagging.
        self.assertNotIn("QUANTITA_RIPRESE_DAL_GESTIONALE", {voce["code"] for voce in esito["avvisi"]})

    def test_le_quantita_le_decide_il_gestionale_anche_a_elenco_uguale(self) -> None:
        """A quantity sourced from the reorder list is re-read from it on every recompute.

        No exception even when the list itself is unchanged: it's re-read
        unconditionally. As a consequence, the "Azzera le quantità proposte
        dal gestionale" (reset suggested quantities) button only holds until
        the next recompute.
        """

        self._elenco_di_prima(self.GESTIONALE_DI_PRIMA)
        self._decisione(quantity=0)

        esito = self.esegui(EsecutoreFinto(
            confronto=self._confronto_del_gestionale(self.GESTIONALE_DI_PRIMA, 3),
        ))

        stato = json.loads(self.stato.read_bytes())
        self.assertEqual(stato["products"][0]["quantity"], 3)
        self.assertNotIn("QUANTITA_RIPRESE_DAL_GESTIONALE", {voce["code"] for voce in esito["avvisi"]})
        # The count isn't lost: it stays in the run summary for anyone who looks.
        self.assertEqual(esito["numeri"].get("quantitaRiprese"), 1)

    def test_le_quantita_scritte_da_te_non_le_tocca_nessuno(self) -> None:
        """`quantitySource: "utente"` means the user typed that number: it's theirs."""

        self._elenco_di_prima(self.GESTIONALE_DI_PRIMA)
        self._decisione(quantity=7, quantitySource="utente")

        self.esegui(EsecutoreFinto(
            confronto=self._confronto_del_gestionale(self.GESTIONALE_NUOVO, 3),
        ))

        stato = json.loads(self.stato.read_bytes())
        self.assertEqual(stato["products"][0]["quantity"], 7)

    def test_la_stessa_riga_con_un_altro_articolo_perde_le_sue_decisioni(self) -> None:
        """Product ids are row numbers: with a new reorder list, row 1 is a
        different product, so a saved supplier choice keyed to that id
        would refer to the wrong item."""

        self._elenco_di_prima(self.GESTIONALE_DI_PRIMA)
        self._decisione(quantity=4, confirmed=True)

        esito = self.esegui(EsecutoreFinto(
            confronto=self._confronto_del_gestionale(
                self.GESTIONALE_NUOVO, 2, ean="8009999999999",
            ),
        ))

        stato = json.loads(self.stato.read_bytes())
        self.assertEqual(stato["products"], [])
        avviso = next(voce for voce in esito["avvisi"] if voce["code"] == "DECISIONI_SCOLLEGATE")
        self.assertIn("un articolo diverso da prima", avviso["message"])
        # And the page rereads it from the document, not just from the job's transient state.
        confronto = json.loads(self.review.read_bytes())
        self.assertIn(
            "DECISIONI_SCOLLEGATE",
            {voce.get("code") for voce in confronto.get("warnings") or []},
        )

    def test_un_prodotto_aggiunto_a_mano_non_perde_la_sua_quantita(self) -> None:
        """Manually added products come from saved state, not from the comparison document.

        Looking for them only among the comparison's own products would mark
        them "gone" on every recompute: their quantity would reset to zero,
        with the wrong explanation ("this offer is missing from the new price list").
        """

        self._scrivi(self.stato, {
            "schemaVersion": 1,
            "runId": "vecchia",
            "products": [{
                "id": "manual:1", "quantity": 2, "selectedSupplierId": "betulla",
                "quantitySource": "utente",
            }],
            "manualProducts": [{
                "id": "manual:1", "name": "Aggiunto a mano", "ean": "8007777777777",
                "offers": [{"supplierId": "betulla", "available": True}],
            }],
        })

        esito = self.esegui(EsecutoreFinto())

        stato = json.loads(self.stato.read_bytes())
        self.assertEqual([voce["id"] for voce in stato["products"]], ["manual:1"])
        self.assertEqual(stato["products"][0]["quantity"], 2)
        self.assertNotIn("SCELTE_NON_PIU_VALIDE", {voce["code"] for voce in esito["avvisi"]})

    # -- an invalid saved supplier choice: two distinct cases ----------------
    #
    # The right question isn't "is the chosen supplier still there", it's
    # "can anyone still supply this": if someone can, the quantity resets
    # to zero because picking which offer is the user's decision; if nobody
    # can, there's nothing to choose and resetting would discard the only
    # thing known about that row — how many units are needed.

    def _confronto_con_offerta(self, *offerte: dict) -> dict:
        return {
            "run": {"id": "nuova"}, "files": [], "suppliers": [{"id": "betulla"}, {"id": "larice"}],
            "products": [{"id": "product:1", "offers": list(offerte)}],
            "warnings": [],
        }

    def test_con_un_altro_fornitore_disponibile_la_quantita_torna_a_zero(self) -> None:
        """Unchanged behavior: here a choice exists, and the user makes it."""

        esecutore = EsecutoreFinto(confronto=self._confronto_con_offerta(
            {"supplierId": "betulla", "available": False},
            {"supplierId": "larice", "available": True},
        ))

        esito = self.esegui(esecutore)

        stato = json.loads(self.stato.read_bytes())
        self.assertEqual(stato["products"][0]["quantity"], 0)
        self.assertEqual(stato["products"][0]["selectedSupplierId"], "")
        self.assertFalse(stato["products"][0]["confirmed"])
        self.assertIn("SCELTE_NON_PIU_VALIDE", {voce["code"] for voce in esito["avvisi"]})

    def test_se_nessuno_ce_l_ha_la_quantita_resta_e_il_fornitore_si_svuota(self) -> None:
        """The intended behavior: this is a product to be sourced, not an error.

        If this test regresses to reading 0, the next recompute drops the
        product from the "to be sourced" list — only items with a
        quantity > 0 appear there — and it disappears with nothing to say so.
        """

        esecutore = EsecutoreFinto(confronto=self._confronto_con_offerta(
            {"supplierId": "betulla", "available": False},
        ))

        esito = self.esegui(esecutore)

        stato = json.loads(self.stato.read_bytes())
        self.assertEqual(stato["products"][0]["quantity"], 4)
        self.assertEqual(stato["products"][0]["selectedSupplierId"], "")
        self.assertFalse(stato["products"][0]["confirmed"])
        # No warning here either: nothing was lost. SCELTE_NON_PIU_VALIDE
        # would claim the quantity was reset, which would be false.
        self.assertNotIn("SCELTE_NON_PIU_VALIDE", {voce["code"] for voce in esito["avvisi"]})

    def test_il_fornitore_svuotato_finisce_davvero_sul_disco(self) -> None:
        """Keeping the quantity must not mean skipping the write entirely.

        State is only rewritten when something changed, and this case
        raises none of the counters that produce a warning: without its own
        reason to rewrite, the cleared supplier would stay on disk and the
        next compilation would order from a supplier unable to fill it.
        """

        self._scrivi(self.stato, {
            "schemaVersion": 1,
            "runId": "vecchia",
            "products": [{
                "id": "product:1", "quantity": 4, "selectedSupplierId": "betulla",
                "confirmed": True,
                "confirmedArticle": pipeline_jobs.impronta_articolo(
                    {"supplierId": "betulla", "ean": "8000000000011"},
                ),
            }],
        })

        self.esegui(EsecutoreFinto(confronto=self._confronto_con_offerta(
            {"supplierId": "betulla", "available": False},
        )))

        salvato = json.loads(self.stato.read_bytes())["products"][0]
        self.assertEqual(salvato["quantity"], 4)
        self.assertEqual(salvato["selectedSupplierId"], "")
        self.assertFalse(salvato["confirmed"])
        self.assertNotIn("confirmedArticle", salvato)

    def test_una_decisione_gia_vuota_non_viene_toccata(self) -> None:
        """No quantity and no supplier: nothing to clean up, and the decision
        must come back exactly as it was."""

        decisione = {"id": "product:1", "quantity": 0, "selectedSupplierId": ""}
        self._scrivi(self.stato, {
            "schemaVersion": 1, "runId": "vecchia", "products": [dict(decisione)],
        })

        esito = self.esegui(EsecutoreFinto(confronto=self._confronto_con_offerta(
            {"supplierId": "betulla", "available": False},
        )))

        self.assertEqual(json.loads(self.stato.read_bytes())["products"], [decisione])
        self.assertNotIn("SCELTE_NON_PIU_VALIDE", {voce["code"] for voce in esito["avvisi"]})

    def test_lo_stato_segue_l_identificativo_della_run_nuova(self) -> None:
        """`PUT /api/state` rejects a state snapshot tagged with another run's id."""

        esecutore = EsecutoreFinto(confronto={
            "run": {"id": "nuova"}, "files": [], "suppliers": [{"id": "betulla"}],
            "products": [{"id": "product:1", "offers": [{"supplierId": "betulla", "available": True}]}],
            "warnings": [],
        })
        esito = self.esegui(esecutore)
        stato = json.loads(self.stato.read_bytes())
        self.assertEqual(stato["runId"], Path(esito["cartella"]).name)
        self.assertEqual(stato["products"][0]["quantity"], 4)


class UnLavoroPerVolta(BancoPipeline):
    def test_due_avvii_insieme_non_sono_ammessi(self) -> None:
        """The test's fake executor must accept `timeout_secondi` like the real one, or this test is a false positive.

        `_esegui` passes the timeout by keyword; a fake with a stale
        signature raises `TypeError` before it even starts, so the run dies
        in milliseconds and the second `avvia()` could win the race by
        accident even without the "one job at a time" guard working. The
        `partito` event below makes the intent explicit: the second start
        is attempted while the first command is still running.
        """

        blocco = threading.Event()
        partito = threading.Event()
        esecutore = EsecutoreFinto()
        originale = esecutore.__call__

        def lento(comando, cartella, avanzamento, **extra):
            partito.set()
            blocco.wait(timeout=10)
            return originale(comando, cartella, avanzamento, **extra)

        gestore = self.gestore(lento)
        gestore.avvia()
        self.assertTrue(partito.wait(timeout=10), "il primo comando non è mai partito")
        with self.assertRaises(LavoroGiaInCorso):
            gestore.avvia()
        blocco.set()
        esito = gestore.attendi(timeout=30)
        self.assertEqual(esito["stato"], pipeline_jobs.COMPLETATO, esito.get("messaggio"))

    def test_il_lucchetto_e_condiviso_con_gli_altri_lavori(self) -> None:
        lucchetto = threading.Lock()
        gestore = self.gestore(EsecutoreFinto(), lucchetto_lavori=lucchetto)
        lucchetto.acquire()
        try:
            with self.assertRaises(LavoroGiaInCorso):
                gestore.avvia()
        finally:
            lucchetto.release()

    def test_il_lucchetto_torna_libero_se_l_avvio_non_riesce(self) -> None:
        """The lock must be released even when the background thread itself fails to start.

        Between `acquire()` and the thread actually starting there are a
        few more steps, and any of them can fail — here, the thread
        creation itself, which is a real failure mode on a machine that's
        low on resources. Without this, a failure there would leave
        `lucchetto_lavori` held by nobody: every later "Ricalcola" would
        answer 409, `POST /api/spegni` would refuse to shut down, and the
        only way out would be force-closing the window — the program
        hanging over a failure that should have cost one error line.
        """

        gestore = self.gestore(EsecutoreFinto())
        originale = pipeline_jobs.threading.Thread

        class FiloCheNonParte(originale):
            def start(self):  # noqa: D102 - simulates "can't start new thread"
                raise RuntimeError("can't start new thread")

        pipeline_jobs.threading.Thread = FiloCheNonParte
        self.addCleanup(setattr, pipeline_jobs.threading, "Thread", originale)
        with self.assertRaises(RuntimeError):
            gestore.avvia()
        pipeline_jobs.threading.Thread = originale

        # The lock is free: verified from outside by trying to acquire it.
        self.assertTrue(gestore.lucchetto_lavori.acquire(blocking=False),
                        "il lucchetto dei lavori e' rimasto in mano a nessuno")
        gestore.lucchetto_lavori.release()
        # And the pipeline actually restarts, which is what the user sees.
        gestore.avvia()
        esito = gestore.attendi(timeout=30)
        self.assertEqual(esito["stato"], pipeline_jobs.COMPLETATO, esito.get("messaggio"))

    def test_il_lucchetto_torna_libero_anche_se_l_audit_esplode(self) -> None:
        """The lock release must not depend on anything else succeeding first.

        `_scrivi_audit_esecuzione` only catches `OSError`: any other
        exception — a `TypeError` from `json.dumps`, a `RecursionError`
        from a `deepcopy` — would leave `lucchetto_lavori` held forever,
        and from there every "Ricalcola" gets a 409, with no shutdown and
        no code update possible.
        """

        lucchetto = threading.Lock()
        gestore = self.gestore(EsecutoreFinto(), lucchetto_lavori=lucchetto)

        def esplode(cartella, registro_artefatti):
            raise TypeError("l'audit non si serializza")

        gestore._scrivi_audit_esecuzione = esplode
        gestore.avvia()
        with io.StringIO() as tampone, contextlib.redirect_stdout(tampone):
            esito = gestore.attendi(timeout=30)
            detto = tampone.getvalue()

        self.assertTrue(lucchetto.acquire(blocking=False), "il lucchetto è rimasto in mano")
        lucchetto.release()
        # The run's final status is already set before the audit write: if
        # that ordering were ever reversed, the page would be left with a
        # progress bar that never advances while `avvia()` answers 409 with
        # the lock actually free.
        self.assertEqual(esito["stato"], pipeline_jobs.COMPLETATO, esito.get("messaggio"))
        # And it's reported as a message, not a raw traceback from a
        # background thread that nobody at the store would ever read.
        self.assertIn("[AVVISO] audit della run non scritto", detto)
        self.assertIn("TypeError", detto)

    def test_il_lucchetto_torna_libero_dopo_una_run_fallita(self) -> None:
        lucchetto = threading.Lock()
        gestore = self.gestore(EsecutoreFinto(uscite={"inspect_sources.py": 9}), lucchetto_lavori=lucchetto)
        gestore.avvia()
        gestore.attendi(timeout=30)
        self.assertTrue(lucchetto.acquire(blocking=False))
        lucchetto.release()

    def test_una_run_interrotta_dal_riavvio_lo_dice(self) -> None:
        percorso = self.dati / pipeline_jobs.NOME_STATO
        self._scrivi(percorso, {"stato": pipeline_jobs.IN_CORSO, "fasi": [], "avvisi": []})
        gestore = self.gestore(EsecutoreFinto())
        self.assertEqual(gestore.stato()["stato"], pipeline_jobs.INTERROTTO)


class LaRiconfigurazioneDellaCompilazione(BancoPipeline):
    def test_a_ogni_ricalcolo_la_compilazione_viene_riconfigurata(self) -> None:
        visti: list[dict] = []
        esito = self.esegui(EsecutoreFinto(), su_confronto_attivato=visti.append)
        self.assertEqual(esito["stato"], pipeline_jobs.COMPLETATO)
        self.assertEqual(len(visti), 1)
        self.assertEqual(len(visti[0]["products"]), 2)

    def test_se_la_riconfigurazione_fallisce_il_confronto_resta_e_lo_dice(self) -> None:
        def rompi(_review: dict) -> None:
            raise RuntimeError("Node non c'è")

        esito = self.esegui(EsecutoreFinto(), su_confronto_attivato=rompi)
        self.assertEqual(esito["stato"], pipeline_jobs.COMPLETATO)
        self.assertIn("COMPILAZIONE_DA_RICONFIGURARE", {avviso["code"] for avviso in esito["avvisi"]})
        self.assertEqual(len(json.loads(self.review.read_bytes())["products"]), 2)

    def test_la_riconfigurazione_arriva_prima_che_il_confronto_nuovo_sia_vivo(self) -> None:
        """The ordering inside activation is a safety property, not an implementation detail.

        Between replacing the live comparison and reconfiguring the order
        writer there's a window, and the process can die inside it (power
        loss, the window closed). If the comparison is replaced first, what
        survives is today's comparison paired with last week's writer
        configuration — today's quantities silently written into last
        week's price-list rows. If the writer is reconfigured first, what
        survives is the previous comparison, which never hurt anyone.

        This test inspects the live file at the exact moment reconfiguration
        is called, since that's the only way to pin down the ordering.
        """

        visto_al_momento: list[str] = []

        def riconfigura(_review: dict) -> None:
            confronto_vivo = json.loads(self.review.read_bytes())
            visto_al_momento.append(str((confronto_vivo.get("run") or {}).get("id") or ""))

        esito = self.esegui(EsecutoreFinto(), su_confronto_attivato=riconfigura)
        self.assertEqual(esito["stato"], pipeline_jobs.COMPLETATO, esito.get("messaggio"))
        # While the writer reconfigures, the live comparison is still the previous one.
        self.assertEqual(visto_al_momento, ["vecchia"])
        # And the new comparison still lands afterward.
        vivo = json.loads(self.review.read_bytes())
        self.assertEqual(vivo["run"]["id"], esito["runId"])

    def test_l_avviso_della_riconfigurazione_fallita_entra_nel_documento(self) -> None:
        """A warning that lives only in the job's transient state won't be
        seen once the page is reopened later.

        This one is raised after the writer reconfiguration step, so it
        must be included in the warnings snapshot written to
        `review_data.json`, or the page would keep no trace that
        compilation needs to be re-checked.
        """

        def rompi(_review: dict) -> None:
            raise RuntimeError("Node non c'è")

        self.esegui(EsecutoreFinto(), su_confronto_attivato=rompi)
        confronto = json.loads(self.review.read_bytes())
        codici = {avviso["code"] for avviso in confronto.get("warnings") or []}
        self.assertIn("COMPILAZIONE_DA_RICONFIGURARE", codici)


class IDocumentiDoppi(BancoPipeline):
    def test_di_due_listini_dello_stesso_fornitore_si_usa_il_piu_recente(self) -> None:
        """The uploads folder accumulates files week after week.

        Two price lists for the same supplier would otherwise fail the
        parser with "duplicate supplier", which tells nobody what to do.
        The most recent one is kept, and the one left out is named: a
        silent discard would be worse.
        """

        esecutore = EsecutoreFinto(profili=[
            profilo("gestionale.xlsx", "gestionale_v1"),
            profilo("betulla-vecchio.xlsx", "betulla_v1", quando="2026-08-01T10:00:00+00:00"),
            profilo("betulla-nuovo.xlsx", "betulla_v1", quando="2026-08-12T10:00:00+00:00"),
        ])
        esito = self.esegui(esecutore)
        decisioni = json.loads((Path(esito["cartella"]) / "preflight_decisions.json").read_bytes())
        nomi = {voce["file_name"] for voce in decisioni["decisions"]}
        self.assertEqual(nomi, {"gestionale.xlsx", "betulla-nuovo.xlsx"})
        avvisi = [a for a in esito["avvisi"] if a["code"] == "DOCUMENTO_PIU_VECCHIO_LASCIATO_FUORI"]
        self.assertEqual(len(avvisi), 1)
        self.assertIn("betulla-vecchio.xlsx", avvisi[0]["message"])

    def test_l_avviso_dice_chi_e_stato_tenuto_chi_no_e_in_base_a_che_cosa(self) -> None:
        """Real-world case: the expired price list is the one copied over most recently.

        The selection looks at the file's modification time, which has
        nothing to do with the price list's own validity: an expired
        BETULLA re-copied today wins over yesterday's valid one. That
        criterion stays (it's the only signal available across every
        supplier), but the warning must say three things, or the reader
        assumes "most recent price list" and trusts it: who was kept, who
        was left out, and what the choice was based on.
        """

        esecutore = EsecutoreFinto(profili=[
            profilo("gestionale.xlsx", "gestionale_v1"),
            profilo("betulla-valido.xlsx", "betulla_v1", quando="2026-08-01T09:00:00+00:00"),
            profilo("betulla-scaduto.xlsx", "betulla_v1", quando="2026-08-12T18:30:00+00:00"),
        ])
        esito = self.esegui(esecutore)

        avvisi = [a for a in esito["avvisi"] if a["code"] == "DOCUMENTO_PIU_VECCHIO_LASCIATO_FUORI"]
        self.assertEqual(len(avvisi), 1)
        avviso = avvisi[0]
        self.assertEqual(avviso["tenuto"], "betulla-scaduto.xlsx")
        self.assertEqual(avviso["lasciatoFuori"], "betulla-valido.xlsx")
        # The two things the old message didn't say: the winner's name...
        self.assertIn("betulla-scaduto.xlsx", avviso["message"])
        self.assertIn("betulla-valido.xlsx", avviso["message"])
        # ...and that the choice looks at the file, not the price list's own validity.
        self.assertIn("data di modifica", avviso["message"])
        self.assertIn("non sulla validità", avviso["message"])

    def test_con_tre_documenti_l_avviso_nomina_il_vincitore_finale(self) -> None:
        """Whoever wins mid-comparison isn't necessarily the final winner.

        With three price lists for the same supplier, a warning about the
        first one discarded could name the intermediate winner instead: a
        real name, of a real file, that never actually made it into the
        comparison.
        """

        esecutore = EsecutoreFinto(profili=[
            profilo("gestionale.xlsx", "gestionale_v1"),
            profilo("betulla-1.xlsx", "betulla_v1", quando="2026-08-01T09:00:00+00:00"),
            profilo("betulla-2.xlsx", "betulla_v1", quando="2026-08-05T09:00:00+00:00"),
            profilo("betulla-3.xlsx", "betulla_v1", quando="2026-08-12T09:00:00+00:00"),
        ])
        esito = self.esegui(esecutore)

        decisioni = json.loads((Path(esito["cartella"]) / "preflight_decisions.json").read_bytes())
        self.assertEqual(
            {voce["file_name"] for voce in decisioni["decisions"]},
            {"gestionale.xlsx", "betulla-3.xlsx"},
        )
        avvisi = [a for a in esito["avvisi"] if a["code"] == "DOCUMENTO_PIU_VECCHIO_LASCIATO_FUORI"]
        self.assertEqual(
            {(a["lasciatoFuori"], a["tenuto"]) for a in avvisi},
            {("betulla-1.xlsx", "betulla-3.xlsx"), ("betulla-2.xlsx", "betulla-3.xlsx")},
        )

    def test_a_parita_di_data_l_avviso_non_attribuisce_la_scelta_alla_data(self) -> None:
        """A folder copied with a tool that preserves timestamps produces this case for real.

        With an identical `modified_at`, the date decided nothing — the
        last one in listing order wins — and the warning must say so,
        instead of crediting a criterion that didn't actually decide.
        """

        stesso_momento = "2026-08-12T10:00:00+00:00"
        esecutore = EsecutoreFinto(profili=[
            profilo("gestionale.xlsx", "gestionale_v1"),
            profilo("betulla-a.xlsx", "betulla_v1", quando=stesso_momento),
            profilo("betulla-b.xlsx", "betulla_v1", quando=stesso_momento),
        ])
        esito = self.esegui(esecutore)

        avvisi = [a for a in esito["avvisi"] if a["code"] == "DOCUMENTO_PIU_VECCHIO_LASCIATO_FUORI"]
        self.assertEqual(len(avvisi), 1)
        self.assertIn("stesso momento", avvisi[0]["message"])
        self.assertIn("ordine di elenco", avvisi[0]["message"])


class LaVoltaPrima(BancoPipeline):
    """What today's run is compared against.

    The comparison against "the previous run" is what produces
    `FORNITORE_SPARITO`, the only warning that notices a price list is
    gone. Picking the wrong folder to compare against just means it warns
    about nothing: an entire supplier drops out of the comparison silently.
    """

    AUDIT_CON_NOCE = {
        "master": {"rows": 3},
        "sources": {"betulla": {"rows": 100}, "noce": {"rows": 900}},
        "price_summary": {"betulla": {"usable": 100, "median": "2.0000"}},
        "inputs": [],
    }
    AUDIT_SENZA_NOCE = {
        "master": {"rows": 3},
        "sources": {"betulla": {"rows": 100}},
        "price_summary": {"betulla": {"usable": 100, "median": "2.0000"}},
        "inputs": [],
    }

    def _run_completa_con_noce(self) -> str:
        esito = self.esegui(EsecutoreFinto(audit=self.AUDIT_CON_NOCE))
        self.assertEqual(esito["stato"], pipeline_jobs.COMPLETATO, esito.get("messaggio"))
        return Path(esito["cartella"]).name

    def _cartella_intrusa(self, verbale: dict | None) -> Path:
        """A folder that looks like a run and that the filesystem reports as more recent.

        The file's timestamp is set manually, so the test doesn't depend on
        how long the machine takes, and under the old selection rule this
        folder would win for certain.
        """

        cartella = self.configurazione().esecuzioni_dir / "2026-08-13_2359"
        (cartella / "dati").mkdir(parents=True)
        self._scrivi(cartella / "dati" / "audit.json", self.AUDIT_SENZA_NOCE)
        if verbale is not None:
            self._scrivi(cartella / pipeline_jobs.NOME_AUDIT_ESECUZIONE, verbale)
        futuro = time.time() + 3600
        os.utime(cartella, (futuro, futuro))
        return cartella

    def _oggi_senza_noce(self) -> dict:
        return self.esegui(EsecutoreFinto(audit=self.AUDIT_SENZA_NOCE))

    def _il_confronto_vivo_non_dichiara_chi_lo_ha_attivato(self) -> None:
        """Simulates a comparison activated before `pipelineRunId` existed, forcing the fallback path.

        Without this, every test below would take the fast path (the live
        comparison already names which run activated it) and the guards on
        `COMPLETATO` and `iniziatoIl` would have no test actually exercising
        them — a fixture that only ever covers one branch.
        """

        percorso = self.configurazione().review_path
        confronto = json.loads(percorso.read_text(encoding="utf-8"))
        if isinstance(confronto.get("run"), dict):
            confronto["run"].pop("pipelineRunId", None)
        self._scrivi(percorso, confronto)

    def test_una_run_uccisa_a_meta_non_e_la_volta_prima(self) -> None:
        """`dati/audit.json` is written mid-pipeline; `esecuzione.json` only at the end.

        A run killed midway leaves the first file but not the second. Using
        it as the comparison baseline would compare today against a
        comparison that was never actually activated, and a supplier
        that was never even in it would drop out silently.
        """

        completa = self._run_completa_con_noce()
        self._il_confronto_vivo_non_dichiara_chi_lo_ha_attivato()
        orfana = self._cartella_intrusa(None)
        self.assertFalse((orfana / pipeline_jobs.NOME_AUDIT_ESECUZIONE).exists())

        esito = self._oggi_senza_noce()

        self.assertEqual(esito["numeri"]["confrontoConLaVoltaPrima"], completa)
        self.assertIn("FORNITORE_SPARITO", {avviso["code"] for avviso in esito["avvisi"]})

    def test_una_run_finita_male_non_e_la_volta_prima(self) -> None:
        """A run that stopped with an error never updated the live comparison: it isn't a valid baseline."""

        completa = self._run_completa_con_noce()
        self._il_confronto_vivo_non_dichiara_chi_lo_ha_attivato()
        self._cartella_intrusa({
            "stato": pipeline_jobs.ERRORE,
            "iniziatoIl": "2099-01-01T00:00:00+00:00",
        })

        esito = self._oggi_senza_noce()

        self.assertEqual(esito["numeri"]["confrontoConLaVoltaPrima"], completa)
        self.assertIn("FORNITORE_SPARITO", {avviso["code"] for avviso in esito["avvisi"]})

    def test_un_verbale_senza_l_ora_di_inizio_non_e_la_volta_prima(self) -> None:
        """Replaces the old fallback to the folder's file-modification time.

        Before, a run log that didn't say when the run started fell back
        to the folder's `st_mtime` — the last time anyone touched it, which
        a copy or a backup operation can push arbitrarily far forward. Now
        a run that can't say when it started is simply not a valid
        baseline and is skipped.
        """

        completa = self._run_completa_con_noce()
        self._il_confronto_vivo_non_dichiara_chi_lo_ha_attivato()
        self._cartella_intrusa({"stato": pipeline_jobs.COMPLETATO})

        esito = self._oggi_senza_noce()

        self.assertEqual(esito["numeri"]["confrontoConLaVoltaPrima"], completa)
        self.assertIn("FORNITORE_SPARITO", {avviso["code"] for avviso in esito["avvisi"]})

    def test_la_run_completa_piu_recente_resta_quella_giusta(self) -> None:
        """The new selection rule must still find the real previous run."""

        self._run_completa_con_noce()
        seconda = self._run_completa_con_noce()

        esito = self._oggi_senza_noce()

        self.assertEqual(esito["numeri"]["confrontoConLaVoltaPrima"], seconda)
        self.assertIn("FORNITORE_SPARITO", {avviso["code"] for avviso in esito["avvisi"]})

    def test_la_run_che_ha_attivato_e_la_volta_prima_anche_senza_verbale_completo(self) -> None:
        """A run that activated the live comparison is the baseline even with an incomplete run log.

        A run that dies right after `os.replace` has already activated its
        comparison, but its run log stays half-written. Requiring
        `COMPLETATO` from it would make it disappear from the comparison
        entirely: the baseline would degrade to "first run" and
        `FORNITORE_SPARITO` would go silent again — the same failure mode
        reached from the other side. The live comparison itself declares
        which run activated it (`run.pipelineRunId`), and that folder is
        the baseline, whatever its own run log says.
        """

        self._run_completa_con_noce()
        attivata = self._run_completa_con_noce()
        cartella = self.configurazione().esecuzioni_dir / attivata
        # Roll the run log back to how it looked right before the process
        # died: the run is half-written, but the comparison it activated
        # is the one on disk.
        self._scrivi(cartella / pipeline_jobs.NOME_AUDIT_ESECUZIONE, {
            "stato": pipeline_jobs.IN_CORSO,
            "iniziatoIl": "2026-08-13T11:59:00+00:00",
        })

        esito = self._oggi_senza_noce()

        self.assertEqual(esito["numeri"]["confrontoConLaVoltaPrima"], attivata)
        self.assertIn("FORNITORE_SPARITO", {avviso["code"] for avviso in esito["avvisi"]})

    def test_un_verbale_con_l_ora_senza_fuso_non_e_la_volta_prima(self) -> None:
        """A naive (timezone-less) `iniziatoIl` timestamp compares unpredictably against local time.

        Mixed in with real run logs — which always carry a timezone,
        written by `avvia()` — a naive timestamp can invert the ordering of
        runs by hours. The pipeline itself never produces one; a folder
        copied from another machine or edited by hand can. A run whose
        start time carries no timezone is not a valid baseline and is
        skipped.
        """

        completa = self._run_completa_con_noce()
        self._il_confronto_vivo_non_dichiara_chi_lo_ha_attivato()
        self._cartella_intrusa({
            "stato": pipeline_jobs.COMPLETATO,
            "iniziatoIl": "2099-01-01T00:00:00",
        })

        esito = self._oggi_senza_noce()

        self.assertEqual(esito["numeri"]["confrontoConLaVoltaPrima"], completa)
        self.assertIn("FORNITORE_SPARITO", {avviso["code"] for avviso in esito["avvisi"]})


class EsecutoreCheEsplode(EsecutoreFinto):
    """A command that raises an arbitrary exception, not a declared `Fermata`.

    This is the branch that once reached the page as a raw
    `AttributeError: module 'registro' has no attribute 'nome_del_fornitore'`.

    With `fotografia_da_installare` it also covers the on-disk code
    changing while the pipeline is running. Without that, a test for
    "failure after the run already started" would be a false positive —
    the entry guard would already have stopped everything and the test
    would pass for the wrong reason.
    """

    def __init__(self, dove: str, fotografia_da_installare: dict | None = None, **impostazioni) -> None:
        super().__init__(**impostazioni)
        self.dove = dove
        self.fotografia_da_installare = fotografia_da_installare

    def __call__(self, comando, cartella, avanzamento, **extra) -> RisultatoComando:
        if Path(list(comando)[1]).name == self.dove:
            if self.fotografia_da_installare is not None:
                pipeline_jobs.versione_del_codice.ALL_AVVIO = self.fotografia_da_installare
            raise AttributeError("module 'registro' has no attribute 'nome_del_fornitore'")
        return super().__call__(comando, cartella, avanzamento, **extra)


class IlCodiceCambiatoSottoIlProgramma(BancoPipeline):
    """Code updated while the server is running must announce itself and say what to do.

    The underlying problem wasn't the raw `AttributeError`: it was that the
    user was shown a line of Python with nothing actionable to do about it
    from the browser. These tests pin down the two things that make it
    self-resolvable: the run refuses to start, and the message says to
    close and reopen the app.
    """

    def _fissa_la_fotografia(self, fotografia: dict) -> None:
        modulo = pipeline_jobs.versione_del_codice
        vera = modulo.ALL_AVVIO
        modulo.ALL_AVVIO = fotografia
        self.addCleanup(setattr, modulo, "ALL_AVVIO", vera)

    def finge_il_codice_cambiato(self, quale: str = "scripts/registro.py") -> None:
        fotografia = dict(pipeline_jobs.versione_del_codice.ALL_AVVIO)
        fotografia[quale] = "0" * 64
        self._fissa_la_fotografia(fotografia)

    def finge_il_codice_fermo(self) -> None:
        """Recomputes the code snapshot right now: nobody has touched anything.

        These tests need to say something about the pipeline's behavior,
        not about whatever moment the suite happens to run at: without
        this, any `.py` edit made while the suite is running would turn
        them red for the wrong reason.
        """

        self._fissa_la_fotografia(pipeline_jobs.versione_del_codice.fotografia())

    def test_la_run_non_parte_nemmeno_e_lo_dice(self) -> None:
        self.finge_il_codice_cambiato()
        esecutore = EsecutoreFinto()

        esito = self.esegui(esecutore)

        self.assertEqual(esito["stato"], pipeline_jobs.ERRORE)
        self.assertEqual(esito["fermata"]["code"], pipeline_jobs.CODICE_CAMBIATO_DOPO_L_AVVIO)
        self.assertEqual(esecutore.chiamati, [])

    def test_la_frase_dice_di_chiudere_e_riaprire_e_che_non_si_perde_niente(self) -> None:
        self.finge_il_codice_cambiato()

        esito = self.esegui(EsecutoreFinto())

        frase = esito["fermata"]["message"]
        self.assertIn("AVVIA_COMPARATORE.cmd", frase)
        self.assertIn("Non perdi niente", frase)
        self.assertIn("scripts/registro.py", frase)

    def test_il_confronto_precedente_resta_intatto(self) -> None:
        self.finge_il_codice_cambiato()

        self.esegui(EsecutoreFinto())

        self.assertEqual(json.loads(self.review.read_bytes()), CONFRONTO_PRECEDENTE)

    def test_un_guasto_a_run_avviata_dice_lo_stesso_rimedio(self) -> None:
        """The code changes while the pipeline is already running: same remedy applies.

        An earlier version of this test was a false positive: it faked the
        code change before the run started, so the entry guard stopped the
        run and the failure branch was never actually exercised. Here the
        run starts with a stable snapshot, the code changes mid-run during
        the build step, and only then does the step fail.
        """

        self.finge_il_codice_fermo()
        cambiata = dict(pipeline_jobs.versione_del_codice.ALL_AVVIO)
        cambiata["scripts/registro.py"] = "0" * 64

        esito = self.esegui(EsecutoreCheEsplode("build_review_data.py", cambiata))

        self.assertEqual(esito["fermata"]["code"], pipeline_jobs.CODICE_CAMBIATO_DOPO_L_AVVIO)
        self.assertIn("AVVIA_COMPARATORE.cmd", esito["fermata"]["message"])
        self.assertIn("AttributeError", esito["fermata"]["dettaglio"])

    def test_un_guasto_vero_resta_un_guasto_e_non_manda_a_riavviare(self) -> None:
        """Without this distinction, every real bug would be reported as "please restart"."""

        self.finge_il_codice_fermo()

        esito = self.esegui(EsecutoreCheEsplode("build_review_data.py"))

        self.assertEqual(esito["fermata"]["code"], "GUASTO_INATTESO")
        self.assertNotIn("AVVIA_COMPARATORE.cmd", esito["fermata"]["message"])

    def test_un_guasto_vero_dice_comunque_che_cosa_puo_fare(self) -> None:
        self.finge_il_codice_fermo()

        esito = self.esegui(EsecutoreCheEsplode("build_review_data.py"))

        self.assertIn("Riprova", esito["fermata"]["message"])
        self.assertIn("chiudi e riapri", esito["fermata"]["message"])

    def test_la_frase_tecnica_non_si_butta(self) -> None:
        """The technical detail is kept for whoever needs to diagnose it, but stays secondary to the user-facing message."""

        self.finge_il_codice_fermo()

        esito = self.esegui(EsecutoreCheEsplode("build_review_data.py"))

        self.assertIn("AttributeError", esito["fermata"]["dettaglio"])
        self.assertIn("nome_del_fornitore", esito["fermata"]["dettaglio"])

    def test_con_il_codice_fermo_la_run_buona_passa(self) -> None:
        """The guard must not cost a healthy run its weekly recompute."""

        self.finge_il_codice_fermo()

        esito = self.esegui(EsecutoreFinto())

        self.assertEqual(esito["stato"], pipeline_jobs.COMPLETATO, esito.get("messaggio"))


class LeUguaglianzeArrivanoAllaLettura(BancoPipeline):
    """Barcodes declared equivalent must reach the step that reads the price lists.

    This is the link where a regression would stay invisible: the
    declaration keeps showing on the page, but the run silently ignores
    it — the manually matched product comes back without that supplier and
    nobody knows why.
    """

    CLASSI = [["4009428623194", "8729721830575"]]

    def argomenti_della_lettura(self, **extra) -> list[str]:
        esecutore = EsecutoreFinto()
        self.esegui(esecutore, **extra)
        return esecutore.argomenti["prepare_manifest_sources.py"]

    def test_il_file_si_scrive_nella_cartella_della_run_e_si_passa(self) -> None:
        argomenti = self.argomenti_della_lettura(uguaglianze_dichiarate=lambda: self.CLASSI)

        self.assertIn("--equivalenze", argomenti)
        percorso = Path(argomenti[argomenti.index("--equivalenze") + 1])
        self.assertEqual(percorso.name, "uguaglianze.json")
        self.assertEqual(
            json.loads(percorso.read_text(encoding="utf-8")), {"classi": self.CLASSI}
        )

    def test_senza_dichiarazioni_non_si_chiede_niente(self) -> None:
        """Requesting a file that doesn't exist would stop the run: a run with
        no declared equivalences is the normal case."""

        self.assertNotIn("--equivalenze", self.argomenti_della_lettura())
        self.assertNotIn("--equivalenze", self.argomenti_della_lettura(uguaglianze_dichiarate=lambda: []))

    def test_i_gruppi_di_un_codice_solo_non_arrivano(self) -> None:
        argomenti = self.argomenti_della_lettura(uguaglianze_dichiarate=lambda: [["4009428623194"]])

        self.assertNotIn("--equivalenze", argomenti)

    def test_un_magazzino_che_non_si_legge_non_ferma_il_ricalcolo(self) -> None:
        """Declared equivalences are extra memory: losing them costs one match,
        stopping the run costs the whole week. It's reported in the run's
        numbers instead of silently pretending there were none."""

        def esplode() -> list[list[str]]:
            raise RuntimeError("il file delle conferme non si apre")

        esecutore = EsecutoreFinto()
        esito = self.esegui(esecutore, uguaglianze_dichiarate=esplode)

        self.assertEqual(esito["stato"], pipeline_jobs.COMPLETATO, esito.get("messaggio"))
        self.assertNotIn("--equivalenze", esecutore.argomenti["prepare_manifest_sources.py"])
        self.assertIn("non si apre", str(esito["numeri"].get("uguaglianzeNonLette")))

    def test_quante_ne_valevano_resta_scritto_nella_run(self) -> None:
        esecutore = EsecutoreFinto()
        esito = self.esegui(esecutore, uguaglianze_dichiarate=lambda: self.CLASSI)

        self.assertEqual(esito["numeri"].get("uguaglianzeDichiarate"), 1)


class LaFaseCheCostaDueMinutiDiceQuantiCasiHaDavanti(BancoPipeline):
    """The AI evaluation step reports how many cases it has ahead of it while it runs.

    Progress is "completed phases divided by nine", all weighted equally.
    `VALUTAZIONE_AI` is the sixth phase and, by the code's own accounting,
    the longest: the progress bar climbs in steps up to 55.6% and then sits
    still for the longest stretch of the whole run, so the phase row must
    say something more than "in progress" while it's running.

    The detail is now written at the start too, and this test checks it
    while the phase is still running: once the phase finishes, that text
    is replaced by the outcome, so checking it afterward would prove nothing.
    """

    def test_mentre_gira_la_riga_della_fase_dice_quanti_casi(self) -> None:
        visto: dict = {}

        class EsecutoreCheGuarda(EsecutoreFinto):
            """Captures the run's state while the AI evaluation phase is in progress."""

            guarda = None

            def __call__(self, comando, cartella, avanzamento, **extra):
                if Path(list(comando)[1]).name == "valuta_shortlist.py" and self.guarda:
                    visto["fasi"] = self.guarda()
                return super().__call__(comando, cartella, avanzamento, **extra)

        esecutore = EsecutoreCheGuarda(
            shortlist=[
                {"gestionale_source_row": 2, "supplier": "betulla"},
                {"gestionale_source_row": 3, "supplier": "betulla"},
            ],
            # The report must declare the same case count as the shortlist,
            # or activation stops earlier — which is the correct behavior.
            rapporto={
                "casi_ricevuti": 2, "casi_valutabili": 2, "casi_decisi": 1,
                "chiamate": 2, "costo_usd": 0.0004, "model": "openai/gpt-5.6-luna",
                "versione_prompt": "v3", "versione_avversario": "v1", "degradato": False,
            },
        )
        gestore = self.gestore(esecutore)
        esecutore.guarda = lambda: [dict(voce) for voce in gestore.stato().get("fasi") or []]

        gestore.avvia()
        esito = gestore.attendi(timeout=30)

        self.assertEqual(esito["stato"], pipeline_jobs.COMPLETATO, esito.get("messaggio"))
        durante = next(voce for voce in visto["fasi"] if voce["nome"] == "VALUTAZIONE_AI")
        self.assertEqual(durante["stato"], pipeline_jobs.IN_CORSO)
        self.assertEqual(durante.get("dettaglio"), "2 casi da valutare")

    def test_con_un_caso_solo_il_plurale_e_giusto(self) -> None:
        visto: dict = {}

        class EsecutoreCheGuarda(EsecutoreFinto):
            guarda = None

            def __call__(self, comando, cartella, avanzamento, **extra):
                if Path(list(comando)[1]).name == "valuta_shortlist.py" and self.guarda:
                    visto["fasi"] = self.guarda()
                return super().__call__(comando, cartella, avanzamento, **extra)

        esecutore = EsecutoreCheGuarda()
        gestore = self.gestore(esecutore)
        esecutore.guarda = lambda: [dict(voce) for voce in gestore.stato().get("fasi") or []]

        gestore.avvia()
        gestore.attendi(timeout=30)

        durante = next(voce for voce in visto["fasi"] if voce["nome"] == "VALUTAZIONE_AI")
        self.assertEqual(durante.get("dettaglio"), "1 caso da valutare")

    def test_a_fase_finita_il_dettaglio_diventa_quello_dell_esito(self) -> None:
        """The startup detail must not linger once the phase is done: what's
        useful then is the outcome, not the original case count."""

        esito = self.esegui(EsecutoreFinto())

        fase = next(voce for voce in esito["fasi"] if voce["nome"] == "VALUTAZIONE_AI")
        self.assertEqual(fase["stato"], pipeline_jobs.COMPLETATO)
        self.assertIn("decisi su", fase.get("dettaglio", ""))
        self.assertNotIn("da valutare", fase.get("dettaglio", ""))


class LAttivazioneNonSiIncrociaColSalvataggio(BancoPipeline):
    """The one stretch where the pipeline writes files that the HTTP routes also serve.

    `_ripulisci_stato` rewrites `state.json`, and right after that
    `review_data.json` becomes the new comparison. A page save landing in
    between would write the same file from the same temp file, and what
    survived on disk would be neither the user's state nor the pipeline's.
    Hence the shared lock: it's the same `ReviewStore.lock` the HTTP routes
    use, passed into the constructor.
    """

    @staticmethod
    def _libero(lucchetto: threading.RLock) -> bool:
        """Whether a different thread can acquire the lock, not this one: an
        `RLock` can be reacquired by its own holder any number of times,
        which would always answer yes."""

        esito: list[bool] = []

        def prova() -> None:
            preso = lucchetto.acquire(blocking=False)
            esito.append(preso)
            if preso:
                lucchetto.release()

        filo = threading.Thread(target=prova)
        filo.start()
        filo.join(timeout=10)
        return esito == [True]

    def test_lo_stato_si_ripulisce_col_lucchetto_delle_rotte_in_mano(self) -> None:
        lucchetto = threading.RLock()
        gestore = self.gestore(EsecutoreFinto(), lucchetto_dati=lucchetto)
        visto: list[bool] = []
        originale = gestore._ripulisci_stato

        def spia(confronto):
            visto.append(self._libero(lucchetto))
            return originale(confronto)

        gestore._ripulisci_stato = spia
        gestore.avvia()
        esito = gestore.attendi(timeout=30)

        self.assertEqual(esito["stato"], pipeline_jobs.COMPLETATO, esito.get("messaggio"))
        self.assertEqual(visto, [False], "la ripulitura dello stato gira senza lucchetto")

    def test_anche_la_sostituzione_del_confronto_vivo(self) -> None:
        """The other end of the section: the moment the new comparison becomes
        the live one. Between the two ends, state and comparison disagree,
        and that's exactly the window a route must not be able to observe."""

        lucchetto = threading.RLock()
        gestore = self.gestore(EsecutoreFinto(), lucchetto_dati=lucchetto)
        visto: list[bool] = []
        replace_vero = os.replace

        def annota(sorgente, destinazione):
            # `resolve()` on both sides: on macOS the temp dir path is
            # `/var/...` from the test and `/private/var/...` from the
            # configuration, so a plain equality check would never match.
            if Path(destinazione).resolve() == self.review.resolve():
                visto.append(self._libero(lucchetto))
            return replace_vero(sorgente, destinazione)

        os.replace = annota
        try:
            gestore.avvia()
            esito = gestore.attendi(timeout=30)
        finally:
            os.replace = replace_vero

        self.assertEqual(esito["stato"], pipeline_jobs.COMPLETATO, esito.get("messaggio"))
        self.assertEqual(visto, [False], "il confronto vivo si sostituisce senza lucchetto")

    def test_a_fase_finita_il_lucchetto_torna_libero(self) -> None:
        lucchetto = threading.RLock()
        gestore = self.gestore(EsecutoreFinto(), lucchetto_dati=lucchetto)

        gestore.avvia()
        esito = gestore.attendi(timeout=30)

        self.assertEqual(esito["stato"], pipeline_jobs.COMPLETATO, esito.get("messaggio"))
        self.assertTrue(self._libero(lucchetto))

    def test_una_pagina_che_salva_di_continuo_non_pianta_la_catena(self) -> None:
        """A run and continuous page saves happening at the same time must not deadlock.

        A second lock is the classic way to earn a deadlock, which here
        would look like the program hanging right after the run finishes.
        The thread below does what the page does — acquire, release — for
        the whole duration of the run.
        """

        lucchetto = threading.RLock()
        gestore = self.gestore(EsecutoreFinto(), lucchetto_dati=lucchetto)
        basta = threading.Event()
        giri = []

        def come_la_pagina() -> None:
            while not basta.is_set():
                with lucchetto:
                    giri.append(1)
                time.sleep(0.001)

        filo = threading.Thread(target=come_la_pagina, daemon=True)
        filo.start()
        try:
            gestore.avvia()
            esito = gestore.attendi(timeout=30)
        finally:
            basta.set()
            filo.join(timeout=10)

        self.assertFalse(filo.is_alive())
        self.assertEqual(esito["stato"], pipeline_jobs.COMPLETATO, esito.get("messaggio"))
        self.assertTrue(giri, "il filo che simula la pagina non ha mai preso il lucchetto")
        confronto = json.loads(self.review.read_bytes())
        self.assertEqual([p["id"] for p in confronto["products"]], ["product:1", "product:2"])


class LOrologioPuoTornareIndietro(BancoPipeline):
    """The wall clock can correct itself mid-run (NTP sync, daylight saving time).

    A clock adjustment landing in the middle of a recompute could write a
    negative or otherwise wrong phase duration to the page and to the
    folder's audit file — the exact number relied on to judge "this was slow".
    """

    def test_le_durate_delle_fasi_non_seguono_l_ora_di_parete(self) -> None:
        vero = pipeline_jobs.datetime

        class OrologioCheTornaIndietro(vero):
            """Every read returns a time one second earlier than the previous read."""

            letture = 0

            @classmethod
            def now(cls, tz=None):  # type: ignore[override]
                cls.letture += 1
                return vero(2026, 8, 20, 12, 0, 0, tzinfo=tz) - timedelta(seconds=cls.letture)

        pipeline_jobs.datetime = OrologioCheTornaIndietro
        try:
            esito = self.esegui(EsecutoreFinto())
        finally:
            pipeline_jobs.datetime = vero

        self.assertEqual(esito["stato"], pipeline_jobs.COMPLETATO, esito.get("messaggio"))
        self.assertGreater(OrologioCheTornaIndietro.letture, 0, "l'orologio finto non è mai stato letto")
        durate = [voce.get("durataSecondi") for voce in esito["fasi"]]
        self.assertEqual(len(durate), len(pipeline_jobs.FASI))
        for nome, durata in zip((voce["nome"] for voce in esito["fasi"]), durate):
            self.assertIsNotNone(durata, nome)
            self.assertGreaterEqual(durata, 0, f"{nome} dice di essere durata {durata} secondi")

    def test_la_cartella_della_run_porta_ancora_l_ora_locale(self) -> None:
        """The one remaining `datetime.now()` call is intentional: the folder
        name is a date meant for a person to read.

        Checking only its format isn't enough: `2026-08-20_1000` in UTC has
        the same shape as `2026-08-20_1200` in local time, so a version of
        this test that only checked the format would stay green even if
        local time were silently swapped for UTC. Here the fake clock
        answers two different hours to the two kinds of calls, and the
        folder name must carry the local one.
        """

        vero = pipeline_jobs.datetime

        class DueOreDiDifferenza(vero):
            @classmethod
            def now(cls, tz=None):  # type: ignore[override]
                if tz is None:
                    return vero(2026, 8, 20, 12, 0, 0)
                return vero(2026, 8, 20, 10, 0, 0, tzinfo=tz)

        pipeline_jobs.datetime = DueOreDiDifferenza
        try:
            esito = self.esegui(EsecutoreFinto())
        finally:
            pipeline_jobs.datetime = vero

        cartella = Path(esito["cartella"])
        self.assertTrue(cartella.is_dir())
        self.assertEqual(cartella.name, "2026-08-20_1200")


class OgniPassoPortaIlSuoTetto(BancoPipeline):
    """The orchestrator sets a per-step timeout, and each phase has its own.

    With the timeout table defined but nothing actually reading it, the
    pipeline would go back to waiting forever with nothing to say so.
    """

    def test_gli_otto_comandi_ricevono_il_tetto_della_loro_fase(self) -> None:
        esecutore = EsecutoreFinto()
        self.esegui(esecutore)

        self.assertEqual(len(esecutore.tetti), len(esecutore.chiamati))
        for script, tetto in esecutore.tetti.items():
            self.assertIsNotNone(tetto, script)
            self.assertGreater(tetto, 0, script)
        # The AI phase measures 105s: it's the only one that doesn't fit
        # the other phases' timeout, and the only one declared separately.
        self.assertEqual(
            esecutore.tetti["valuta_shortlist.py"], pipeline_jobs.TETTO_DELLE_FASI["VALUTAZIONE_AI"]
        )
        self.assertEqual(
            esecutore.tetti["inspect_sources.py"], pipeline_jobs.TETTO_PREDEFINITO_DI_FASE
        )


class UnApprendimentoNonRiuscitoNonButtaViaIlConfronto(BancoPipeline):
    """A failed schema-learning attempt must not discard an otherwise valid comparison.

    On a price list with no header row, the guided mapping declares
    columns by position, `impara_adattatore` refuses to derive a
    header-based fingerprint from it, and the resulting
    `APPRENDIMENTO_SCHEMA_NON_RIUSCITO` stop must not discard a comparison
    that was already built and correct. Retrying doesn't help either way —
    the manual decision stays in place and the run takes the same path
    again — so neither closing nor reopening the app would resolve anything.

    The other half of this test suite covers the flip side: the manual
    mapping bridge is only removed for adapters that were actually
    learned, otherwise that price list would stop opening the following
    week.
    """

    def gestore_con_una_decisione(self) -> PipelineJobManager:
        gestore = self.gestore(EsecutoreFinto())
        corsa = pipeline_jobs._Corsa(
            cartella=self.dati / "esecuzioni" / "r1", registro_artefatti={},
        )
        corsa.cartella.mkdir(parents=True, exist_ok=True)
        corsa.manifest_path = corsa.cartella / "manifest.json"
        corsa.decisioni_da_imparare = ["offerte.xlsx"]
        self.corsa = corsa
        return gestore

    def rapporto(self, imparati: list[str], saltati: list[tuple[str, str]]) -> dict:
        return {
            "imparati": [{"file": nome} for nome in imparati],
            "saltati": [{"file": nome, "motivo": motivo} for nome, motivo in saltati],
        }

    def con_esiti(self, gestore: PipelineJobManager, esiti: list[dict]) -> None:
        """Makes `impara_adattatore` respond with the given reports, in order."""

        rimasti = list(esiti)

        def finto(corsa, fase, argomenti, *, script, uscite_ammesse=(0,), **extra):
            uscita = argomenti[argomenti.index("--output") + 1]
            self._scrivi(Path(uscita), rimasti.pop(0))
            return RisultatoComando(uscita=0, stdout="", stderr="")

        gestore._esegui = finto  # type: ignore[method-assign]

    def test_il_confronto_resta_e_l_utente_lo_sa(self) -> None:
        gestore = self.gestore_con_una_decisione()
        motivo = ("la mappatura non dichiara nessuna colonna per nome: un'impronta per "
                  "intestazioni ha bisogno di almeno un nome da ritrovare nel documento")
        self.con_esiti(gestore, [self.rapporto([], [("offerte.xlsx", f"Rifiutato: {motivo}")])])

        gestore._impara_schemi_confermati(self.corsa, "COSTRUZIONE")

        avvisi = gestore.stato().get("avvisi") or []
        codici = [voce.get("code") for voce in avvisi]
        self.assertIn("SCHEMA_NON_MEMORIZZATO", codici)
        detto = next(voce for voce in avvisi if voce.get("code") == "SCHEMA_NON_MEMORIZZATO")
        self.assertIn("offerte.xlsx", detto["title"])
        self.assertIn("non è andato perso", detto["message"])
        self.assertIn(motivo, detto["message"])
        self.assertEqual(detto["severity"], "warning")

    def test_il_ponte_a_mano_non_si_toglie_a_chi_non_e_stato_imparato(self) -> None:
        """Removing it would leave a price list unable to open the following week."""

        percorso = self.dati / pipeline_jobs.NOME_DECISIONI_MANUALI
        self._scrivi(percorso, {"decisions": [{"file_name": "offerte.xlsx", "state": "NUOVO_FORNITORE"}]})
        gestore = self.gestore_con_una_decisione()
        self.con_esiti(gestore, [self.rapporto([], [("offerte.xlsx", "Rifiutato: niente nomi")])])

        gestore._impara_schemi_confermati(self.corsa, "COSTRUZIONE")

        rimaste = json.loads(percorso.read_text(encoding="utf-8"))["decisions"]
        self.assertEqual([voce["file_name"] for voce in rimaste], ["offerte.xlsx"])

    def test_nemmeno_un_guasto_vero_butta_via_il_confronto(self) -> None:
        """A clean rejection was already handled as a warning; an actual crash exited 1 and stopped everything.

        Real cause on a real machine: a read-only `app/data/` folder, or
        the registry file locked open by antivirus software while being read.
        """

        gestore = self.gestore_con_una_decisione()

        def esplode(*_argomenti, **_extra):
            raise PermissionError("adattatori_imparati.json.tmp")

        gestore._esegui = esplode  # type: ignore[method-assign]

        gestore._impara_schemi_confermati(self.corsa, "COSTRUZIONE")

        avvisi = gestore.stato().get("avvisi") or []
        detto = next(voce for voce in avvisi if voce.get("code") == "SCHEMA_NON_MEMORIZZATO")
        self.assertIn("offerte.xlsx", detto["title"])
        self.assertIn("PermissionError", detto["message"])

    def test_un_documento_che_finisce_a_un_altro_fornitore_non_si_liquida_con_niente_da_fare(self) -> None:
        """Next week that price list would be read as belonging to a different supplier."""

        gestore = self.gestore_con_una_decisione()
        motivo = ("Rifiutato: con l'adattatore appena scritto il documento risulta SCHEMA_NOTO "
                  "«betulla_v1» invece di SCHEMA_NOTO «offerte_v1»: impronta per intestazioni")
        self.con_esiti(gestore, [self.rapporto([], [("offerte.xlsx", motivo)])])

        gestore._impara_schemi_confermati(self.corsa, "COSTRUZIONE")

        detto = next(voce for voce in (gestore.stato().get("avvisi") or [])
                     if voce.get("code") == "SCHEMA_NON_MEMORIZZATO")
        self.assertEqual(detto["severity"], "error")
        self.assertIn("BETULLA", detto["message"])
        self.assertNotIn("non c'è niente da fare", detto["message"])

    def test_quando_l_apprendimento_riesce_il_ponte_si_toglie(self) -> None:
        percorso = self.dati / pipeline_jobs.NOME_DECISIONI_MANUALI
        self._scrivi(percorso, {"decisions": [{"file_name": "offerte.xlsx", "state": "NUOVO_FORNITORE"}]})
        gestore = self.gestore_con_una_decisione()
        self.con_esiti(gestore, [self.rapporto(["offerte.xlsx"], []), self.rapporto(["offerte.xlsx"], [])])

        gestore._impara_schemi_confermati(self.corsa, "COSTRUZIONE")

        # With no entries left, the manual bridge file disappears entirely:
        # this is how `_fase_riconoscimento` stops reporting "covered by a
        # manual decision".
        self.assertFalse(percorso.exists())
        codici = [voce.get("code") for voce in (gestore.stato().get("avvisi") or [])]
        self.assertNotIn("SCHEMA_NON_MEMORIZZATO", codici)



class LeCartelleDiLavoroNonCresconoPerSempre(BancoPipeline):
    """Old run folders must be pruned automatically.

    Measured at about 13 MB each on the real comparison: at one recompute
    a week that's roughly 700 MB a year on the store PC, more counting
    retries. The real cost isn't disk space, it's that a full disk opens
    up exactly the failure modes this test file exists to prevent.
    """

    def cartelle(self) -> list[str]:
        return sorted(voce.name for voce in (self.dati / "esecuzioni").iterdir() if voce.is_dir())

    def finte(self, *nomi: str) -> None:
        for nome in nomi:
            cartella = self.dati / "esecuzioni" / nome
            cartella.mkdir(parents=True, exist_ok=True)
            (cartella / "esecuzione.json").write_text("{}", encoding="utf-8")

    def test_si_tengono_le_ultime_cinque(self) -> None:
        self.finte("2026-01-01_0900", "2026-02-01_0900", "2026-03-01_0900", "2026-04-01_0900",
                   "2026-05-01_0900", "2026-06-01_0900", "2026-07-01_0900")
        gestore = self.gestore(EsecutoreFinto())
        gestore.avvia()
        esito = gestore.attendi(timeout=30)
        self.assertEqual(esito["stato"], pipeline_jobs.COMPLETATO, esito.get("messaggio"))

        rimaste = self.cartelle()
        # The oldest ones are gone...
        for sparita in ("2026-01-01_0900", "2026-02-01_0900", "2026-03-01_0900"):
            self.assertNotIn(sparita, rimaste)
        # ...and the most recent ones are still there, plus the one just created.
        self.assertIn("2026-07-01_0900", rimaste)
        self.assertIn(esito["runId"], rimaste)
        self.assertLessEqual(len(rimaste), pipeline_jobs.ESECUZIONI_DA_TENERE + 1)

    def test_la_cartella_del_confronto_vivo_non_si_tocca_mai(self) -> None:
        """This is the folder the page reopens to explain which columns it read.

        The live comparison declares which run it came from: if that
        folder is pruned for being old, "Quali colonne leggo" (which columns
        do I read) and the guided mapping would answer "that comparison's
        folder is gone" for a comparison currently being viewed.
        """

        # This test fixture's live comparison comes from a run named
        # "vecchia", which alphabetically sorts as the oldest of all.
        self._scrivi(self.review, {**CONFRONTO_PRECEDENTE,
                                   "run": {"id": "vecchia", "status": "ready", "pipelineRunId": "vecchia"}})
        self.finte("vecchia", "2026-01-01_0900", "2026-02-01_0900", "2026-03-01_0900",
                   "2026-04-01_0900", "2026-05-01_0900", "2026-06-01_0900", "2026-07-01_0900")

        gestore = self.gestore(EsecutoreFinto())
        gestore.avvia()
        gestore.attendi(timeout=30)

        self.assertIn("vecchia", self.cartelle())

    def test_una_cartella_che_non_si_cancella_non_ferma_il_ricalcolo(self) -> None:
        """Freeing up disk space is a convenience, not a precondition for running."""

        self.finte("2026-01-01_0900", "2026-02-01_0900", "2026-03-01_0900", "2026-04-01_0900",
                   "2026-05-01_0900", "2026-06-01_0900", "2026-07-01_0900")
        originale = pipeline_jobs.shutil.rmtree

        def non_si_cancella(percorso, *resto, **extra):
            raise OSError("in uso da un altro programma")

        pipeline_jobs.shutil.rmtree = non_si_cancella
        self.addCleanup(setattr, pipeline_jobs.shutil, "rmtree", originale)
        gestore = self.gestore(EsecutoreFinto())
        gestore.avvia()
        esito = gestore.attendi(timeout=30)
        pipeline_jobs.shutil.rmtree = originale

        self.assertEqual(esito["stato"], pipeline_jobs.COMPLETATO, esito.get("messaggio"))
        self.assertIn("2026-01-01_0900", self.cartelle())



class UnoStatoCheNonSiScriveNonFerma(BancoPipeline):
    """A pipeline that can't persist its own status must still finish cleanly.

    `pipeline_status.json` is rewritten on every phase. If the write
    fails (full disk, read-only folder, antivirus locking the temp file),
    the failure must not originate a second exception inside `_lavora`'s
    own error handler when it calls `_segna_fase` again to record
    `ERRORE`: a retry of the same failing write would escape before the
    status could ever become `ERRORE`, the background thread would die,
    the lock would release, and the page would be left with a progress bar
    stuck on "in progress" forever, with recompute and uploads both disabled.
    """

    def senza_scrittura(self):
        originale = pipeline_jobs.scrivi_json

        def non_si_scrive(percorso, valore):
            if Path(percorso).name == pipeline_jobs.NOME_STATO:
                raise OSError("disco pieno")
            return originale(percorso, valore)

        pipeline_jobs.scrivi_json = non_si_scrive
        self.addCleanup(setattr, pipeline_jobs, "scrivi_json", originale)

    def test_la_run_arriva_in_fondo_lo_stesso(self) -> None:
        self.senza_scrittura()
        gestore = self.gestore(EsecutoreFinto())
        with contextlib.redirect_stdout(io.StringIO()) as console:
            gestore.avvia()
            esito = gestore.attendi(timeout=30)

        self.assertEqual(esito["stato"], pipeline_jobs.COMPLETATO, esito.get("messaggio"))
        # And reported once, not spammed: a console filled with the same
        # line repeated isn't something anyone reads.
        self.assertEqual(console.getvalue().count("[AVVISO] Non riesco a scrivere"), 1)

    def test_una_run_che_fallisce_arriva_a_dirlo(self) -> None:
        """The combination that can hang the page: a real failure plus an unwritable status file."""

        self.senza_scrittura()

        def esplode(comando, cartella, avanzamento, **extra):
            raise RuntimeError("il passo si è rotto")

        gestore = self.gestore(esplode)
        with contextlib.redirect_stdout(io.StringIO()):
            gestore.avvia()
            esito = gestore.attendi(timeout=30)

        self.assertEqual(esito["stato"], pipeline_jobs.ERRORE)
        self.assertNotEqual(esito["stato"], pipeline_jobs.IN_CORSO)
        # And the lock is released: the user can retry without closing anything.
        self.assertTrue(gestore.lucchetto_lavori.acquire(blocking=False))
        gestore.lucchetto_lavori.release()


if __name__ == "__main__":
    unittest.main()
