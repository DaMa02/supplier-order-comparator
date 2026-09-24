"""Tests for `scripts/valuta_shortlist.py`, the AI evaluation step.

What's covered here is the contract with the orchestrator, not the quality
of the model's answers — that's measured by the benchmark instead.

- States without a decision are omitted from the file, never invented: a
  network fault must not become an `UNRESOLVED` indistinguishable from "the
  model didn't know".
- Both output files are always written, even when the phase is degraded: an
  `ai_decisions.json` left over from the previous run would otherwise be
  read as fresh.
- The report is the only accounting anyone will read, and it's the
  independent source for `merge_match_decisions.py --decisions-attese`.
- Degradation is a return value: no key, network down, spend cap reached all
  exit with code 5 and the files in place, not with an exception.
- The case fingerprint travels across both scripts: the one written here
  from the case sent to the model must match the one the merge step
  recomputes from the shortlist. This is the only test that would notice if
  the two computations diverged.

No test touches the network: the transport is injected.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parents[1]
for _cartella in (SKILL_ROOT / "app", SKILL_ROOT / "scripts"):
    if str(_cartella) not in sys.path:
        sys.path.insert(0, str(_cartella))

import valuta_shortlist  # noqa: E402
from ai_client import Candidato, CasoValutazione, ClientAI  # noqa: E402
from build_semantic_shortlists import impronta_caso  # noqa: E402
from valuta_shortlist import (  # noqa: E402
    USCITA_DEGRADATO,
    USCITA_INGRESSO_NON_UTILIZZABILE,
    USCITA_OK,
    caso_dalla_voce,
    casi_dalle_shortlist,
)


CHIAVE_FINTA = "sk-prova-non-vera-0123456789"


def voce(
    *,
    riga: int = 12,
    fornitore: str = "betulla",
    descrizione: str = "PANTERA SHAMPOO 250ML RICCI NEW",
    candidati: tuple[tuple[int, str, float], ...] = (
        (441, "PANTERA SH.250 RICCI", 0.81),
        (512, "PANTERA BALSAMO 200", 0.42),
    ),
) -> dict[str, Any]:
    """An entry of `semantic_shortlists.json`, shaped like the real pipeline writes it."""

    return {
        "gestionale_source_row": riga,
        "ean": "8009249129239",
        "description": descrizione,
        "supplier": fornitore,
        "reason": "EAN_ASSENTE",
        "candidates": [
            {
                "source_row": r,
                "ean": f"800{r}",
                "description": d,
                # Real price lists carry the price as a string.
                "unit_price_net": "3.9800",
                "score": s,
                "shared_tokens": ["PANTERA"],
                "attribute_conflicts": [],
                "query_attributes": {},
                "candidate_attributes": {},
            }
            for r, d, s in candidati
        ],
    }


def risposta_ok(contenuto: str, *, costo: float = 0.0001) -> tuple[int, dict]:
    return 200, {
        "model": "openai/gpt-5.6-luna",
        "provider": "DeepInfra",
        "choices": [{"finish_reason": "stop", "message": {"content": contenuto}}],
        "usage": {"prompt_tokens": 200, "completion_tokens": 40, "cost": costo},
    }


def contenuto(azione: str, source_row: Any, confidenza: str = "ALTA", motivo: str = "prova") -> str:
    return json.dumps(
        {"azione": azione, "source_row": source_row, "confidenza": confidenza, "motivo": motivo},
        ensure_ascii=False,
    )


def trasporto(risposte: dict[str, Any]):
    """A fake transport that answers based on the item being queried.

    Each value can be: the JSON content of a good response, a `(status,
    body)` pair to return as-is, or an exception to raise — which is how a
    network failure is simulated without touching the network."""

    def chiamata(url: str, corpo: dict, intestazioni: dict, timeout: float):
        domanda = corpo["messages"][-1]["content"]
        articolo = domanda.splitlines()[0].split(": ", 1)[1]
        reazione = risposte[articolo]
        if isinstance(reazione, BaseException):
            raise reazione
        if isinstance(reazione, tuple):
            return reazione
        return risposta_ok(reazione)

    return chiamata


def righe_mostrate(corpo: dict) -> list[int]:
    return [int(n) for n in re.findall(r"source_row (\d+):", corpo["messages"][-1]["content"])]


def esegui(
    cartella: Path,
    shortlists: list[dict[str, Any]],
    *,
    trasporto_finto=None,
    chiave: str = CHIAVE_FINTA,
    modifiche: dict[str, Any] | None = None,
    crea_client=None,
) -> tuple[int, Any, Any, str]:
    """Call `main` in-process, with the client injected. No network involved.

    Returns exit code, decisions, report and whatever was printed. `None` in
    place of a file means that file wasn't written."""

    percorso_shortlists = cartella / "semantic_shortlists.json"
    percorso_shortlists.write_bytes(
        json.dumps(shortlists, ensure_ascii=False).encode("utf-8")
    )
    decisioni = cartella / "ai_decisions.json"
    rapporto = cartella / "ai_rapporto.json"

    viste: dict[str, Any] = {}

    def crea(configurazione, memoria):
        viste.update(configurazione)
        if crea_client is not None:
            return crea_client(configurazione, memoria)
        return ClientAI(
            {**configurazione, **(modifiche or {})},
            trasporto=trasporto_finto,
            memoria=memoria,
            chiave=chiave,
        )

    schermo = io.StringIO()
    with redirect_stdout(schermo):
        codice = valuta_shortlist.main(
            [
                "--shortlists", str(percorso_shortlists),
                "--output", str(decisioni),
                "--rapporto", str(rapporto),
                # Always a temp directory: the default memory file is
                # `app/data/memoria_ai.json`, and tests must not touch real data.
                "--memoria", str(cartella / "memoria_ai.json"),
            ],
            crea_client=crea,
        )
    return (
        codice,
        json.loads(decisioni.read_text(encoding="utf-8")) if decisioni.exists() else None,
        json.loads(rapporto.read_text(encoding="utf-8")) if rapporto.exists() else None,
        schermo.getvalue(),
    )


