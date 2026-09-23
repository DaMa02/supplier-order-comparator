"""L'orchestratore della catena (Fase 6c).

Quattro cose valgono da sole l'intero file, e sono quelle in cui una
implementazione plausibile sbaglia **in silenzio**:

1. **Se una fase fallisce, il confronto precedente resta intatto.**  E' la
   promessa su cui poggia il pulsante: chi lo preme non deve poterci perdere il
   lavoro della settimana.  Qui si prova su ogni singola fase, una per una.
2. **Un artefatto che il passo dice di aver scritto e che non c'e' ferma la
   catena.**  E' il difetto storico numero 1 del piano — «un passo saltato
   lascia in giro il file di ieri» — e la difesa e' un registro delle impronte,
   non una promessa.
3. **L'attivazione ha delle precondizioni, e sono numeri.**  Zero prodotti,
   zero fornitori, o una contabilita' dei casi che non torna: nessuna delle tre
   deve poter attivare un confronto.
4. **Il controllo con la volta prima avvisa e non ferma.**  Un listino che
   dimezza le righe o raddoppia i prezzi produce un avviso in cima alla pagina e
   una run che arriva in fondo.

Niente rete e niente sottoprocessi: l'esecutore e' iniettato e simula i nove
comandi scrivendo gli artefatti che scriverebbero davvero.  Il collaudo con i
comandi veri e' la prova di accettazione, e sta nel piano.
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


# ⚠ Il codice a barre non e' decorazione: dal 15 agosto 2026 e' lui a dire se
# la riga 1 del confronto nuovo porta lo stesso articolo della riga 1 di prima.
# Un fixture che cambia nome allo stesso prodotto senza cambiargli l'EAN
# descrive un prodotto sostituito, e le decisioni prese su di lui si scollegano.
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
    """Simula i nove comandi scrivendo gli artefatti veri, senza sottoprocessi.

    Ogni comando puo' essere alterato dal test: `uscite` cambia il codice
    d'uscita, `salta` gli impedisce di scrivere i propri file, `esplode` lo fa
    fallire.  E' cosi' che si provano i modi di fallire senza dover rompere gli
    script veri.
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
        # Il tetto oltre il quale il passo si considera impiantato: qui non
        # serve a niente, ma se l'orchestratore smettesse di passarlo nessun
        # collaudo se ne accorgerebbe — e il tetto non ci sarebbe piu'.
        self.tetti[script] = timeout_secondi
        # Gli argomenti veri di ogni passo: `chiamati` dice **che** un comando e'
        # stato lanciato, non **con che cosa**, e un ingresso che l'orchestratore
        # dimentica di passare non si vedrebbe da nessuna parte.
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
        # Le fasi sono nove: l'attivazione non e' un comando, e' l'unico passo
        # che l'orchestratore fa da se' perche' e' quello che tocca il
        # confronto vivo.
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
        # Il registro delle impronte: e' quello che rende «l'ho scritto» una
        # cosa verificabile invece di una promessa.
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
        """Senza, la compilazione di Cipresso direbbe «manca la mappatura».

        E' il modo con cui la fermata numero uno «si automatizza da se'»: la
        decisione generata deve contenere tutto quello che conteneva quella
        scritta a mano, colonna d'ordine compresa.
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
    """La promessa del pulsante: una run che fallisce non costa il confronto."""

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
        """Una mappatura confermata nella pagina, legata (o no) a un documento."""

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
        # `profilo()` costruisce l'impronta "000...0": è quella confermata.
        self._decisione_confermata(impronta="0" * 64)

        esito = self.esegui(self._con_misterioso())

        self.assertEqual(esito["stato"], pipeline_jobs.COMPLETATO, esito.get("messaggio"))
        self.assertNotIn("DECISIONE_MANUALE_SCARTATA", {avviso["code"] for avviso in esito["avvisi"]})

    def test_un_altro_documento_con_lo_stesso_nome_non_eredita_le_colonne(self) -> None:
        """Il caso vero: si elimina un listino configurato male e si ricarica.

        Il file nuovo si chiama come quello di prima. Riapplicargli la mappatura
        vecchia vuol dire leggere le colonne sbagliate — cioè scrivere le
        quantità sulle righe sbagliate — senza che niente lo dica.
        """

        self._decisione_confermata(impronta="a" * 64)

        esito = self.esegui(self._con_misterioso())

        self.assertIn("DECISIONE_MANUALE_SCARTATA", {avviso["code"] for avviso in esito["avvisi"]})
        avviso = next(
            voce for voce in esito["avvisi"] if voce["code"] == "DECISIONE_MANUALE_SCARTATA"
        )
        self.assertIn("misterioso.xlsx", avviso["title"])
        self.assertIn("un altro documento con lo stesso nome", avviso["message"])
        # E la decisione non copre più il documento: la catena si ferma a
        # chiedere le colonne, invece di inventarsele.
        self.assertEqual(esito["fermata"]["code"], "SCHEMA_SCONOSCIUTO")
        self.assertEqual(esito["fermata"]["documenti"], ["misterioso.xlsx"])

    def test_una_decisione_scritta_a_mano_senza_impronta_continua_a_valere(self) -> None:
        """È la via d'uscita documentata: toglierla toglierebbe il rimedio."""

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
        """`prepare_manifest_sources` gira anche su un manifest bocciato.

        Non legge `manifest_validation.json`: se non si ferma qui, due fornitori
        con lo stesso identificativo o un master mancante diventano un confronto
        sbagliato invece di un messaggio.
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
        """E' la terza fermata (13 agosto 2026, su delega): deve dire che fare.

        Un fornitore caricato apposta non puo' sparire dal confronto con un
        semplice avviso — gli ordini si farebbero senza di lui.  E come le
        altre due fermate, il messaggio deve portare il rimedio, non solo la
        diagnosi.
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
        """La tautologia della 6b, spostata di un livello e chiusa qui.

        Se i candidati sono mille e la fase AI dichiara di averne ricevuti
        cinquecento, cinquecento prodotti sono spariti dalla valutazione e ogni
        singolo passo esce con zero.  E' il numero che se ne accorge.
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
        """Due minuti dopo nessuno guarda più la barra di avanzamento."""

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
    """Bloccante 3 della verifica del 12 agosto 2026.

    Il prezzo puntato sulla colonna sbagliata dava 242 prodotti a 0,00 € con
    confidenza CERTA e un piano d'ordine da 0,00 €, mentre `dati/audit.json`
    scriveva gia' `usable: 0` per quel fornitore.  Zero avvisi: il controllo
    sui prezzi confrontava solo «con la volta prima» — quindi alla prima run
    non guardava niente — e dalla seconda saltava proprio il crollo a nulla,
    perche' una mediana assente usciva da `if not prima or adesso is None`.

    Un fornitore a zero non sparisce dal confronto: **vince**, su ogni riga.
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
        """La forma decisa: si vede come un errore, ma «avvisa e non ferma».

        `blocking: true` chiuderebbe la compilazione, e le fermate sono
        soltanto quelle sull'ingresso; `severity: "warning"` lo farebbe
        leggere di sfuggita in mezzo agli altri.
        """

        esito = self.esegui(EsecutoreFinto(audit=self._audit(usable=0, median=None)))

        avviso = next(voce for voce in esito["avvisi"] if voce["code"] == "PREZZI_A_ZERO")
        self.assertEqual(avviso["severity"], "error")
        self.assertIs(avviso["blocking"], False)

    def test_l_avviso_entra_anche_nel_documento(self) -> None:
        """Due minuti dopo nessuno guarda più la barra di avanzamento."""

        self.esegui(EsecutoreFinto(audit=self._audit(usable=0, median=None)))

        confronto = json.loads(self.review.read_bytes())
        self.assertIn("PREZZI_A_ZERO", {voce["code"] for voce in confronto["warnings"]})

    def test_anche_alla_seconda_run_con_le_stesse_righe(self) -> None:
        """È il caso che passava: stesse righe, prezzi crollati, zero parole."""

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
        """Un avviso che parte sempre è un avviso che nessuno legge più."""

        esito = self.esegui(EsecutoreFinto())

        self.assertNotIn("PREZZI_A_ZERO", {voce["code"] for voce in esito["avvisi"]})
        self.assertNotIn("fornitoriSenzaPrezzi", esito["numeri"])

    def test_un_audit_senza_riepilogo_prezzi_non_grida_al_lupo(self) -> None:
        """«Non misurato» non e' «misurato zero».

        Un audit di formato piu' vecchio, senza `price_summary`, accendeva
        PREZZI_A_ZERO su ogni fornitore della run: un avviso rosso per
        fornitore su una cosa che nessuno ha misurato e' rumore che spegne
        l'avviso vero (revisione avversariale del 13 agosto 2026).
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
        """«4660 prezzi maggiori di zero» e «mediana zero» non stanno insieme."""

        esito = self.esegui(EsecutoreFinto(audit=self._audit(usable=4660, median="0.0000")))

        avviso = next(voce for voce in esito["avvisi"] if voce["code"] == "PREZZI_A_ZERO")
        self.assertNotIn("maggiori di zero", avviso["message"])
        self.assertIn("prezzo mediano dichiarato", avviso["message"])


class ChiNonSiPuoCompilareSiDice(BancoPipeline):
    """Satellite 1: un fornitore nel confronto per cui non nascera' nessuna copia.

    Misurato il 12 agosto 2026: 102 prodotti assegnati a ACERO e
    avvertimenti vuoti, perche' i compilabili erano una tupla nel codice del
    lanciatore e chi non c'era spariva prima di essere nominato.
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
        """Basta dichiarare `order_write` perche' l'avviso taccia: nessun codice.

        E' la prova che la compilabilita' e' dichiarativa — cioe' che un
        fornitore imparato la settimana prossima potra' diventare compilabile
        senza che nessuno tocchi il programma.
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
    """Dal 5 settembre 2026 lo spedito piu' recente vince, e la catena lo
    applica al file **prima** di profilare qualunque documento: e' nella
    profilazione che `registro.riconosci` sceglie l'adattatore, e una voce
    imparata vecchia si prenderebbe il listino al posto di quella spedita.
    L'avviso esce una volta, perche' la voce esce dal file.
    """

    def test_la_voce_superata_esce_dal_file_e_l_utente_lo_sa(self) -> None:
        registro_di_prova = self.radice / "adapters.json"
        documento = json.loads(
            (Path(pipeline_jobs.REFERENCES_DIR) / "adapters.json").read_text(encoding="utf-8"))
        self._scrivi(registro_di_prova, documento)
        imparato = registro_di_prova.with_name("adattatori_imparati.json")
        self._scrivi(imparato, {"schema_version": 1, "adapters": [
            # Senza timbro sopra una spedita: e' la voce del 21 agosto sul PC
            # del negozio, quella che copriva BETULLA.
            {"id": "betulla_v1__locale", "supplier_id": "betulla", "display_name": "BETULLA",
             "kind": "supplier", "order_write": {"sheet": "FIRST", "data_start_row": 2,
                                                 "order_column": "H"}},
            # Imparata da zero: non la tocca nessuno.
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
        # La seconda volta: niente da spostare, niente da dire.
        gestore._metti_da_parte_gli_adattatori_superati()
        self.assertEqual(
            len([v for v in gestore.stato().get("avvisi") or [] if v.get("code") == "ADATTATORE_MESSO_DA_PARTE"]),
            1,
        )


class LaCausaDellaMancataCopiaSiDiceInCatena(BancoPipeline):
    """I-1 della revisione avversariale del 13 agosto 2026.

    Un fornitore DICHIARATO dal registro puo' comunque restare senza copia:
    l'intestazione della colonna d'ordine e' cambiata, il foglio e' sparito,
    le righe dichiarate stanno fuori dal documento.  Quella famiglia di
    avvisi moriva dentro il messaggio di `prepare_writer_config`, che il
    server butta via quando la scrittura riesce: BETULLA con l'intestazione
    cambiata spariva dalla configurazione senza una parola in pagina, e lo si
    scopriva alla compilazione con una frase che non diceva la causa.
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
        """Due minuti dopo nessuno guarda più la barra di avanzamento."""

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
        """«Il registro non dichiara» davanti a un JSON rotto è una bugia."""

        registro_rotto = self.radice / "adapters.json"
        registro_rotto.write_text("{ rotto", encoding="utf-8")

        esito = self._esegui_con(registro_rotto, self._confronto(self._listino("ORDINE")))

        self.assertEqual(esito["stato"], pipeline_jobs.COMPLETATO, esito.get("messaggio"))
        codici = [voce["code"] for voce in esito["avvisi"]]
        self.assertEqual(codici.count("REGISTRO_ILLEGGIBILE"), 1, esito["avvisi"])
        self.assertNotIn("FORNITORE_NON_COMPILABILE", codici)