class DallIngressoAiCasiTests(unittest.TestCase):
    """A shortlist entry becomes the case the model sees. If this translation
    is wrong, the model answers correctly to the wrong question."""

    def test_la_voce_diventa_il_caso_con_i_campi_giusti(self) -> None:
        caso = caso_dalla_voce(voce(), 0)
        self.assertEqual(caso.gestionale_source_row, 12)
        self.assertEqual(caso.supplier, "betulla")
        self.assertEqual(caso.descrizione, "PANTERA SHAMPOO 250ML RICCI NEW")
        self.assertEqual([c.source_row for c in caso.candidati], [441, 512])
        self.assertEqual(caso.candidati[0].description, "PANTERA SH.250 RICCI")
        self.assertAlmostEqual(caso.candidati[0].score, 0.81)

    def test_il_candidato_non_porta_ne_ean_ne_prezzo(self) -> None:
        """Deliberate: the EAN is the ground truth the benchmark relies on, so
        showing it to the model would invalidate every past measurement; the
        price has nothing to do with product identity. If either is ever
        added, it must break this test, not silently skew a measurement."""
        campi = set(vars(caso_dalla_voce(voce(), 0).candidati[0]))
        self.assertEqual(campi, {"source_row", "description", "score"})

    def test_le_descrizioni_non_si_normalizzano(self) -> None:
        """The text goes both to the model and into the fingerprint. Stripping
        spaces here but not in the merge step would fail the comparison on
        every case, discarding an entire run."""
        caso = caso_dalla_voce(voce(descrizione="  PANTERA  250  "), 0)
        self.assertEqual(caso.descrizione, "  PANTERA  250  ")

    def test_un_caso_senza_candidati_non_va_al_modello(self) -> None:
        """`ClientAI` raises on a case with no candidates, and rightly so:
        asking it to choose among nothing is a misuse of the API. Six out of
        948 cases in the real run have no candidates, so this isn't a
        textbook case."""
        casi, senza = casi_dalle_shortlist([voce(), voce(riga=13, candidati=())])
        self.assertEqual([c.gestionale_source_row for c in casi], [12])
        self.assertEqual([c.gestionale_source_row for c in senza], [13])

    def test_una_coppia_duplicata_si_ferma_qui(self) -> None:
        """Downstream this would surface as a "duplicate decision" in
        `merge_match_decisions.py`, sending whoever debugs it to look in the
        wrong file."""
        with self.assertRaises(ValueError) as errore:
            casi_dalle_shortlist([voce(), voce()])
        self.assertIn("più di una volta", str(errore.exception))

    def test_gli_ingressi_malformati_sono_un_guasto_non_un_caso_saltato(self) -> None:
        """The shortlist is written by `build_semantic_shortlists.py`: a
        malformed entry means something upstream broke, and silently
        continuing would drop products from the comparison without saying
        so."""
        casi_storti = {
            "voce non oggetto": ["non un oggetto"],
            "riga assente": [{**voce(), "gestionale_source_row": None}],
            "riga con la virgola": [{**voce(), "gestionale_source_row": 12.5}],
            "riga booleana": [{**voce(), "gestionale_source_row": True}],
            "fornitore vuoto": [{**voce(), "supplier": "   "}],
            "descrizione assente": [{**voce(), "description": None}],
            "descrizione non testo": [{**voce(), "description": ["PANTERA"]}],
            "candidati non lista": [{**voce(), "candidates": {"source_row": 1}}],
            "candidato non oggetto": [{**voce(), "candidates": ["441"]}],
            "candidato senza riga": [{**voce(), "candidates": [{"description": "X", "score": 0.5}]}],
            "candidato senza descrizione": [{**voce(), "candidates": [{"source_row": 1, "score": 0.5}]}],
            "candidato senza punteggio": [{**voce(), "candidates": [{"source_row": 1, "description": "X"}]}],
            "punteggio non numerico": [
                {**voce(), "candidates": [{"source_row": 1, "description": "X", "score": "molto"}]}
            ],
            # `float(True)` silently gives 1.0, and a boolean score would
            # enter the fingerprint as a perfect score.
            "punteggio booleano": [
                {**voce(), "candidates": [{"source_row": 1, "description": "X", "score": True}]}
            ],
            # `int(float("inf"))` raises `OverflowError`, which `main` doesn't
            # catch: that means a traceback, exit code 1, and neither file
            # written. `NaN`, by contrast, must be caught explicitly — it
            # would otherwise pass silently.
            "riga infinita": [{**voce(), "gestionale_source_row": float("inf")}],
            "riga non numerabile": [{**voce(), "gestionale_source_row": float("nan")}],
        }
        for nome, shortlists in casi_storti.items():
            with self.subTest(nome), self.assertRaises(ValueError):
                casi_dalle_shortlist(shortlists)

    def test_una_riga_scritta_come_stringa_si_accetta(self) -> None:
        """JSON written by another program can carry `"12"`: that isn't a
        fault. `12.5` is, because `int()` would silently truncate it, and a
        row off by one is exactly the kind of bug the row-verification guard
        exists to catch."""
        caso = caso_dalla_voce({**voce(), "gestionale_source_row": "12"}, 0)
        self.assertEqual(caso.gestionale_source_row, 12)

    def test_una_shortlist_che_non_e_una_lista_si_ferma(self) -> None:
        with self.assertRaises(ValueError):
            casi_dalle_shortlist({"gestionale_source_row": 12})