class LaFermataApreLaConfigurazioneGuidata(BancoPipeline):
    """Uno schema nuovo si sistema dalla pagina, senza file o comandi."""

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
        # E non solo nello stato del job: la pagina lo rilegge dal documento.
        confronto = json.loads(self.review.read_bytes())
        self.assertIn(
            "SCELTE_NON_PIU_VALIDE",
            {avviso["code"] for avviso in confronto.get("warnings") or []},
        )

    # -- la conferma vale per l'articolo che l'utente ha guardato -----------

    OFFERTA_DI_PRIMA = {
        "supplierId": "betulla",
        "available": True,
        "ean": "8000000000011",
        "supplierCode": "C-11",
        "description": "PASTA MEZZE MANICHE 500 G",
        "requiresConfirmation": True,
    }

    def _stato_confermato(self, offerta: dict) -> None:
        """Lo stato come lo lascia `save_state` dopo una conferma dell'utente."""

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
        # Stesso prodotto della run di prima — stesso codice a barre — con
        # un'offerta che puo' essere cambiata: e' il caso di cui parlano queste
        # prove.  Il prodotto sostituito ha una prova sua.
        return {
            "run": {"id": "nuova"}, "files": [], "suppliers": [{"id": "betulla", "name": "BETULLA"}],
            "products": [{"id": "product:1", "name": "Uno", "ean": "8000000000001", "offers": [offerta]}],
            "warnings": [],
        }

    def test_una_conferma_non_copre_un_articolo_diverso_della_settimana_dopo(self) -> None:
        """Il fornitore ha ancora un'offerta, ma è un'altra riga del listino.

        Niente veniva azzerato — l'offerta c'è — e la casella «confermo che è lo
        stesso articolo» restava spuntata su un articolo che l'utente non aveva
        mai visto. La compilazione passava senza una parola.
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
        # La quantità NON si azzera: il prodotto va ancora ordinato, è la
        # conferma che va rifatta.
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
        """Rifare la domanda a ogni ricalcolo insegna a spuntare senza leggere."""

        self._stato_confermato(self.OFFERTA_DI_PRIMA)

        esito = self.esegui(EsecutoreFinto(confronto=self._confronto_con(dict(self.OFFERTA_DI_PRIMA))))

        stato = json.loads(self.stato.read_bytes())
        self.assertTrue(stato["products"][0]["confirmed"])
        self.assertNotIn("CONFERME_SCADUTE", {avviso["code"] for avviso in esito["avvisi"]})

    def test_un_prezzo_nuovo_sullo_stesso_articolo_non_fa_scadere_niente(self) -> None:
        """L'impronta è l'identità dell'articolo, non le sue condizioni.

        I listini cambiano prezzo ogni settimana: se bastasse quello, la domanda
        tornerebbe su ogni riga e nessuno la leggerebbe più.
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
        """Stato salvato prima di questa regola: si fallisce chiuso."""

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

    # -- le quantita' del gestionale seguono il gestionale -------------------
    #
    # ⚠ Il difetto del 15 agosto 2026, con le parole di chi lo ha subito:
    # «ordine2 non veniva usato davvero: nonostante alcuni prodotti avessero
    # quantita' diverse, mostrava tutti 0».  Il confronto nuovo era giusto; a
    # coprirlo era la decisione salvata sull'elenco di prima.

    GESTIONALE_DI_PRIMA = [{"role": "master", "name": "ordine.xlsx", "sourceSha256": "a" * 64}]
    GESTIONALE_NUOVO = [{"role": "master", "name": "ordine2.xlsx", "sourceSha256": "b" * 64}]

    def _elenco_di_prima(self, documenti: list[dict], *, ean: str = "8000000000001") -> None:
        """Il confronto vivo: quello che c'era prima di questo ricalcolo."""

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
        """Il gestionale è un altro documento: i colli sono i suoi."""

        self._elenco_di_prima(self.GESTIONALE_DI_PRIMA)
        self._decisione(quantity=0)

        esito = self.esegui(EsecutoreFinto(
            confronto=self._confronto_del_gestionale(self.GESTIONALE_NUOVO, 3),
        ))

        stato = json.loads(self.stato.read_bytes())
        self.assertEqual(stato["products"][0]["quantity"], 3)
        # ⚠ E NESSUN avviso: che la quantità venga dall'elenco è la regola
        # normale, non una notizia. «Non riempire tutto di notifiche, popup, e
        # roba da paranoici logorroici» (15 agosto 2026).
        self.assertNotIn("QUANTITA_RIPRESE_DAL_GESTIONALE", {voce["code"] for voce in esito["avvisi"]})

    def test_le_quantita_le_decide_il_gestionale_anche_a_elenco_uguale(self) -> None:
        """«La quantità deve prenderla dal gestionale e stop» (15 agosto 2026).

        Nessuna condizione, nemmeno «l'elenco è cambiato»: quello che viene
        dall'elenco si rilegge dall'elenco a ogni ricalcolo. ⚠ Conseguenza
        voluta: «Azzera le quantità predefinite» vale fino al ricalcolo dopo.
        """

        self._elenco_di_prima(self.GESTIONALE_DI_PRIMA)
        self._decisione(quantity=0)

        esito = self.esegui(EsecutoreFinto(
            confronto=self._confronto_del_gestionale(self.GESTIONALE_DI_PRIMA, 3),
        ))

        stato = json.loads(self.stato.read_bytes())
        self.assertEqual(stato["products"][0]["quantity"], 3)
        self.assertNotIn("QUANTITA_RIPRESE_DAL_GESTIONALE", {voce["code"] for voce in esito["avvisi"]})
        # Il numero non si perde: resta nel riepilogo della run, per chi lo cerca.
        self.assertEqual(esito["numeri"].get("quantitaRiprese"), 1)

    def test_le_quantita_scritte_da_te_non_le_tocca_nessuno(self) -> None:
        """`utente` vuol dire che quel numero l'ha scritto lui: è suo."""

        self._elenco_di_prima(self.GESTIONALE_DI_PRIMA)
        self._decisione(quantity=7, quantitySource="utente")

        self.esegui(EsecutoreFinto(
            confronto=self._confronto_del_gestionale(self.GESTIONALE_NUOVO, 3),
        ))

        stato = json.loads(self.stato.read_bytes())
        self.assertEqual(stato["products"][0]["quantity"], 7)

    def test_la_stessa_riga_con_un_altro_articolo_perde_le_sue_decisioni(self) -> None:
        """Gli identificativi sono numeri di riga: con un elenco nuovo la riga 1
        e' un altro prodotto, e la scelta del fornitore non parla di lui."""

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
        # E la pagina lo rilegge dal documento, non solo dallo stato del job.
        confronto = json.loads(self.review.read_bytes())
        self.assertIn(
            "DECISIONI_SCOLLEGATE",
            {voce.get("code") for voce in confronto.get("warnings") or []},
        )

    def test_un_prodotto_aggiunto_a_mano_non_perde_la_sua_quantita(self) -> None:
        """I prodotti aggiunti a mano arrivano dallo stato, non dal confronto.

        Cercarli solo fra i prodotti del confronto li dichiarava «spariti» a
        ogni ricalcolo: la quantita' scritta su di loro tornava a zero, con la
        spiegazione sbagliata («il listino nuovo non ha piu' l'offerta»).
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

    # -- il fornitore salvato non regge piu': due casi, non uno --------------
    #
    # ⚠ Fino al 16 agosto 2026 erano lo stesso caso e finivano tutti e due a
    # zero.  La domanda giusta non e' «il fornitore scelto c'e' ancora», e'
    # «qualcuno puo' servirlo»: se qualcuno puo', la quantita' torna a zero
    # perche' su quale offerta metterla e' una scelta dell'utente; se non puo'
    # nessuno, non c'e' niente da scegliere e azzerare butterebbe via l'unica
    # cosa nota di quella riga — quanti ne servono.

    def _confronto_con_offerta(self, *offerte: dict) -> dict:
        return {
            "run": {"id": "nuova"}, "files": [], "suppliers": [{"id": "betulla"}, {"id": "larice"}],
            "products": [{"id": "product:1", "offers": list(offerte)}],
            "warnings": [],
        }

    def test_con_un_altro_fornitore_disponibile_la_quantita_torna_a_zero(self) -> None:
        """Comportamento invariato: qui una scelta c'è, e la fa l'utente."""

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
        """La difesa nuova: e' un prodotto da reperire, non un errore.

        Se questa prova torna a leggere 0, al ricalcolo successivo il prodotto
        esce dall'elenco «Prodotti da reperire» — ci entra solo chi ha una
        quantita' > 0 — e sparisce senza che niente lo dica.
        """

        esecutore = EsecutoreFinto(confronto=self._confronto_con_offerta(
            {"supplierId": "betulla", "available": False},
        ))

        esito = self.esegui(esecutore)

        stato = json.loads(self.stato.read_bytes())
        self.assertEqual(stato["products"][0]["quantity"], 4)
        self.assertEqual(stato["products"][0]["selectedSupplierId"], "")
        self.assertFalse(stato["products"][0]["confirmed"])
        # ⚠ E nessun avviso: non e' stato perso niente. «SCELTE_NON_PIU_VALIDE»
        # direbbe che la quantita' e' tornata a zero, e sarebbe falso.
        self.assertNotIn("SCELTE_NON_PIU_VALIDE", {voce["code"] for voce in esito["avvisi"]})

    def test_il_fornitore_svuotato_finisce_davvero_sul_disco(self) -> None:
        """Tenere la quantita' non deve diventare «non riscrivere niente».

        Lo stato si riscrive solo se qualcosa e' cambiato, e questo caso non
        alza nessuno dei contatori che producono un avviso: senza una ragione
        propria per riscriverlo, il fornitore sparito resterebbe sul disco e la
        compilazione seguente ordinerebbe da chi non ce l'ha.
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
        """Niente quantita' e nessun fornitore: non c'e' niente da ripulire, e
        la decisione dev'esserci ancora tale e quale."""

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
        """`PUT /api/state` rifiuta uno snapshot di un'altra run."""

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
        """⚠ Il doppio deve accettare `timeout_secondi`, e non e' un dettaglio.

        Fino al 20 agosto la sua firma era rimasta indietro: `_esegui` passa il
        tetto **per nome**, il doppio sollevava `TypeError` prima ancora di
        cominciare, la run moriva in millisecondi e questa prova passava solo
        perche' il filo principale vinceva la corsa verso il secondo `avvia()`.
        Verde oggi, rossa a caso domani, e nel frattempo «un lavoro per volta»
        non era piu' coperto da niente. Il `partito` qui sotto lo dichiara: il
        secondo avvio si prova **mentre** il primo comando sta girando.
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
        """⚠ Il ripiego copriva la sola creazione della cartella.

        Fra `acquire()` e la partenza del filo ci sono altre tre cose, e
        qualunque di loro puo' fallire — qui il filo che non parte, che e' il
        caso vero su una macchina a corto di risorse. Fino al 22 agosto 2026 un
        guasto li' lasciava `lucchetto_lavori` in mano a nessuno: da quel
        momento ogni «Ricalcola» rispondeva 409, `POST /api/spegni` si
        rifiutava di spegnere, e all'utente restava chiudere la finestra a
        forza — il programma inchiodato dal guasto che doveva costare una riga
        d'errore.
        """

        gestore = self.gestore(EsecutoreFinto())
        originale = pipeline_jobs.threading.Thread

        class FiloCheNonParte(originale):
            def start(self):  # noqa: D102 - «can't start new thread», succede
                raise RuntimeError("can't start new thread")

        pipeline_jobs.threading.Thread = FiloCheNonParte
        self.addCleanup(setattr, pipeline_jobs.threading, "Thread", originale)
        with self.assertRaises(RuntimeError):
            gestore.avvia()
        pipeline_jobs.threading.Thread = originale

        # Il lucchetto e' libero: si vede da fuori, provando a prenderlo.
        self.assertTrue(gestore.lucchetto_lavori.acquire(blocking=False),
                        "il lucchetto dei lavori e' rimasto in mano a nessuno")
        gestore.lucchetto_lavori.release()
        # E il programma riparte davvero, che e' la cosa che l'utente vede.
        gestore.avvia()
        esito = gestore.attendi(timeout=30)
        self.assertEqual(esito["stato"], pipeline_jobs.COMPLETATO, esito.get("messaggio"))

    def test_il_lucchetto_torna_libero_anche_se_l_audit_esplode(self) -> None:
        """⚠ Il rilascio non deve dipendere da nient'altro.

        `_scrivi_audit_esecuzione` intercetta il solo `OSError`: qualunque
        altra eccezione — un `TypeError` di `json.dumps`, un `RecursionError`
        su un `deepcopy` — lasciava `lucchetto_lavori` in mano per sempre, e da
        li' 409 a ogni «Ricalcola», niente spegnimento e niente aggiornamento
        del codice. Trovato dalla verifica avversariale del 20 agosto 2026.
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
        # Lo stato della run e' gia' definitivo prima dell'audit: se un giorno
        # qualcuno lo spostasse dopo, la pagina resterebbe con una barra che non
        # avanza piu' e `avvia()` risponderebbe 409 col lucchetto libero.
        self.assertEqual(esito["stato"], pipeline_jobs.COMPLETATO, esito.get("messaggio"))
        # E si dice, invece di uscire come traceback grezzo da un filo: in
        # negozio quello non lo legge nessuno.
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
        """L'ordine dentro l'attivazione e' una difesa, non un dettaglio.

        Fra la sostituzione del confronto vivo e la riconfigurazione della
        scrittura c'e' una finestra, e il processo puo' morirci dentro: il
        computer si spegne, la finestra si chiude.  Se la sostituzione viene
        prima, quello che resta e' il confronto di **adesso** con la
        configurazione della **settimana scorsa** — cioe' le quantita' di oggi
        scritte nelle righe del listino di sette giorni fa, in silenzio.  Se
        viene prima la riconfigurazione, quello che resta e' il confronto di
        prima, che non ha mai fatto male a nessuno.

        La prova guarda il file vivo **nel momento** in cui la riconfigurazione
        viene chiamata: e' l'unico modo di inchiodare un ordine.
        """

        visto_al_momento: list[str] = []

        def riconfigura(_review: dict) -> None:
            confronto_vivo = json.loads(self.review.read_bytes())
            visto_al_momento.append(str((confronto_vivo.get("run") or {}).get("id") or ""))

        esito = self.esegui(EsecutoreFinto(), su_confronto_attivato=riconfigura)
        self.assertEqual(esito["stato"], pipeline_jobs.COMPLETATO, esito.get("messaggio"))
        # Quando la compilazione si riconfigura, il confronto vivo e' ancora
        # quello di prima.
        self.assertEqual(visto_al_momento, ["vecchia"])
        # E alla fine il confronto nuovo c'e' lo stesso.
        vivo = json.loads(self.review.read_bytes())
        self.assertEqual(vivo["run"]["id"], esito["runId"])

    def test_l_avviso_della_riconfigurazione_fallita_entra_nel_documento(self) -> None:
        """Un avviso che vive solo nello stato del job e' un avviso che nessuno legge.

        Nasceva dopo la fotografia degli avvisi, quindi in `review_data.json`
        non ci arrivava mai: due minuti dopo, nella pagina, non restava traccia
        del fatto che la compilazione andava ricontrollata.
        """

        def rompi(_review: dict) -> None:
            raise RuntimeError("Node non c'è")

        self.esegui(EsecutoreFinto(), su_confronto_attivato=rompi)
        confronto = json.loads(self.review.read_bytes())
        codici = {avviso["code"] for avviso in confronto.get("warnings") or []}
        self.assertIn("COMPILAZIONE_DA_RICONFIGURARE", codici)


class IDocumentiDoppi(BancoPipeline):
    def test_di_due_listini_dello_stesso_fornitore_si_usa_il_piu_recente(self) -> None:
        """La cartella dei caricamenti si accumula settimana dopo settimana.

        Due listini BETULLA farebbero fallire il parser con «Fornitore duplicato»,
        che non dice a nessuno che cosa fare.  Si tiene il piu' recente e **si
        dice** quale e' rimasto fuori: uno scarto silenzioso e' peggio.
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
        """Il caso vero: il listino scaduto e' quello copiato per ultimo.

        La scelta guarda la data di modifica del **file**, che con la validita'
        del **listino** non c'entra niente: un BETULLA scaduto ricopiato oggi
        vince su quello valido di ieri.  Il criterio resta (e' l'unico segnale
        che c'e' su tutti i fornitori), ma l'avviso deve dire tre cose, se no
        chi legge capisce «il listino piu' recente» e si fida: chi e' stato
        tenuto, chi e' rimasto fuori, e su che cosa e' stata fatta la scelta.
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
        # Le due cose che il messaggio non diceva: il nome del vincitore...
        self.assertIn("betulla-scaduto.xlsx", avviso["message"])
        self.assertIn("betulla-valido.xlsx", avviso["message"])
        # ...e che la scelta guarda il file, non la validita' del listino.
        self.assertIn("data di modifica", avviso["message"])
        self.assertIn("non sulla validità", avviso["message"])

    def test_con_tre_documenti_l_avviso_nomina_il_vincitore_finale(self) -> None:
        """Chi vinceva a meta' giro non e' detto che vinca alla fine.

        Con tre listini dello stesso fornitore, l'avviso del primo scartato
        nominerebbe il vincitore intermedio: un nome vero, di un file vero, che
        pero' nel confronto non e' entrato.
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
        """Una cartella copiata con robocopy conserva i tempi: capita davvero.

        A parita' di `modified_at` la data non ha deciso niente — vince
        l'ultimo in ordine di elenco — e l'avviso lo deve dire, invece di
        attribuire la scelta a un criterio che non l'ha fatta.
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
    """Con che cosa si confronta la run di oggi.

    Il confronto con «la volta prima» e' quello che fa uscire
    `FORNITORE_SPARITO`, ed e' l'unico avviso che si accorge di un listino che
    non c'e' piu'.  Se si aggancia alla cartella sbagliata non avvisa e basta:
    un fornitore intero esce dal confronto senza una parola.
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
        """Una cartella che sembra una run e che il disco dice piu' recente.

        L'ora del file si sposta a mano: cosi' la prova non dipende da quanto
        ci mette la macchina, e sotto la regola vecchia questa cartella vince
        di sicuro.
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
        """Il caso del ripiego: un confronto attivato prima che `pipelineRunId` esistesse.

        ⚠ Serve ai collaudi della SCANSIONE: senza questa riga passerebbero
        tutti dal percorso veloce (il confronto vivo dichiara chi l'ha
        attivato) e i cancelli su `COMPLETATO` e `iniziatoIl` resterebbero
        senza un collaudo che li tenga fermi — la stessa trappola delle
        fixture a un ramo solo della 6a.
        """

        percorso = self.configurazione().review_path
        confronto = json.loads(percorso.read_text(encoding="utf-8"))
        if isinstance(confronto.get("run"), dict):
            confronto["run"].pop("pipelineRunId", None)
        self._scrivi(percorso, confronto)

    def test_una_run_uccisa_a_meta_non_e_la_volta_prima(self) -> None:
        """`dati/audit.json` a meta' catena, `esecuzione.json` solo alla fine.

        Una run uccisa in mezzo lascia il primo e non il secondo.  Prendendola
        come termine di paragone si confronta oggi con un confronto che non e'
        mai stato attivato, e NOCE — che non c'era nemmeno li' — esce senza
        che nessuno lo dica.
        """

        completa = self._run_completa_con_noce()
        self._il_confronto_vivo_non_dichiara_chi_lo_ha_attivato()
        orfana = self._cartella_intrusa(None)
        self.assertFalse((orfana / pipeline_jobs.NOME_AUDIT_ESECUZIONE).exists())

        esito = self._oggi_senza_noce()

        self.assertEqual(esito["numeri"]["confrontoConLaVoltaPrima"], completa)
        self.assertIn("FORNITORE_SPARITO", {avviso["code"] for avviso in esito["avvisi"]})

    def test_una_run_finita_male_non_e_la_volta_prima(self) -> None:
        """Una run che si è fermata non ha aggiornato niente: non è «la volta prima»."""

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
        """Il ramo che ha preso il posto del ripiego sull'ora del file.

        Prima, un verbale che non diceva quando la run fosse cominciata faceva
        ricadere la scelta sull'`st_mtime` della cartella — cioe' sull'ultima
        volta che qualcuno l'ha toccata, che una copia o un backup spostano in
        avanti quanto vogliono.  Adesso una run che non sa dire quando e'
        cominciata non e' un termine di paragone: si salta.
        """

        completa = self._run_completa_con_noce()
        self._il_confronto_vivo_non_dichiara_chi_lo_ha_attivato()
        self._cartella_intrusa({"stato": pipeline_jobs.COMPLETATO})

        esito = self._oggi_senza_noce()

        self.assertEqual(esito["numeri"]["confrontoConLaVoltaPrima"], completa)
        self.assertIn("FORNITORE_SPARITO", {avviso["code"] for avviso in esito["avvisi"]})

    def test_la_run_completa_piu_recente_resta_quella_giusta(self) -> None:
        """La regola nuova non deve smettere di trovare la volta prima vera."""

        self._run_completa_con_noce()
        seconda = self._run_completa_con_noce()

        esito = self._oggi_senza_noce()

        self.assertEqual(esito["numeri"]["confrontoConLaVoltaPrima"], seconda)
        self.assertIn("FORNITORE_SPARITO", {avviso["code"] for avviso in esito["avvisi"]})

    def test_la_run_che_ha_attivato_e_la_volta_prima_anche_senza_verbale_completo(self) -> None:
        """Il rilievo IMPORTANTE 2 della revisione avversariale del 13 agosto 2026.

        Una run che muore un attimo dopo `os.replace` ha attivato il suo
        confronto, ma il verbale resta a meta'.  Pretendere `COMPLETATO` anche
        da lei la faceva sparire dal paragone: il confronto degradava a «prima
        esecuzione» e `FORNITORE_SPARITO` taceva di nuovo — lo stesso danno che
        la correzione dichiarava di chiudere, raggiunto dall'altro lato.  Il
        confronto vivo dichiara chi l'ha attivato (`run.pipelineRunId`), e
        quella cartella E' la volta prima, qualunque cosa dica il suo verbale.
        """

        self._run_completa_con_noce()
        attivata = self._run_completa_con_noce()
        cartella = self.configurazione().esecuzioni_dir / attivata
        # Il verbale torna com'era un attimo prima della morte: la run e' viva
        # a meta', ma il confronto che ha attivato e' quello sul disco.
        self._scrivi(cartella / pipeline_jobs.NOME_AUDIT_ESECUZIONE, {
            "stato": pipeline_jobs.IN_CORSO,
            "iniziatoIl": "2026-08-13T11:59:00+00:00",
        })

        esito = self._oggi_senza_noce()

        self.assertEqual(esito["numeri"]["confrontoConLaVoltaPrima"], attivata)
        self.assertIn("FORNITORE_SPARITO", {avviso["code"] for avviso in esito["avvisi"]})

    def test_un_verbale_con_l_ora_senza_fuso_non_e_la_volta_prima(self) -> None:
        """Un `iniziatoIl` nudo si misura con l'ora locale.

        Mischiato ai verbali veri — che il fuso ce l'hanno sempre, lo scrive
        `avvia()` — un'ora nuda puo' invertire l'ordine delle run di ore
        intere.  Il programma non la produce; una cartella copiata da un'altra
        macchina o scritta a mano si'.  Chi non sa dire QUANDO con il fuso non
        e' un termine di paragone: si salta.
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
    """Un comando che solleva un'eccezione qualunque, non una `Fermata`.

    E' il ramo che il 14 agosto 2026 e' arrivato in pagina come
    `AttributeError: module 'registro' has no attribute 'nome_del_fornitore'`,
    e fino a quel giorno non aveva un solo test.

    Con `fotografia_da_installare` fa anche l'altra meta' di quel giorno: il
    codice sul disco che cambia **mentre** la catena cammina.  Senza, la prova
    del ramo «guasto a run avviata» sarebbe fasulla — la guardia d'ingresso
    avrebbe gia' fermato tutto e il test passerebbe per un altro motivo.
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
    """Il programma aggiornato mentre gira si dichiara, e dice cosa fare.

    Il difetto vero del 14 agosto non e' stato l'`AttributeError`: e' stato che
    l'utente si e' trovato davanti una riga di Python in inglese **senza una
    sola cosa da fare** per uscirne dal browser.  Queste prove fissano le due
    cose che lo rendono risolvibile da solo: la run non parte, e la frase dice
    di chiudere e riaprire.
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
        """La fotografia si riscatta adesso: nessuno ha toccato niente.

        Serve perche' questi test devono dire qualcosa sul programma, non
        sull'ora in cui gira la suite: senza, basterebbe una modifica a un
        `.py` durante la passata per farli diventare rossi per il motivo
        sbagliato.
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
        """Il codice cambia **mentre** la catena cammina: stesso rimedio.

        ⚠ La prima versione di questo test era fasulla, e l'ha detto una
        controprova rimasta verde: fingeva il codice cambiato **prima**
        dell'avvio, quindi a fermare la run era la guardia d'ingresso e il ramo
        del guasto non veniva mai attraversato.  Qui la run parte con il disco
        fermo, il disco cambia dentro la fase della costruzione, e solo allora
        il passo esplode — come il 14 agosto.
        """

        self.finge_il_codice_fermo()
        cambiata = dict(pipeline_jobs.versione_del_codice.ALL_AVVIO)
        cambiata["scripts/registro.py"] = "0" * 64

        esito = self.esegui(EsecutoreCheEsplode("build_review_data.py", cambiata))

        self.assertEqual(esito["fermata"]["code"], pipeline_jobs.CODICE_CAMBIATO_DOPO_L_AVVIO)
        self.assertIn("AVVIA_COMPARATORE.cmd", esito["fermata"]["message"])
        self.assertIn("AttributeError", esito["fermata"]["dettaglio"])

    def test_un_guasto_vero_resta_un_guasto_e_non_manda_a_riavviare(self) -> None:
        """Senza questo, ogni difetto del programma diventerebbe «riavvia»."""

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
        """Serve a chi deve capire, ma sta di lato: non è la risposta."""

        self.finge_il_codice_fermo()

        esito = self.esegui(EsecutoreCheEsplode("build_review_data.py"))

        self.assertIn("AttributeError", esito["fermata"]["dettaglio"])
        self.assertIn("nome_del_fornitore", esito["fermata"]["dettaglio"])

    def test_con_il_codice_fermo_la_run_buona_passa(self) -> None:
        """La guardia non deve costare una run buona alla settimana."""

        self.finge_il_codice_fermo()

        esito = self.esegui(EsecutoreFinto())

        self.assertEqual(esito["stato"], pipeline_jobs.COMPLETATO, esito.get("messaggio"))