class IlFileDelleDecisioniTests(unittest.TestCase):
    """What ends up in the decisions file, and above all what doesn't."""

    def test_una_decisione_per_caso_deciso_nel_formato_del_merge(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            codice, decisioni, rapporto, _schermo = esegui(
                Path(temporanea),
                [voce()],
                trasporto_finto=trasporto(
                    {"PANTERA SHAMPOO 250ML RICCI NEW": contenuto("ACCEPT", 441)}
                ),
            )
        self.assertEqual(codice, USCITA_OK)
        self.assertEqual(len(decisioni), 1)
        riga = decisioni[0]
        self.assertEqual(riga["gestionale_source_row"], 12)
        self.assertEqual(riga["supplier"], "betulla")
        self.assertEqual(riga["action"], "ACCEPT")
        self.assertEqual(riga["source_row"], 441)
        self.assertEqual(riga["confidence"], "ALTA")
        self.assertTrue(riga["rationale"])
        self.assertIn("requires_user_confirmation", riga)
        self.assertEqual(rapporto["casi_decisi"], 1)

    def test_ogni_decisione_porta_l_impronta_del_caso_mandato_al_modello(self) -> None:
        """The fingerprint is the only thing that ties a decision to what the
        model actually had in front of it: without it, a file from another
        run would pass unnoticed."""
        una = voce()
        with tempfile.TemporaryDirectory() as temporanea:
            _codice, decisioni, _rapporto, _schermo = esegui(
                Path(temporanea),
                [una],
                trasporto_finto=trasporto(
                    {"PANTERA SHAMPOO 250ML RICCI NEW": contenuto("ACCEPT", 441)}
                ),
            )
        attesa = impronta_caso(
            12,
            "betulla",
            "PANTERA SHAMPOO 250ML RICCI NEW",
            [(c["source_row"], c["description"], c["score"]) for c in una["candidates"]],
        )
        self.assertEqual(decisioni[0]["ai_impronta_caso"], attesa)
        self.assertEqual(decisioni[0]["ai_articolo_mostrato"], "PANTERA SHAMPOO 250ML RICCI NEW")

    def test_gli_stati_senza_decisione_non_producono_nessuna_riga(self) -> None:
        """Writing a fake `UNRESOLVED` in place of a network fault would erase
        the difference between "the model didn't know" and "I couldn't ask"
        — and the latter is fixed by simply rerunning."""
        with tempfile.TemporaryDirectory() as temporanea:
            codice, decisioni, rapporto, _schermo = esegui(
                Path(temporanea),
                [
                    voce(descrizione="DECIDE"),
                    voce(riga=13, descrizione="RETE GIU'"),
                    voce(riga=14, descrizione="FUORI SCHEMA"),
                ],
                trasporto_finto=trasporto({
                    "DECIDE": contenuto("REJECT", None),
                    "RETE GIU'": OSError("connessione rifiutata"),
                    "FUORI SCHEMA": "non sono nemmeno json",
                }),
            )
        self.assertEqual(codice, USCITA_DEGRADATO)
        self.assertEqual([r["gestionale_source_row"] for r in decisioni], [12])
        self.assertEqual(rapporto["casi_valutabili"], 3)
        self.assertEqual(rapporto["casi_decisi"], 1)
        self.assertEqual(rapporto["casi_senza_decisione"], 2)

    def test_un_rifiuto_non_porta_nessuna_riga_scelta(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            _codice, decisioni, _rapporto, _schermo = esegui(
                Path(temporanea),
                [voce()],
                trasporto_finto=trasporto(
                    {"PANTERA SHAMPOO 250ML RICCI NEW": contenuto("REJECT", 441)}
                ),
            )
        self.assertEqual(decisioni[0]["action"], "REJECT")
        self.assertIsNone(decisioni[0]["source_row"])

    def test_il_file_e_una_lista_anche_quando_e_vuota(self) -> None:
        """`merge_match_decisions.py` requires a list: a missing file or an
        object makes it exit 2 and 3 respectively."""
        with tempfile.TemporaryDirectory() as temporanea:
            codice, decisioni, rapporto, _schermo = esegui(
                Path(temporanea), [voce(candidati=())], trasporto_finto=trasporto({})
            )
        self.assertEqual(decisioni, [])
        self.assertEqual(rapporto["casi_senza_candidati"], 1)
        self.assertEqual(rapporto["coppie_senza_candidati"], [[12, "betulla"]])
        # And since that one case couldn't even be attempted, the phase is
        # degraded: see the test below.
        self.assertEqual(codice, USCITA_DEGRADATO)

    def test_nessun_candidato_da_nessuna_parte_non_e_un_successo(self) -> None:
        """The case where a naive implementation declares "evaluated
        everything there was to evaluate" and exits 0, when in fact nothing
        was evaluated.

        No network failure and no bad key needed: it's enough for an adapter
        to change and a price list to come out without prices.
        `build_semantic_shortlists.py` discards those rows and writes
        hundreds of entries with `candidates: []`; the AI phase evaluates
        nothing, and without this check the whole chain would reach the end
        with exit code 0."""
        with tempfile.TemporaryDirectory() as temporanea:
            codice, decisioni, rapporto, _schermo = esegui(
                Path(temporanea),
                [voce(riga=riga, candidati=()) for riga in range(1, 21)],
                trasporto_finto=trasporto({}),
            )
        self.assertEqual(codice, USCITA_DEGRADATO)
        self.assertEqual(decisioni, [])
        self.assertTrue(rapporto["degradato"])
        self.assertIn("nessuno dei 20 casi ricevuti", rapporto["motivo_degrado"])
        self.assertIn("un listino non si è caricato", rapporto["motivo_degrado"])

    def test_una_coda_vuota_invece_e_un_successo(self) -> None:
        """The distinction the check above has to make. If EAN matching
        resolved everything, `semantic_shortlists.json` is an empty list:
        there was no work, and reporting degradation would send the
        orchestrator to redo a phase that had nothing to do."""
        with tempfile.TemporaryDirectory() as temporanea:
            codice, decisioni, rapporto, _schermo = esegui(
                Path(temporanea), [], trasporto_finto=trasporto({})
            )
        self.assertEqual(codice, USCITA_OK)
        self.assertEqual(decisioni, [])
        self.assertFalse(rapporto["degradato"])
        self.assertEqual(rapporto["casi_ricevuti"], 0)

    def test_gli_esiti_fuori_ordine_non_producono_nessuna_decisione(self) -> None:
        """Pairing a case with its outcome relies on ordering, which the
        client promises. If that promise broke, every decision would land on
        the wrong item with its `ALTA` confidence intact: better zero
        decisions than 948 attributed at random."""

        class ClientCheMescola:
            contabilita = {"chiamate": 2, "costo_usd": 0.0, "per_stato": {}, "dalla_memoria": 0}

            def valuta_molti_con_verifica(self, casi):
                return list(reversed([EsitoFinto(caso) for caso in casi]))

        with tempfile.TemporaryDirectory() as temporanea:
            codice, decisioni, rapporto, _schermo = esegui(
                Path(temporanea),
                [voce(), voce(riga=13, descrizione="ALTRO")],
                crea_client=lambda configurazione, memoria: ClientCheMescola(),
            )
        self.assertEqual(codice, USCITA_DEGRADATO)
        self.assertEqual(decisioni, [])
        self.assertIn("fuori posto", rapporto["motivo_degrado"])

    def test_un_esito_in_meno_ferma_tutto_invece_di_accoppiare_a_caso(self) -> None:
        """Ordering alone isn't enough: `zip` stops at the shorter sequence
        without saying anything. The test above passes an outcome list of
        the same length, so it never exercises the count check — a mutation
        removing that check would still pass. A client that returns nine
        results out of ten would silently drop decisions already paid
        for."""

        class ClientCheNePerdeUno:
            contabilita = {"chiamate": 1, "costo_usd": 0.0, "per_stato": {}, "dalla_memoria": 0}

            def valuta_molti_con_verifica(self, casi):
                return [EsitoFinto(casi[0])]

        with tempfile.TemporaryDirectory() as temporanea:
            codice, decisioni, rapporto, _schermo = esegui(
                Path(temporanea),
                [voce(), voce(riga=13, descrizione="ALTRO")],
                crea_client=lambda configurazione, memoria: ClientCheNePerdeUno(),
            )
        self.assertEqual(codice, USCITA_DEGRADATO)
        self.assertEqual(decisioni, [])
        self.assertIn("1 esiti per 2 casi", rapporto["motivo_degrado"])

    def test_ogni_decisione_dichiara_con_che_cosa_e_stata_presa(self) -> None:
        """The fingerprint says the case is the same, not what it was judged
        with. If a supplier's price list doesn't change, the case is
        identical and a decision from a previous week passes the
        fingerprint check: without these three fields, a decision made with
        an older prompt version — one without the adversarial check, say —
        would enter today's order with nothing saying so. They're written
        here; comparing them against the live configuration is the
        orchestrator's job."""
        with tempfile.TemporaryDirectory() as temporanea, mock.patch.dict(
            os.environ, {"OPENROUTER_MODEL": "prova/modello-di-prova"}
        ):
            _codice, decisioni, rapporto, _schermo = esegui(
                Path(temporanea),
                [voce()],
                trasporto_finto=trasporto(
                    {"PANTERA SHAMPOO 250ML RICCI NEW": contenuto("ACCEPT", 441)}
                ),
            )
        self.assertEqual(decisioni[0]["ai_modello"], "prova/modello-di-prova")
        self.assertEqual(decisioni[0]["ai_versione_prompt"], rapporto["versione_prompt"])
        self.assertEqual(decisioni[0]["ai_versione_avversario"], rapporto["versione_avversario"])


class EsitoFinto:
    """A fake outcome carrying its case's decision, to test ordering."""

    stato = "OK"

    def __init__(self, caso: CasoValutazione) -> None:
        self.decisione = {
            "gestionale_source_row": caso.gestionale_source_row,
            "supplier": caso.supplier,
            "action": "REJECT",
            "source_row": None,
            "confidence": "MEDIA",
            "rationale": "finto",
            "requires_user_confirmation": True,
        }


class IlRapportoTests(unittest.TestCase):
    """The report is the only accounting anyone will read: the finished
    program runs unattended, and nobody reads the logs."""

    def test_i_conteggi_tornano_e_casi_decisi_e_la_lunghezza_del_file(self) -> None:
        """`casi_decisi` is the number the orchestrator passes to
        `--decisions-attese`: if it weren't the file's length, reconciliation
        would fail on a healthy run."""
        with tempfile.TemporaryDirectory() as temporanea:
            _codice, decisioni, rapporto, _schermo = esegui(
                Path(temporanea),
                [voce(descrizione="UNO"), voce(riga=13, descrizione="DUE"), voce(riga=14, candidati=())],
                trasporto_finto=trasporto({
                    "UNO": contenuto("ACCEPT", 441),
                    "DUE": contenuto("REJECT", None),
                }),
            )
        self.assertEqual(rapporto["casi_ricevuti"], 3)
        self.assertEqual(rapporto["casi_valutabili"], 2)
        self.assertEqual(rapporto["casi_senza_candidati"], 1)
        self.assertEqual(rapporto["casi_decisi"], len(decisioni))
        self.assertEqual(rapporto["per_azione"], {"ACCEPT": 1, "REJECT": 1})
        self.assertFalse(rapporto["degradato"])

    def test_i_due_conteggi_per_stato_sono_diversi_e_non_e_una_svista(self) -> None:
        """`per_stato` has one entry per case; the client's own counter also
        includes the second, adversarial pass, which runs on every `ACCEPT`.
        Summing the latter and reading it as "cases" would overstate the
        actual amount of work done."""
        with tempfile.TemporaryDirectory() as temporanea:
            _codice, _decisioni, rapporto, _schermo = esegui(
                Path(temporanea),
                [voce()],
                trasporto_finto=trasporto(
                    {"PANTERA SHAMPOO 250ML RICCI NEW": contenuto("ACCEPT", 441)}
                ),
            )
        self.assertEqual(sum(rapporto["per_stato"].values()), 1)
        self.assertEqual(sum(rapporto["per_stato_incluse_verifiche"].values()), 2)
        self.assertEqual(rapporto["chiamate"], 2)

    def test_la_spesa_e_la_durata_ci_sono(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            _codice, _decisioni, rapporto, _schermo = esegui(
                Path(temporanea),
                [voce()],
                trasporto_finto=trasporto(
                    {"PANTERA SHAMPOO 250ML RICCI NEW": contenuto("ACCEPT", 441)}
                ),
            )
        self.assertAlmostEqual(rapporto["costo_usd"], 0.0002)
        self.assertGreaterEqual(rapporto["durata_s"], 0.0)

    def test_il_rapporto_dichiara_la_configurazione_viva_non_una_copia_scritta(self) -> None:
        """If an environment variable picks the model, the report must say the
        real one: it's the only record of which model actually answered."""
        with tempfile.TemporaryDirectory() as temporanea, mock.patch.dict(
            os.environ, {"OPENROUTER_MODEL": "prova/modello-di-prova"}
        ):
            _codice, _decisioni, rapporto, _schermo = esegui(
                Path(temporanea),
                [voce()],
                trasporto_finto=trasporto(
                    {"PANTERA SHAMPOO 250ML RICCI NEW": contenuto("REJECT", None)}
                ),
            )
        self.assertEqual(rapporto["model"], "prova/modello-di-prova")
        self.assertEqual(rapporto["versione_prompt"], "v3")

    def test_il_motivo_del_degrado_dice_quali_stati(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            _codice, _decisioni, rapporto, _schermo = esegui(
                Path(temporanea),
                [voce(descrizione="UNO"), voce(riga=13, descrizione="DUE")],
                trasporto_finto=trasporto({
                    "UNO": OSError("rete giù"),
                    "DUE": OSError("rete giù"),
                }),
            )
        self.assertTrue(rapporto["degradato"])
        self.assertIn("2 casi su 2", rapporto["motivo_degrado"])
        self.assertIn("ERRORE_RETE", rapporto["motivo_degrado"])

    def test_le_decisioni_si_scrivono_prima_del_rapporto(self) -> None:
        """The write order is a contract, not just a comment: swapping the two
        writes must be caught by a test, not left undefended. If the process
        dies in between, the orchestrator must find a missing report — and
        so notice the failure — instead of a report claiming 942 decisions
        next to the previous run's stale file."""
        scritti: list[str] = []
        vero = valuta_shortlist.scrivi_json

        def annota(percorso, documento):
            scritti.append(percorso.name)
            vero(percorso, documento)

        with tempfile.TemporaryDirectory() as temporanea, mock.patch.object(
            valuta_shortlist, "scrivi_json", annota
        ):
            esegui(Path(temporanea), [voce(candidati=())], trasporto_finto=trasporto({}))
        self.assertEqual(scritti, ["ai_decisions.json", "ai_rapporto.json"])

    def test_il_rapporto_della_run_precedente_non_sopravvive(self) -> None:
        """If writing the report fails, yesterday's must not stay next to
        today's decisions — a stale report next to fresh decisions would
        defeat the "missing report signals failure" guarantee above."""
        with tempfile.TemporaryDirectory() as temporanea:
            cartella = Path(temporanea)
            vecchio = cartella / "ai_rapporto.json"
            vecchio.write_bytes(b'{"casi_decisi": 999}')

            def esplode(percorso, documento):
                if percorso.name == "ai_decisions.json":
                    raise OSError("disco pieno")

            with mock.patch.object(valuta_shortlist, "scrivi_json", esplode):
                codice, _decisioni, rapporto, _schermo = esegui(
                    cartella, [voce(candidati=())], trasporto_finto=trasporto({})
                )
        self.assertEqual(codice, USCITA_INGRESSO_NON_UTILIZZABILE)
        self.assertIsNone(rapporto)

    def test_il_rapporto_si_stampa_anche_a_schermo(self) -> None:
        """The merge step already has its own test for this. "Nobody reads the
        logs" matters most for whoever runs the phase by hand: without these
        numbers they can't tell if two cases were missing or nine hundred."""
        with tempfile.TemporaryDirectory() as temporanea:
            _codice, _decisioni, rapporto, schermo = esegui(
                Path(temporanea),
                [voce(descrizione="UNO"), voce(riga=13, descrizione="DUE")],
                trasporto_finto=trasporto({
                    "UNO": contenuto("ACCEPT", 441),
                    "DUE": OSError("rete giù"),
                }),
            )
        stampato = json.loads(schermo[schermo.index("{"):])
        self.assertEqual(stampato["casi_decisi"], rapporto["casi_decisi"])
        self.assertEqual(stampato["casi_senza_decisione"], 1)
        self.assertEqual(stampato["parallelismo"], rapporto["parallelismo"])

    def test_il_rapporto_non_contiene_mai_la_chiave(self) -> None:
        """The key travels in every call's headers: a single error message
        that echoes the request back is enough for it to end up in a file
        someone later attaches to a report."""
        with tempfile.TemporaryDirectory() as temporanea:
            _codice, decisioni, rapporto, schermo = esegui(
                Path(temporanea),
                [voce()],
                trasporto_finto=trasporto({
                    "PANTERA SHAMPOO 250ML RICCI NEW": (
                        401,
                        {"error": {"message": f"chiave non valida: {CHIAVE_FINTA}"}},
                    )
                }),
            )
        testo = json.dumps(rapporto, ensure_ascii=False) + json.dumps(decisioni) + schermo
        self.assertNotIn(CHIAVE_FINTA, testo)


class IlDegradoEUnValoreDiRitornoTests(unittest.TestCase):
    """"If OpenRouter doesn't answer, the program keeps going" is a
    deliberate choice: a degraded phase exits with the files in place and a
    code that says so, not with an exception and no artifacts."""

    def test_senza_chiave_i_file_ci_sono_lo_stesso_ed_esce_cinque(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            codice, decisioni, rapporto, _schermo = esegui(
                Path(temporanea), [voce()], chiave="", trasporto_finto=trasporto({})
            )
        self.assertEqual(codice, USCITA_DEGRADATO)
        self.assertEqual(decisioni, [])
        self.assertEqual(rapporto["per_stato"], {"SENZA_CHIAVE": 1})
        self.assertIn("SENZA_CHIAVE", rapporto["motivo_degrado"])

    def test_il_tetto_di_spesa_raggiunto_non_e_un_errore_d_uso(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            codice, decisioni, rapporto, _schermo = esegui(
                Path(temporanea),
                [voce(descrizione="UNO"), voce(riga=13, descrizione="DUE")],
                modifiche={"tetto_chiamate": 1, "tetto_spesa_usd": 100.0},
                trasporto_finto=trasporto({
                    "UNO": contenuto("REJECT", None),
                    "DUE": contenuto("REJECT", None),
                }),
            )
        self.assertEqual(codice, USCITA_DEGRADATO)
        self.assertEqual(len(decisioni), 1)
        self.assertIn("TETTO_CHIAMATE", rapporto["per_stato"])

    def test_un_guasto_imprevisto_scrive_lo_stesso_i_due_file(self) -> None:
        """Without this safety net the orchestrator would get a traceback and
        no artifacts — and the previous run's `resolved_matches.json` would
        stay on disk to be read as fresh."""

        def esplode(configurazione, memoria):
            raise RuntimeError("il client non si costruisce")

        with tempfile.TemporaryDirectory() as temporanea:
            codice, decisioni, rapporto, _schermo = esegui(
                Path(temporanea), [voce()], crea_client=esplode
            )
        self.assertEqual(codice, USCITA_DEGRADATO)
        self.assertEqual(decisioni, [])
        self.assertIn("RuntimeError", rapporto["motivo_degrado"])
        self.assertIn("il client non si costruisce", rapporto["motivo_degrado"])

    def test_un_guasto_imprevisto_non_lascia_uscire_la_chiave(self) -> None:
        def esplode(configurazione, memoria):
            raise RuntimeError(f"Authorization: Bearer {CHIAVE_FINTA}")

        with tempfile.TemporaryDirectory() as temporanea:
            _codice, _decisioni, rapporto, _schermo = esegui(
                Path(temporanea), [voce()], crea_client=esplode
            )
        self.assertNotIn(CHIAVE_FINTA, json.dumps(rapporto, ensure_ascii=False))

    def test_tutto_valutato_esce_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            codice, _decisioni, rapporto, _schermo = esegui(
                Path(temporanea),
                [voce(descrizione="UNO"), voce(riga=13, descrizione="DUE")],
                trasporto_finto=trasporto({
                    "UNO": contenuto("ACCEPT", 441),
                    "DUE": contenuto("UNRESOLVED", None),
                }),
            )
        self.assertEqual(codice, USCITA_OK)
        self.assertFalse(rapporto["degradato"])
        self.assertEqual(rapporto["motivo_degrado"], "")


class LaVerificaAvversarialeArrivaFinQuiTests(unittest.TestCase):
    """The second, adversarial pass isn't a client implementation detail: it's
    what makes `ALTA` trustworthy, and the reason
    `merge_match_decisions.py` lets an `ALTA` decision into an order without
    asking anyone."""

    def test_un_accept_non_confermato_scende_a_unresolved(self) -> None:
        chiamate: list[list[int]] = []

        def chiamata(url, corpo, intestazioni, timeout):
            righe = righe_mostrate(corpo)
            chiamate.append(righe)
            # The second pass shows a single candidate: that's how it's identified.
            if len(righe) == 1:
                return risposta_ok(contenuto("REJECT", None, "ALTA", "non è lo stesso formato"))
            return risposta_ok(contenuto("ACCEPT", 441))

        with tempfile.TemporaryDirectory() as temporanea:
            _codice, decisioni, _rapporto, _schermo = esegui(
                Path(temporanea), [voce()], trasporto_finto=chiamata
            )
        self.assertEqual(len(chiamate), 2)
        self.assertEqual(decisioni[0]["action"], "UNRESOLVED")
        self.assertIsNone(decisioni[0]["source_row"])
        self.assertIn("verifica avversariale", decisioni[0]["rationale"])


class LaCliVeraTests(unittest.TestCase):
    """The script run the way the orchestrator runs it: as a subprocess.

    None of these tests touch the network — either the input is malformed,
    or there's no case with candidates to send."""

    def lancia(self, argomenti: list[str]) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [sys.executable, str(SKILL_ROOT / "scripts" / "valuta_shortlist.py"), *argomenti],
            capture_output=True,
        )

    def test_i_tre_percorsi_sono_obbligatori(self) -> None:
        """Making `--rapporto` optional would reopen, from another angle, the
        same gap `--decisions-attese` closes: forgetting it would be enough."""
        with tempfile.TemporaryDirectory() as temporanea:
            cartella = Path(temporanea)
            shortlists = cartella / "sl.json"
            shortlists.write_bytes(b"[]")
            completi = [
                "--shortlists", str(shortlists),
                "--output", str(cartella / "dec.json"),
                "--rapporto", str(cartella / "rap.json"),
            ]
            for mancante in ("--shortlists", "--output", "--rapporto"):
                with self.subTest(mancante):
                    indice = completi.index(mancante)
                    ridotti = completi[:indice] + completi[indice + 2:]
                    esito = self.lancia(ridotti + ["--memoria", str(cartella / "mem.json")])
                    self.assertEqual(esito.returncode, 2)
                    self.assertIn(mancante, esito.stderr.decode("utf-8", errors="replace"))

    def test_un_ingresso_illeggibile_esce_due_senza_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            cartella = Path(temporanea)
            esito = self.lancia([
                "--shortlists", str(cartella / "non-c-e.json"),
                "--output", str(cartella / "dec.json"),
                "--rapporto", str(cartella / "rap.json"),
                "--memoria", str(cartella / "mem.json"),
            ])
        self.assertEqual(esito.returncode, USCITA_INGRESSO_NON_UTILIZZABILE)
        self.assertIn("Ingresso non utilizzabile", esito.stdout.decode("utf-8"))
        self.assertNotIn("Traceback", esito.stderr.decode("utf-8"))

    def test_l_uscita_e_utf8_qualunque_sia_la_console(self) -> None:
        """On Windows a process writing to a pipe uses cp1252, and real
        product descriptions pass through here: a single `CAFFÈ` is enough
        to hand the orchestrator bytes that aren't UTF-8."""
        with tempfile.TemporaryDirectory() as temporanea:
            cartella = Path(temporanea)
            shortlists = cartella / "sl.json"
            shortlists.write_bytes(
                json.dumps([{**voce(), "description": ["CAFFÈ MISCELA PERÙ"]}], ensure_ascii=False).encode("utf-8")
            )
            esito = self.lancia([
                "--shortlists", str(shortlists),
                "--output", str(cartella / "dec.json"),
                "--rapporto", str(cartella / "rap.json"),
                "--memoria", str(cartella / "mem.json"),
            ])
        self.assertEqual(esito.returncode, USCITA_INGRESSO_NON_UTILIZZABILE)
        self.assertIn("CAFFÈ MISCELA PERÙ", esito.stdout.decode("utf-8"))

    def test_senza_casi_da_valutare_scrive_i_due_file_ed_esce_zero(self) -> None:
        """The chain must be able to continue even when EAN matching resolved
        everything: `build_review_data.py` without `--resolved` produces an
        incomplete comparison.

        An empty queue is an empty list, not a list of entries with no
        candidates: the latter means a price list failed to load, and exits
        5."""
        with tempfile.TemporaryDirectory() as temporanea:
            cartella = Path(temporanea)
            shortlists = cartella / "sl.json"
            shortlists.write_bytes(b"[]")
            decisioni = cartella / "dec.json"
            rapporto = cartella / "rap.json"
            esito = self.lancia([
                "--shortlists", str(shortlists),
                "--output", str(decisioni),
                "--rapporto", str(rapporto),
                "--memoria", str(cartella / "mem.json"),
            ])
            self.assertEqual(esito.returncode, USCITA_OK, esito.stdout.decode("utf-8", "replace"))
            self.assertEqual(json.loads(decisioni.read_text(encoding="utf-8")), [])
            contabilita = json.loads(rapporto.read_text(encoding="utf-8"))
        self.assertEqual(contabilita["casi_ricevuti"], 0)
        self.assertEqual(contabilita["chiamate"], 0)
        self.assertFalse(contabilita["degradato"])

    def test_un_uscita_non_scrivibile_esce_due_e_non_finge_di_aver_deciso(self) -> None:
        """Exit code 2, not 0: neither file was written. Exiting 0 would tell
        the orchestrator "evaluated everything there was", and the previous
        run's `ai_decisions.json` would stay on disk to be read as fresh —
        exactly the failure half of this CLI exists to guard against. A
        mutation replacing 2 with 0 would otherwise go unnoticed."""
        with tempfile.TemporaryDirectory() as temporanea:
            cartella = Path(temporanea)
            shortlists = cartella / "sl.json"
            shortlists.write_bytes(json.dumps([voce(candidati=())], ensure_ascii=False).encode("utf-8"))
            # A directory in place of the file: `write_bytes` raises `OSError`.
            impossibile = cartella / "dec.json"
            impossibile.mkdir()
            esito = self.lancia([
                "--shortlists", str(shortlists),
                "--output", str(impossibile),
                "--rapporto", str(cartella / "rap.json"),
                "--memoria", str(cartella / "mem.json"),
            ])
            rapporto_scritto = (cartella / "rap.json").exists()
        self.assertEqual(esito.returncode, USCITA_INGRESSO_NON_UTILIZZABILE)
        self.assertIn("Uscite non scrivibili", esito.stdout.decode("utf-8"))
        self.assertNotIn("Traceback", esito.stderr.decode("utf-8"))
        self.assertFalse(rapporto_scritto)

    def test_la_memoria_predefinita_e_quella_del_programma(self) -> None:
        """An identical case isn't paid for twice, not even across runs: the
        default value is the real file, not a temp directory."""
        args = valuta_shortlist.parse_args([
            "--shortlists", "a.json", "--output", "b.json", "--rapporto", "c.json",
        ])
        self.assertEqual(args.memoria.name, "memoria_ai.json")
        self.assertEqual(args.memoria.parent.name, "data")


class LImprontaAttraversaLaCatenaTests(unittest.TestCase):
    """The two code paths that compute the fingerprint — here from the case
    sent to the model, in the merge step from today's shortlist — must
    produce the same value.

    This is the only test that would notice if they diverged: taken alone,
    `valuta_shortlist` and `merge_match_decisions` are each internally
    consistent."""

    def prepara(
        self,
        cartella: Path,
        shortlists: list[dict[str, Any]],
        matching_in_piu: list[dict[str, Any]] | None = None,
    ) -> tuple[Path, Path, Path]:
        percorso_shortlists = cartella / "semantic_shortlists.json"
        percorso_shortlists.write_bytes(json.dumps(shortlists, ensure_ascii=False).encode("utf-8"))

        matching = [{
            "gestionale": {"source_row": v["gestionale_source_row"], "description": v["description"], "ean": v["ean"]},
            "suppliers": {v["supplier"]: {"status": "EAN_ASSENTE", "usable_candidates": []}},
        } for v in shortlists] + list(matching_in_piu or [])
        percorso_matching = cartella / "matching.json"
        percorso_matching.write_bytes(json.dumps(matching, ensure_ascii=False).encode("utf-8"))

        normalized: dict[str, list[dict[str, Any]]] = {}
        for v in shortlists:
            righe = normalized.setdefault(v["supplier"], [])
            for candidato in v["candidates"]:
                righe.append({
                    "source_row": candidato["source_row"],
                    "ean": candidato["ean"],
                    "description": candidato["description"],
                    "unit_price_net": candidato["unit_price_net"],
                    "order_multiplier": 6,
                    "usable": True,
                })
        percorso_normalized = cartella / "normalized.json"
        percorso_normalized.write_bytes(json.dumps(normalized, ensure_ascii=False).encode("utf-8"))
        return percorso_matching, percorso_normalized, percorso_shortlists

    def merge(
        self,
        cartella: Path,
        shortlists: list[dict[str, Any]],
        decisioni: Path,
        attese: int,
        matching_in_piu: list[dict[str, Any]] | None = None,
    ):
        percorso_matching, percorso_normalized, percorso_shortlists = self.prepara(
            cartella, shortlists, matching_in_piu
        )
        uscita = cartella / "resolved.json"
        esito = subprocess.run(
            [
                sys.executable, str(SKILL_ROOT / "scripts" / "merge_match_decisions.py"),
                "--matching", str(percorso_matching),
                "--normalized", str(percorso_normalized),
                "--shortlists", str(percorso_shortlists),
                "--decisions", str(decisioni),
                "--decisions-attese", str(attese),
                "--output", str(uscita),
            ],
            capture_output=True, text=True, encoding="utf-8",
        )
        return esito, json.loads(uscita.read_text(encoding="utf-8"))

    def decidi(self, cartella: Path, shortlists: list[dict[str, Any]]) -> Path:
        codice, decisioni, _rapporto, _schermo = esegui(
            cartella,
            shortlists,
            trasporto_finto=trasporto(
                {"PANTERA SHAMPOO 250ML RICCI NEW": contenuto("ACCEPT", 441)}
            ),
        )
        self.assertEqual(codice, USCITA_OK)
        self.assertEqual(len(decisioni), 1)
        return cartella / "ai_decisions.json"

    def giro(self, cartella: Path, shortlists: list[dict[str, Any]], descrizione: str):
        """A full round trip: `valuta_shortlist` writes, `merge` recomputes."""
        codice, decisioni, _rapporto, _schermo = esegui(
            cartella, shortlists, trasporto_finto=trasporto({descrizione: contenuto("ACCEPT", 441)})
        )
        self.assertEqual(codice, USCITA_OK)
        self.assertEqual(len(decisioni), 1)
        return self.merge(cartella, shortlists, cartella / "ai_decisions.json", 1)

    def test_il_punteggio_scritto_come_testo_non_rompe_il_confronto(self) -> None:
        """`voce()` already shows that real price lists carry the price as a
        string. If an adapter ever does the same with the score, the two
        fingerprint computations would read `0.81` and `"0.81"`: this is
        exactly why `_confrontabile` exists, and no other test exercised it
        end to end."""
        una = voce()
        una["candidates"][0]["score"] = "0.81"
        una["candidates"][1]["score"] = "0.42"
        with tempfile.TemporaryDirectory() as temporanea:
            esito, risolti = self.giro(Path(temporanea), [una], "PANTERA SHAMPOO 250ML RICCI NEW")
        self.assertEqual(esito.returncode, 0, esito.stdout + esito.stderr)
        self.assertEqual(risolti[0]["suppliers"]["betulla"]["status"], "SEMANTICO_PROPOSTO")

    def test_gli_spazi_e_le_accentate_non_rompono_il_confronto(self) -> None:
        """`test_le_descrizioni_non_si_normalizzano` proves this side leaves
        the text untouched; this proves the other side does too. They're the
        two halves of the same invariant."""
        descrizione = "  CAFFÈ  MISCELA  PERÙ  250G  "
        with tempfile.TemporaryDirectory() as temporanea:
            esito, risolti = self.giro(Path(temporanea), [voce(descrizione=descrizione)], descrizione)
        self.assertEqual(esito.returncode, 0, esito.stdout + esito.stderr)
        self.assertEqual(risolti[0]["suppliers"]["betulla"]["status"], "SEMANTICO_PROPOSTO")

    def test_la_riga_scritta_come_testo_non_rompe_il_confronto(self) -> None:
        """`numero_di_riga` accepts `"12"`, but in the merge step
        `shortlist_index` and the loop key must be normalized the same way
        the decisions are, to an integer: otherwise the pair stops matching,
        every decision becomes "unmatched" and the merge exits 3 — sending
        the orchestrator to repay for the AI phase on a healthy run."""
        with tempfile.TemporaryDirectory() as temporanea:
            esito, risolti = self.giro(
                Path(temporanea), [{**voce(), "gestionale_source_row": "12"}],
                "PANTERA SHAMPOO 250ML RICCI NEW",
            )
        self.assertEqual(esito.returncode, 0, esito.stdout + esito.stderr)
        self.assertEqual(risolti[0]["suppliers"]["betulla"]["status"], "SEMANTICO_PROPOSTO")

    def test_una_coppia_semantica_senza_shortlist_non_passa_per_valutata(self) -> None:
        """The second of two blocking gaps. With 500 shortlists out of 948
        pairs, the AI phase evaluates 500, declares 500, the merge step
        finds 500, and everything reconciles — because both sides are blind
        to the same missing pairs. Without this check, 448 products would
        silently vanish while the pipeline exits 0."""
        with tempfile.TemporaryDirectory() as temporanea:
            cartella = Path(temporanea)
            shortlists = [voce()]
            percorso = self.decidi(cartella, shortlists)
            # The matching file declares two semantic pairs, the shortlist only one.
            esito, risolti = self.merge(
                cartella, shortlists, percorso, 1,
                matching_in_piu=[{
                    "gestionale": {"source_row": 99, "description": "MAI VALUTATO", "ean": "8009"},
                    "suppliers": {"betulla": {"status": "EAN_ASSENTE", "usable_candidates": []}},
                }],
            )
        self.assertEqual(esito.returncode, 3, esito.stdout + esito.stderr)
        riepilogo = json.loads(esito.stdout)
        self.assertEqual(riepilogo["coppie_senza_shortlist"], 1)
        self.assertEqual(riepilogo["senza_shortlist"], [{"gestionale_source_row": 99, "supplier": "betulla"}])
        mancante = [r for r in risolti if r["gestionale"]["source_row"] == 99][0]["suppliers"]["betulla"]
        self.assertEqual(mancante["status"], "DA_VERIFICARE")
        self.assertIn("Nessuna shortlist", mancante["rationale"])

    def test_la_decisione_della_run_corrente_entra_nel_confronto(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            cartella = Path(temporanea)
            shortlists = [voce()]
            percorso = self.decidi(cartella, shortlists)
            esito, risolti = self.merge(cartella, shortlists, percorso, 1)
        self.assertEqual(esito.returncode, 0, esito.stdout + esito.stderr)
        riepilogo = json.loads(esito.stdout)
        self.assertEqual(riepilogo["decisioni_scartate_perche_di_un_altra_run"], 0)
        match = risolti[0]["suppliers"]["betulla"]
        self.assertEqual(match["status"], "SEMANTICO_PROPOSTO")
        self.assertEqual(match["selected"]["source_row"], 441)

    def test_la_stessa_decisione_su_una_shortlist_di_un_altra_run_viene_buttata(self) -> None:
        """The reorder list is the same file week to week: the pair matches,
        the counts reconcile, and every other guard compares today's
        artifacts against each other. Without the fingerprint, this decision
        would enter the order at `ALTA` confidence and unconfirmed."""
        with tempfile.TemporaryDirectory() as temporanea:
            cartella = Path(temporanea)
            percorso = self.decidi(cartella, [voce()])
            # Same pair, same row 441 in the price list: only what the model
            # would have seen changes, i.e. the shortlist candidates.
            di_oggi = [voce(candidati=((441, "PANTERA SH.250 RICCI", 0.81), (777, "DASY LAVATRICE 30 LAV", 0.55)))]
            esito, risolti = self.merge(cartella, di_oggi, percorso, 1)
        self.assertEqual(esito.returncode, 3, esito.stdout + esito.stderr)
        riepilogo = json.loads(esito.stdout)
        self.assertEqual(riepilogo["decisioni_scartate_perche_di_un_altra_run"], 1)
        match = risolti[0]["suppliers"]["betulla"]
        self.assertEqual(match["status"], "DA_VERIFICARE")
        self.assertEqual(match["ai_decisione_scartata"], "DECISIONE_DI_UNA_ALTRA_RUN")
        self.assertIsNone(match["selected"])


if __name__ == "__main__":
    unittest.main()