class LeUguaglianzeArrivanoAllaLettura(BancoPipeline):
    """I codici dichiarati uguali devono arrivare al passo che legge i listini.

    È l'anello dove un difetto non si vedrebbe: la dichiarazione resta scritta
    nel magazzino, la pagina continua a mostrarla, e la run la ignora in
    silenzio — cioè il prodotto abbinato a mano torna senza quel fornitore e
    nessuno sa perché.
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
        """Chiedere un file che non c'è fermerebbe la run: una run senza
        dichiarazioni è il caso normale."""

        self.assertNotIn("--equivalenze", self.argomenti_della_lettura())
        self.assertNotIn("--equivalenze", self.argomenti_della_lettura(uguaglianze_dichiarate=lambda: []))

    def test_i_gruppi_di_un_codice_solo_non_arrivano(self) -> None:
        argomenti = self.argomenti_della_lettura(uguaglianze_dichiarate=lambda: [["4009428623194"]])

        self.assertNotIn("--equivalenze", argomenti)

    def test_un_magazzino_che_non_si_legge_non_ferma_il_ricalcolo(self) -> None:
        """Le uguaglianze sono una memoria in più: perderle costa un abbinamento,
        fermare la run costa la settimana. Ma si dice nei numeri, invece di
        lasciare credere che non ce ne fossero."""

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
    """Rilievo [3] dell'onda 5, meta' servizio.

    L'avanzamento e' «fasi completate diviso nove», tutte pesate uguali.
    `VALUTAZIONE_AI` e' la sesta e per dichiarazione del codice stesso costa due
    minuti: la barra sale a scatti fino al 55,6 % e poi resta immobile per il
    tempo piu' lungo dell'intero ricalcolo. La riga della fase diceva «in corso»
    e basta, perche' il dettaglio veniva scritto solo alla fine.

    ⚠ Adesso il dettaglio si scrive anche all'inizio, e questa prova lo guarda
    MENTRE la fase gira: a fase finita quel testo viene sostituito da quello
    dell'esito, quindi guardarlo dopo non proverebbe niente.
    """

    def test_mentre_gira_la_riga_della_fase_dice_quanti_casi(self) -> None:
        visto: dict = {}

        class EsecutoreCheGuarda(EsecutoreFinto):
            """Fotografa lo stato del ricalcolo mentre la fase AI sta girando."""

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
            # Il rapporto deve dichiarare gli stessi casi della shortlist,
            # altrimenti l'attivazione si ferma prima — ed e' giusto cosi'.
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
        """Il dettaglio d'avvio non deve restare li' a dire quanti casi c'erano
        quando la fase e' finita: quello che serve allora e' com'e' andata."""

        esito = self.esegui(EsecutoreFinto())

        fase = next(voce for voce in esito["fasi"] if voce["nome"] == "VALUTAZIONE_AI")
        self.assertEqual(fase["stato"], pipeline_jobs.COMPLETATO)
        self.assertIn("decisi su", fase.get("dettaglio", ""))
        self.assertNotIn("da valutare", fase.get("dettaglio", ""))


class LAttivazioneNonSiIncrociaColSalvataggio(BancoPipeline):
    """L'unico tratto in cui la catena scrive dati che le rotte stanno servendo.

    `_ripulisci_stato` riscrive `state.json` e subito dopo `review_data.json`
    diventa quello nuovo.  Un salvataggio della pagina arrivato in mezzo
    scriveva lo stesso file dallo stesso temporaneo, e quello che restava sul
    disco non era ne' lo stato dell'utente ne' quello della catena.  Da qui il
    lucchetto condiviso: e' lo stesso `ReviewStore.lock` delle rotte, passato
    al costruttore.
    """

    @staticmethod
    def _libero(lucchetto: threading.RLock) -> bool:
        """Se un ALTRO filo riesce a prenderlo. Da qui no: un `RLock` si

        riprende quante volte si vuole, e la risposta sarebbe sempre si'."""

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
        """L'altro capo della sezione: il momento in cui il confronto nuovo

        diventa quello vivo. Fra i due estremi lo stato e il confronto non
        sono d'accordo, ed e' li' che una rotta non deve poter guardare."""

        lucchetto = threading.RLock()
        gestore = self.gestore(EsecutoreFinto(), lucchetto_dati=lucchetto)
        visto: list[bool] = []
        replace_vero = os.replace

        def annota(sorgente, destinazione):
            # `resolve()` su tutt'e due: su macOS la cartella temporanea e'
            # `/var/...` per il test e `/private/var/...` per la
            # configurazione, e il confronto secco non prenderebbe mai.
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
        """La prova che il rilievo chiedeva: una run e un salvataggio insieme.

        Un secondo lucchetto e' il modo classico di guadagnarsi un abbraccio
        mortale, che qui si vedrebbe come programma piantato a ricalcolo
        finito.  Il filo qui sotto fa quello che fa la pagina — prende e
        rilascia — per tutta la durata della run.
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
    """Windows Time corregge l'ora quando vuole, e due volte l'anno c'e' l'ora legale.

    Una correzione presa in mezzo a un ricalcolo scriveva nella pagina e
    nell'audit della cartella una durata di un'ora sbagliata, o negativa — ed
    e' il numero che poi si usa per dire «era lento».
    """

    def test_le_durate_delle_fasi_non_seguono_l_ora_di_parete(self) -> None:
        vero = pipeline_jobs.datetime

        class OrologioCheTornaIndietro(vero):
            """Ogni lettura arriva un secondo prima della precedente."""

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
        """L'unica `datetime.now()` che resta, e non e' una dimenticanza: il

        nome della cartella e' una data che una persona legge.

        ⚠ Non basta controllarne la forma: `2026-08-20_1000` in UTC ha la
        stessa forma di `2026-08-20_1200` in locale, e la prima versione di
        questa prova restava verde sostituendo l'ora locale con quella UTC.
        Qui l'orologio finto risponde due ore diverse alle due domande, e il
        nome deve portare quella locale.
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
    """Il tetto lo mette l'orchestratore, e ogni fase ha il suo.

    Con la tabella dei tetti in un posto solo e nessuno che la legge, il
    programma tornerebbe ad aspettare per sempre senza che niente lo dica.
    """

    def test_gli_otto_comandi_ricevono_il_tetto_della_loro_fase(self) -> None:
        esecutore = EsecutoreFinto()
        self.esegui(esecutore)

        self.assertEqual(len(esecutore.tetti), len(esecutore.chiamati))
        for script, tetto in esecutore.tetti.items():
            self.assertIsNotNone(tetto, script)
            self.assertGreater(tetto, 0, script)
        # La fase AI dura 105 secondi misurati: e' l'unica che non entra nel
        # tetto delle altre, ed e' l'unica dichiarata a parte.
        self.assertEqual(
            esecutore.tetti["valuta_shortlist.py"], pipeline_jobs.TETTO_DELLE_FASI["VALUTAZIONE_AI"]
        )
        self.assertEqual(
            esecutore.tetti["inspect_sources.py"], pipeline_jobs.TETTO_PREDEFINITO_DI_FASE
        )


class UnApprendimentoNonRiuscitoNonButtaViaIlConfronto(BancoPipeline):
    """Il difetto del 21 agosto 2026, e la sua contropartita.

    Su un listino senza riga di intestazione la mappatura guidata dichiara le
    colonne per NUMERO, `impara_adattatore` rifiuta di ricavarne un'impronta
    per intestazioni, e la fermata `APPRENDIMENTO_SCHEMA_NON_RIUSCITO` buttava
    via un confronto **gia' costruito e buono**. Riprovare non serviva —
    la decisione a mano resta al suo posto e la run rifa' la stessa strada — e
    ne' chiudere ne' riaprire il programma scioglievano niente.

    La contropartita e' l'altra meta' della prova: il ponte a mano si toglie
    solo per chi e' stato imparato davvero, altrimenti la settimana dopo quel
    listino non si aprirebbe piu'.
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
        """`impara_adattatore` risponde con i rapporti dichiarati, in ordine."""

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
        """Toglierlo vorrebbe dire un listino che la settimana dopo non si apre."""

        percorso = self.dati / pipeline_jobs.NOME_DECISIONI_MANUALI
        self._scrivi(percorso, {"decisions": [{"file_name": "offerte.xlsx", "state": "NUOVO_FORNITORE"}]})
        gestore = self.gestore_con_una_decisione()
        self.con_esiti(gestore, [self.rapporto([], [("offerte.xlsx", "Rifiutato: niente nomi")])])

        gestore._impara_schemi_confermati(self.corsa, "COSTRUZIONE")

        rimaste = json.loads(percorso.read_text(encoding="utf-8"))["decisions"]
        self.assertEqual([voce["file_name"] for voce in rimaste], ["offerte.xlsx"])

    def test_nemmeno_un_guasto_vero_butta_via_il_confronto(self) -> None:
        """Il rifiuto pulito era gia' un avviso; il guasto usciva 1 e fermava tutto.

        Succede su un PC vero: la cartella `app/data/` in sola lettura, il
        registro tenuto aperto da un antivirus mentre lo si legge.
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
        """La settimana prossima quel listino verrà letto come il listino di un altro."""

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

        # Rimasto senza voci, il ponte a mano sparisce del tutto: e' il modo in
        # cui `_fase_riconoscimento` smette di dire «entra per decisione manuale».
        self.assertFalse(percorso.exists())
        codici = [voce.get("code") for voce in (gestore.stato().get("avvisi") or [])]
        self.assertNotIn("SCHEMA_NON_MEMORIZZATO", codici)



class LeCartelleDiLavoroNonCresconoPerSempre(BancoPipeline):
    """Ogni ricalcolo lascia una cartella datata, e nessuno le cancellava.

    Misurate a 13 MB l'una sul confronto vero: con un ricalcolo a settimana
    fanno circa settecento megabyte l'anno sul disco del negozio, molti di piu'
    contando i tentativi. Il danno non e' lo spazio: e' che a disco pieno si
    aprono in fila i guasti che questo file esiste per evitare.

    Prove eseguite.
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
        # Le quattro piu' vecchie se ne sono andate…
        for sparita in ("2026-01-01_0900", "2026-02-01_0900", "2026-03-01_0900"):
            self.assertNotIn(sparita, rimaste)
        # …e le piu' recenti sono ancora li', insieme a quella appena creata.
        self.assertIn("2026-07-01_0900", rimaste)
        self.assertIn(esito["runId"], rimaste)
        self.assertLessEqual(len(rimaste), pipeline_jobs.ESECUZIONI_DA_TENERE + 1)

    def test_la_cartella_del_confronto_vivo_non_si_tocca_mai(self) -> None:
        """È quella che la pagina riapre per dire quali colonne ha letto.

        Il confronto vivo dichiara da quale run viene: se quella cartella
        sparisce perche' e' vecchia, «Quali colonne legge» e la mappatura
        guidata rispondono «la cartella di quel confronto non c'e' piu'» su un
        confronto che si sta guardando in quel momento.
        """

        # Il confronto vivo del banco viene da una run chiamata «vecchia», ed e'
        # la piu' vecchia di tutte in ordine alfabetico.
        self._scrivi(self.review, {**CONFRONTO_PRECEDENTE,
                                   "run": {"id": "vecchia", "status": "ready", "pipelineRunId": "vecchia"}})
        self.finte("vecchia", "2026-01-01_0900", "2026-02-01_0900", "2026-03-01_0900",
                   "2026-04-01_0900", "2026-05-01_0900", "2026-06-01_0900", "2026-07-01_0900")

        gestore = self.gestore(EsecutoreFinto())
        gestore.avvia()
        gestore.attendi(timeout=30)

        self.assertIn("vecchia", self.cartelle())

    def test_una_cartella_che_non_si_cancella_non_ferma_il_ricalcolo(self) -> None:
        """Fare spazio e' una comodita', non una precondizione per lavorare."""

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
    """⚠ Il guasto peggiore era il più silenzioso.

    `pipeline_status.json` si riscrive a ogni fase. Se non si scrive — disco
    pieno, cartella in sola lettura, antivirus sul temporaneo — l'eccezione
    partiva da dentro il gestore d'errore di `_lavora`, che chiama di nuovo
    `_segna_fase` e riprova la stessa scrittura fallita: la seconda eccezione
    usciva prima che lo stato diventasse `ERRORE`. Il filo moriva, il lucchetto
    si liberava, e in pagina restava una barra ferma su «in corso» che non
    sarebbe avanzata mai più, col ricalcolo e i caricamenti spenti.

    Prove eseguite.
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
        # E lo dice, una volta sola: una console piena della stessa riga non la
        # legge nessuno.
        self.assertEqual(console.getvalue().count("[AVVISO] Non riesco a scrivere"), 1)

    def test_una_run_che_fallisce_arriva_a_dirlo(self) -> None:
        """È il caso che inchiodava la pagina: guasto vero + stato non scrivibile."""

        self.senza_scrittura()

        def esplode(comando, cartella, avanzamento, **extra):
            raise RuntimeError("il passo si è rotto")

        gestore = self.gestore(esplode)
        with contextlib.redirect_stdout(io.StringIO()):
            gestore.avvia()
            esito = gestore.attendi(timeout=30)

        self.assertEqual(esito["stato"], pipeline_jobs.ERRORE)
        self.assertNotEqual(esito["stato"], pipeline_jobs.IN_CORSO)
        # E il lucchetto e' tornato libero: si riprova senza chiudere niente.
        self.assertTrue(gestore.lucchetto_lavori.acquire(blocking=False))
        gestore.lucchetto_lavori.release()


if __name__ == "__main__":
    unittest.main()
