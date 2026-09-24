"""Tests for `merge_match_decisions.py`, which decides whether a model-proposed
match enters an order unreviewed.

Covered here:

- an `ACCEPT` at `ALTA` confidence skips confirmation; everything else asks for it;
- every `REJECT` carries its best candidate's score, the only trace left of a
  wrong rejection;
- an accepted row is verified against the shortlist (what the model
  actually saw), not against `matching_result.json`, which is written
  together with the price list and so can never diverge from it;
- a row never shown to the model can't enter an order;
- `--decisions-attese` is mandatory, and zero is a legitimate value;
- counting decisions doesn't prove they belong to this run.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from build_semantic_shortlists import impronta_caso  # noqa: E402
from merge_match_decisions import (  # noqa: E402
    CONFIDENZA_SENZA_CONFERMA,
    METODO_STESSO_CODICE,
    SOGLIA_RIFIUTO_SOSPETTO,
    USCITA_DECISIONI_NON_RICONCILIATE,
    USCITA_INGRESSO_NON_UTILIZZABILE,
    USCITA_LISTINO_DISALLINEATO,
    USCITA_OK,
    USCITA_RIGA_INVENTATA,
    identita,
    impronta_attesa,
    miglior_punteggio,
    indice_per_codice,
    prezzo_confrontabile,
    propaga_lo_stesso_codice,
)


# Not passing `attese` at all and passing `attese=None` are different: the
# latter is what proves the flag is mandatory.
DERIVA = object()


def scrivi(cartella: Path, nome: str, documento: Any) -> Path:
    percorso = cartella / nome
    percorso.write_text(json.dumps(documento, ensure_ascii=False), encoding="utf-8")
    return percorso


def normalizzato_dalle_shortlist(shortlists: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the normalized price list that matches the given shortlists.

    In the real pipeline, `semantic_shortlists.json` is built by reading
    `normalized_sources.json`, so the two files always agree row for row.
    Tests start from that baseline; the interesting case is when they stop
    agreeing, which is then constructed on purpose."""
    per_fornitore: dict[str, dict[int, dict[str, Any]]] = {}
    for shortlist in shortlists:
        righe = per_fornitore.setdefault(shortlist["supplier"], {})
        for candidato in shortlist.get("candidates", []):
            righe[candidato["source_row"]] = {
                "source_row": candidato["source_row"],
                "ean": candidato.get("ean", ""),
                "description": candidato.get("description", ""),
                "unit_price_net": candidato.get("unit_price_net", 1.0),
                # The field the shortlist doesn't carry but the comparison
                # needs: why the full record has to come from the price list.
                "order_multiplier": 6,
                "usable": True,
            }
    return {fornitore: list(righe.values()) for fornitore, righe in per_fornitore.items()} or {"betulla": []}


def matching_di_prova(stato: str = "EAN_ASSENTE", usable: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    return [
        {
            "gestionale": {"source_row": 12, "description": "PANTERA SHAMPOO 250ML RICCI NEW", "ean": "8001"},
            "suppliers": {"betulla": {"status": stato, "usable_candidates": usable or []}},
        }
    ]


def shortlist_di_prova(punteggi: list[float]) -> list[dict[str, Any]]:
    return [
        {
            "gestionale_source_row": 12,
            "supplier": "betulla",
            "candidates": [
                {"source_row": 100 + indice, "description": f"CANDIDATO {indice}",
                 "unit_price_net": 1.0, "score": punteggio}
                for indice, punteggio in enumerate(punteggi)
            ],
        }
    ]


def timbra(
    decisions: list[dict[str, Any]], shortlists: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Stamp each decision with its case fingerprint, like `valuta_shortlist.py` does.

    In the real pipeline whoever called the model writes it, and a decision
    without it is never applied. Here the test harness adds it, so tests
    about something else don't have to; to test its absence, or a wrong
    fingerprint, pass `timbra=False` or set it yourself — a decision that
    already has one is left untouched."""
    def coppia(riga: Any, fornitore: Any) -> tuple[Any, Any]:
        # Same rule as `indicizza_decisioni`: `"12"` and `12` are the same
        # pair, and one test writes the row as a string on purpose.
        try:
            riga = int(riga)
        except (TypeError, ValueError):
            pass
        return (riga, str(fornitore))

    per_coppia = {
        coppia(voce.get("gestionale_source_row"), voce.get("supplier")): voce for voce in shortlists
    }
    timbrate = []
    for decisione in decisions:
        if "ai_impronta_caso" in decisione:
            timbrate.append(decisione)
            continue
        shortlist = per_coppia.get(
            coppia(decisione.get("gestionale_source_row"), decisione.get("supplier")),
            {"candidates": []},
        )
        timbrate.append({**decisione, "ai_impronta_caso": impronta_attesa(shortlist)})
    return timbrate


def esegui_grezzo(
    cartella: Path,
    *,
    matching: list[dict[str, Any]],
    shortlists: list[dict[str, Any]],
    decisions: list[dict[str, Any]] | None,
    normalized: dict[str, Any] | None = None,
    attese: Any = DERIVA,
    con_impronta: bool = True,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run the script as a real subprocess, like the pipeline does, and return
    what the pipeline sees: exit code, stdout and the output path.

    `decisions=None` means `--decisions` isn't passed at all, i.e. the AI
    phase never ran. `attese=None` means `--decisions-attese` isn't passed.
    `con_impronta=False` writes the decisions as given, without the
    fingerprint the AI phase always attaches."""
    if decisions is not None and con_impronta:
        decisions = timbra(decisions, shortlists)
    percorso_matching = scrivi(cartella, "matching.json", matching)
    percorso_normalized = scrivi(
        cartella, "normalized.json", normalized if normalized is not None else normalizzato_dalle_shortlist(shortlists)
    )
    percorso_shortlists = scrivi(cartella, "shortlists.json", shortlists)
    percorso_output = cartella / "resolved.json"

    comando = [
        sys.executable,
        str(SCRIPTS / "merge_match_decisions.py"),
        "--matching", str(percorso_matching),
        "--normalized", str(percorso_normalized),
        "--shortlists", str(percorso_shortlists),
        "--output", str(percorso_output),
    ]
    if decisions is not None:
        comando += ["--decisions", str(scrivi(cartella, "decisions.json", decisions))]
    if attese is DERIVA:
        attese = len(decisions or [])
    if attese is not None:
        comando += ["--decisions-attese", str(attese)]

    return subprocess.run(comando, capture_output=True, text=True, encoding="utf-8"), percorso_output


def esegui(
    cartella: Path,
    *,
    matching: list[dict[str, Any]],
    shortlists: list[dict[str, Any]],
    decisions: list[dict[str, Any]] | None,
    normalized: dict[str, Any] | None = None,
    attese: Any = DERIVA,
    con_impronta: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Like above, but asserts success and returns the resolved matches and summary."""
    esito, percorso_output = esegui_grezzo(
        cartella, matching=matching, shortlists=shortlists,
        decisions=decisions, normalized=normalized, attese=attese,
        con_impronta=con_impronta,
    )
    if esito.returncode != USCITA_OK:
        raise AssertionError(f"lo script è fallito ({esito.returncode}): {esito.stdout}{esito.stderr}")
    return (
        json.loads(percorso_output.read_text(encoding="utf-8")),
        json.loads(esito.stdout),
    )


# Two products, not one: the real file has 942, and with a single product an
# order-dependent bug has no way to show up.
DUE_PRODOTTI = [
    {"gestionale": {"source_row": riga, "description": f"PRODOTTO {riga}", "ean": f"800{riga}"},
     "suppliers": {"betulla": {"status": "EAN_ASSENTE", "usable_candidates": []}}}
    for riga in (12, 13)
]

DUE_SHORTLIST = [
    {"gestionale_source_row": 12, "supplier": "betulla", "description": "PRODOTTO 12",
     "candidates": [{"source_row": 100, "ean": "8100", "description": "CANDIDATO A",
                     "unit_price_net": 1.0, "score": 0.72}]},
    {"gestionale_source_row": 13, "supplier": "betulla", "description": "PRODOTTO 13",
     "candidates": [{"source_row": 200, "ean": "8200", "description": "CANDIDATO B",
                     "unit_price_net": 2.0, "score": 0.66}]},
]


ACCETTA_LA_100 = [{
    "gestionale_source_row": 12,
    "supplier": "betulla",
    "action": "ACCEPT",
    "source_row": 100,
    "confidence": "ALTA",
    "rationale": "stesso prodotto",
}]


class GliEsitiSonoUnContrattoTests(unittest.TestCase):
    """The exit codes are a contract with callers: if two failures collapse
    onto the same code, callers can no longer tell them apart."""

    def test_sono_i_numeri_promessi(self) -> None:
        self.assertEqual(USCITA_OK, 0)
        self.assertEqual(USCITA_INGRESSO_NON_UTILIZZABILE, 2)
        self.assertEqual(USCITA_DECISIONI_NON_RICONCILIATE, 3)
        self.assertEqual(USCITA_LISTINO_DISALLINEATO, 4)
        self.assertEqual(USCITA_RIGA_INVENTATA, 5)

    def test_l_uscita_e_utf8_anche_con_le_accentate(self) -> None:
        """The summary carries the real product descriptions. On Windows a
        process writing to a pipe uses cp1252: a single `CAFFÈ` is enough to
        hand the reader bytes that aren't UTF-8, and the orchestrator reads
        exactly this output."""
        with tempfile.TemporaryDirectory() as temporanea:
            esito, _percorso = esegui_grezzo(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=[{
                    "gestionale_source_row": 12, "supplier": "betulla",
                    "candidates": [{"source_row": 100, "description": "CAFFÈ MISCELA D'ORO 250 G",
                                    "unit_price_net": 1.0, "score": 0.72}],
                }],
                decisions=ACCETTA_LA_100,
                normalized={"betulla": [{"source_row": 100, "description": "TUTT'ALTRO",
                                       "unit_price_net": 1.0, "order_multiplier": 6}]},
            )
        self.assertEqual(esito.returncode, USCITA_LISTINO_DISALLINEATO)
        riepilogo = json.loads(esito.stdout)
        self.assertEqual(riepilogo["disallineamenti"][0]["mostrato_al_modello"], "CAFFÈ MISCELA D'ORO 250 G")

    def test_i_guasti_non_si_confondono_fra_loro(self) -> None:
        esiti = [USCITA_OK, USCITA_INGRESSO_NON_UTILIZZABILE,
                 USCITA_DECISIONI_NON_RICONCILIATE, USCITA_LISTINO_DISALLINEATO,
                 USCITA_RIGA_INVENTATA]
        self.assertEqual(len(set(esiti)), len(esiti))


class MigliorPunteggioTests(unittest.TestCase):
    def test_prende_il_massimo_non_il_primo(self) -> None:
        """Doesn't read `candidates[0]`: ordering belongs to whoever wrote the
        shortlist, and this function must not depend on it."""
        shortlist = {"candidates": [{"score": 0.30}, {"score": 0.91}, {"score": 0.44}]}
        self.assertAlmostEqual(miglior_punteggio(shortlist), 0.91)

    def test_senza_punteggi_e_none_non_zero(self) -> None:
        """"Don't know" and "scored zero similarity" are different things:
        the latter would make a rejection nobody knows anything about look
        harmless."""
        self.assertIsNone(miglior_punteggio({"candidates": [{"description": "senza punteggio"}]}))
        self.assertIsNone(miglior_punteggio({"candidates": []}))
        self.assertIsNone(miglior_punteggio({}))

    def test_un_booleano_non_e_un_punteggio(self) -> None:
        """In Python `True` is an int and would pass as 1.0: the highest score
        possible, produced by malformed data."""
        self.assertIsNone(miglior_punteggio({"candidates": [{"score": True}]}))


class IdentitaTests(unittest.TestCase):
    """What proves two rows are the same product."""

    def test_spazi_e_maiuscole_non_contano(self) -> None:
        """Identity must not be stricter than it needs to be: two spellings of
        the same description are the same product."""
        self.assertEqual(
            identita({"ean": " 8001 ", "description": "pantera  shampoo", "unit_price_net": 1.0}),
            identita({"ean": "8001", "description": "PANTERA SHAMPOO", "unit_price_net": 1.0}),
        )

    def test_un_ean_nullo_e_uno_vuoto_sono_lo_stesso_prodotto(self) -> None:
        """In real price lists a missing EAN arrives from the JSON as `null`,
        not as an absent key: `null` vs `""` would be a fabricated mismatch
        that stops a good run."""
        self.assertEqual(
            identita({"ean": None, "description": "X", "unit_price_net": 1.0}),
            identita({"ean": "", "description": "X", "unit_price_net": 1.0}),
        )

    def test_una_descrizione_nulla_e_una_vuota_sono_lo_stesso_prodotto(self) -> None:
        self.assertEqual(
            identita({"ean": "8001", "description": None, "unit_price_net": 1.0}),
            identita({"ean": "8001", "description": "", "unit_price_net": 1.0}),
        )

    def test_il_prezzo_fa_parte_dell_identita(self) -> None:
        """Two batches of the same item at a different price aren't
        interchangeable: the row that goes into the order is the one the
        price is taken from."""
        self.assertNotEqual(
            identita({"ean": "8001", "description": "X", "unit_price_net": 2.18}),
            identita({"ean": "8001", "description": "X", "unit_price_net": 99.99}),
        )

    def test_un_prezzo_assente_non_diventa_zero(self) -> None:
        self.assertIsNone(identita({"ean": "8001", "description": "X"})[2])
        self.assertIsNone(identita({"ean": "8001", "description": "X", "unit_price_net": None})[2])
        self.assertIsNone(identita({"ean": "8001", "description": "X", "unit_price_net": True})[2])

    def test_un_prezzo_in_stringa_e_un_prezzo(self) -> None:
        """`prepare_sources.py` writes prices as strings, so the identity
        check must compare them numerically: an earlier version that
        accepted only numbers silently ignored the price on every real row."""
        self.assertEqual(prezzo_confrontabile("2.1000"), 2.1)
        self.assertNotEqual(
            identita({"ean": "8001", "description": "X", "unit_price_net": "1.0000"}),
            identita({"ean": "8001", "description": "X", "unit_price_net": "9.9900"}),
        )

    def test_lo_stesso_numero_scritto_in_due_modi_e_lo_stesso_prezzo(self) -> None:
        """The opposite direction matters just as much: if one artifact carries
        `1.0` and the other `"1.0"`, two spellings of the same number must not
        become two different products and stop a healthy run."""
        self.assertEqual(
            identita({"ean": "8001", "description": "X", "unit_price_net": 1.0}),
            identita({"ean": "8001", "description": "X", "unit_price_net": "1.0000"}),
        )

    def test_un_prezzo_che_non_e_un_numero_resta_none(self) -> None:
        self.assertIsNone(prezzo_confrontabile("n.d."))
        self.assertIsNone(prezzo_confrontabile(""))


class ConfermaDegliAccettatiTests(unittest.TestCase):
    """The confidence-gated confirmation rule, and the benchmark that backs it."""

    def _accetta_con(self, confidenza: str) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as temporanea:
            risolti, _riepilogo = esegui(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=shortlist_di_prova([0.72]),
                decisions=[{**ACCETTA_LA_100[0], "confidence": confidenza}],
            )
        return risolti[0]["suppliers"]["betulla"]

    def test_alta_entra_nell_ordine_senza_conferma(self) -> None:
        risultato = self._accetta_con(CONFIDENZA_SENZA_CONFERMA)
        self.assertIs(risultato["requires_user_confirmation"], False)
        self.assertEqual(risultato["status"], "SEMANTICO_PROPOSTO")

    def test_media_e_bassa_la_conferma_la_chiedono_ancora(self) -> None:
        for confidenza in ("MEDIA", "BASSA"):
            with self.subTest(confidenza=confidenza):
                self.assertIs(self._accetta_con(confidenza)["requires_user_confirmation"], True)

    def test_una_confidenza_sconosciuta_chiede_conferma(self) -> None:
        """An unrecognized value isn't `ALTA`: when in doubt, ask. It's the
        only direction this comparison can err in without causing harm."""
        self.assertIs(self._accetta_con("ALTISSIMA")["requires_user_confirmation"], True)
        self.assertIs(self._accetta_con("")["requires_user_confirmation"], True)

    def test_il_riepilogo_dice_quanti_sono_entrati_senza_conferma(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            _risolti, riepilogo = esegui(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=shortlist_di_prova([0.72]),
                decisions=ACCETTA_LA_100,
            )
        self.assertEqual(riepilogo["accettati_senza_conferma"], 1)

    def test_un_accettato_che_la_conferma_la_chiede_non_conta_fra_i_senza_conferma(self) -> None:
        """The count is meant to measure how much trust `ALTA` carries: if it
        also counts `MEDIA`, which does ask for confirmation, it stops
        meaning anything."""
        with tempfile.TemporaryDirectory() as temporanea:
            _risolti, riepilogo = esegui(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=shortlist_di_prova([0.72]),
                decisions=[{**ACCETTA_LA_100[0], "confidence": "MEDIA"}],
            )
        self.assertEqual(riepilogo["supplier_results"], {"SEMANTICO_PROPOSTO": 1})
        self.assertEqual(riepilogo["accettati_senza_conferma"], 0)


class RifiutiSilenziosiTests(unittest.TestCase):
    """A wrong rejection leaves no trace: the best candidate's score is the
    only signal available without paying for another call."""

    def _rifiuta_con(self, punteggi: list[float]) -> tuple[dict[str, Any], dict[str, Any]]:
        with tempfile.TemporaryDirectory() as temporanea:
            risolti, riepilogo = esegui(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=shortlist_di_prova(punteggi),
                decisions=[{
                    "gestionale_source_row": 12,
                    "supplier": "betulla",
                    "action": "REJECT",
                    "source_row": None,
                    "confidence": "ALTA",
                    "rationale": "nessun candidato equivalente",
                }],
            )
        return risolti[0]["suppliers"]["betulla"], riepilogo

    def test_il_punteggio_del_miglior_candidato_viaggia_col_rifiuto(self) -> None:
        risultato, _riepilogo = self._rifiuta_con([0.41, 0.83, 0.55])
        self.assertAlmostEqual(risultato["ai_reject_best_score"], 0.83)

    def test_viaggia_anche_quando_e_basso(self) -> None:
        """The threshold is applied once, by whoever displays the data: this
        score is always written, otherwise changing the threshold would mean
        redoing the run."""
        risultato, _riepilogo = self._rifiuta_con([0.11])
        self.assertAlmostEqual(risultato["ai_reject_best_score"], 0.11)

    def test_sopra_soglia_il_riepilogo_lo_conta(self) -> None:
        _risultato, riepilogo = self._rifiuta_con([SOGLIA_RIFIUTO_SOSPETTO + 0.05])
        self.assertEqual(riepilogo["rifiuti_con_candidato_forte"], 1)

    def test_sotto_soglia_non_lo_conta(self) -> None:
        _risultato, riepilogo = self._rifiuta_con([SOGLIA_RIFIUTO_SOSPETTO - 0.05])
        self.assertEqual(riepilogo["rifiuti_con_candidato_forte"], 0)

    def test_esattamente_sulla_soglia_conta(self) -> None:
        """The comparison is `>=`: a boundary case must fall on one declared
        side, not depend on how the code happens to be written."""
        _risultato, riepilogo = self._rifiuta_con([SOGLIA_RIFIUTO_SOSPETTO])
        self.assertEqual(riepilogo["rifiuti_con_candidato_forte"], 1)

    def test_una_shortlist_senza_punteggi_non_diventa_un_rifiuto_innocuo(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            risolti, riepilogo = esegui(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=[{
                    "gestionale_source_row": 12,
                    "supplier": "betulla",
                    "candidates": [{"source_row": 100, "description": "SENZA PUNTEGGIO"}],
                }],
                decisions=[{
                    "gestionale_source_row": 12,
                    "supplier": "betulla",
                    "action": "REJECT",
                    "source_row": None,
                    "confidence": "ALTA",
                    "rationale": "",
                }],
            )
        self.assertIsNone(risolti[0]["suppliers"]["betulla"]["ai_reject_best_score"])
        self.assertEqual(riepilogo["rifiuti_con_candidato_forte"], 0)

    def test_un_match_per_ean_non_porta_nessun_punteggio_di_rifiuto(self) -> None:
        """The field exists only where it means something: setting it
        everywhere would make it impossible to tell "rejected by the AI" from
        everything else."""
        with tempfile.TemporaryDirectory() as temporanea:
            risolti, _riepilogo = esegui(
                Path(temporanea),
                matching=[{
                    "gestionale": {"source_row": 12, "description": "PRODOTTO", "ean": "8001"},
                    "suppliers": {
                        "betulla": {
                            "status": "EAN_ESATTO",
                            "usable_candidates": [{"source_row": 100, "unit_price_net": 1.0, "description": "PRODOTTO"}],
                        }
                    },
                }],
                shortlists=[],
                decisions=[],
            )
        self.assertNotIn("ai_reject_best_score", risolti[0]["suppliers"]["betulla"])


class RigaRisoltaControCioCheIlModelloHaVistoTests(unittest.TestCase):
    """The row number identifies a position, not a product. If the price list
    is re-read and has one extra row at the top, row 100 is a different item
    — and an `ALTA` decision would put it in the order without asking
    anyone."""

    def _con_listino(self, righe: list[dict[str, Any]]) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
        cartella = Path(self.enterContext(tempfile.TemporaryDirectory()))
        esito, percorso = esegui_grezzo(
            cartella,
            matching=matching_di_prova(),
            shortlists=shortlist_di_prova([0.72]),
            decisions=ACCETTA_LA_100,
            normalized={"betulla": righe},
        )
        return esito, percorso, cartella

    def test_il_prodotto_giusto_passa_e_porta_il_record_completo(self) -> None:
        """The positive control: when the two files agree, the match goes
        through, and `selected` is the price-list record, not the shortlist
        candidate — which doesn't carry the order multiplier."""
        with tempfile.TemporaryDirectory() as temporanea:
            risolti, riepilogo = esegui(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=shortlist_di_prova([0.72]),
                decisions=ACCETTA_LA_100,
            )
        risultato = risolti[0]["suppliers"]["betulla"]
        self.assertEqual(risultato["status"], "SEMANTICO_PROPOSTO")
        self.assertEqual(risultato["selected"]["order_multiplier"], 6)
        self.assertEqual(riepilogo["decisioni_scartate_per_disallineamento"], 0)

    def test_un_listino_slittato_di_una_riga_scarta_la_decisione(self) -> None:
        """The harmful case: same row 100, different product."""
        esito, percorso, _cartella = self._con_listino([{
            "source_row": 100, "ean": "", "description": "TUTT ALTRO PRODOTTO",
            "unit_price_net": 9.99, "order_multiplier": 6,
        }])
        self.assertEqual(esito.returncode, USCITA_LISTINO_DISALLINEATO)
        riepilogo = json.loads(esito.stdout)
        self.assertEqual(riepilogo["decisioni_scartate_per_disallineamento"], 1)
        self.assertEqual(riepilogo["disallineamenti"][0]["mostrato_al_modello"], "CANDIDATO 0")
        self.assertEqual(riepilogo["disallineamenti"][0]["trovato_nel_listino"], "TUTT ALTRO PRODOTTO")
        # The file is still written, with the pair degraded: downstream,
        # `build_review_data` can't tell a missing file from an empty list,
        # and the previous run's file would otherwise be read as fresh.
        risolti = json.loads(percorso.read_text(encoding="utf-8"))
        risultato = risolti[0]["suppliers"]["betulla"]
        self.assertEqual(risultato["status"], "DA_VERIFICARE")
        self.assertIsNone(risultato["selected"])
        self.assertIs(risultato["requires_user_confirmation"], True)

    def test_una_decisione_scartata_non_conta_fra_gli_accettati_senza_conferma(self) -> None:
        """The summary must not count a match it discarded as a success."""
        esito, _percorso, _cartella = self._con_listino([{
            "source_row": 100, "ean": "", "description": "TUTT ALTRO",
            "unit_price_net": 9.99, "order_multiplier": 6,
        }])
        riepilogo = json.loads(esito.stdout)
        self.assertEqual(riepilogo["accettati_senza_conferma"], 0)
        self.assertEqual(riepilogo["supplier_results"], {"DA_VERIFICARE": 1})

    def test_un_ean_diverso_basta_da_solo(self) -> None:
        """Same description, different EAN: it's a different item, and this
        happens in real price lists — descriptions repeat, EANs don't."""
        with tempfile.TemporaryDirectory() as temporanea:
            esito, _percorso = esegui_grezzo(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=[{
                    "gestionale_source_row": 12,
                    "supplier": "betulla",
                    "candidates": [{"source_row": 100, "ean": "8001", "description": "CANDIDATO 0",
                                    "unit_price_net": 1.0, "score": 0.72}],
                }],
                decisions=ACCETTA_LA_100,
                normalized={"betulla": [{
                    "source_row": 100, "ean": "8002", "description": "CANDIDATO 0",
                    "unit_price_net": 1.0, "order_multiplier": 6,
                }]},
            )
        self.assertEqual(esito.returncode, USCITA_LISTINO_DISALLINEATO)

    def test_un_prezzo_diverso_basta_da_solo(self) -> None:
        """Same EAN, same description, different price: in real price lists
        that's two batches, and the one that ends up in the order must be
        the right one."""
        esito, _percorso, _cartella = self._con_listino([{
            "source_row": 100, "ean": "", "description": "CANDIDATO 0",
            "unit_price_net": 99.99, "order_multiplier": 6,
        }])
        self.assertEqual(esito.returncode, USCITA_LISTINO_DISALLINEATO)

    def test_una_riga_sparita_dal_listino_non_ripiega_sulla_shortlist(self) -> None:
        """A missing row must not fall back to the shortlist candidate, which
        doesn't carry the order multiplier: that would put the offer in the
        comparison as unavailable, with nothing saying why."""
        esito, _percorso, _cartella = self._con_listino([])
        self.assertEqual(esito.returncode, USCITA_LISTINO_DISALLINEATO)
        self.assertIsNone(json.loads(esito.stdout)["disallineamenti"][0]["trovato_nel_listino"])

    def test_un_listino_senza_ean_non_fa_morire_la_run(self) -> None:
        """`null` in the price list and `""` in the shortlist are the same
        product: a missing EAN is the norm, not a fault."""
        with tempfile.TemporaryDirectory() as temporanea:
            _risolti, riepilogo = esegui(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=shortlist_di_prova([0.72]),
                decisions=ACCETTA_LA_100,
                normalized={"betulla": [{
                    "source_row": 100, "ean": None, "description": "CANDIDATO 0",
                    "unit_price_net": 1.0, "order_multiplier": 6,
                }]},
            )
        self.assertEqual(riepilogo["decisioni_scartate_per_disallineamento"], 0)

    def test_un_ean_ambiguo_si_verifica_contro_la_shortlist(self) -> None:
        """For an `EAN_AMBIGUO` case, the candidate must not be taken from
        `matching_result.json`: `prepare_sources.py` writes it together with
        the normalized price list, so comparing them would compare the price
        list with itself and this guard would never fire. The model, instead,
        only ever sees the shortlist. Here the shortlist is stale and the
        price list isn't: the decision must be discarded."""
        with tempfile.TemporaryDirectory() as temporanea:
            esito, _percorso = esegui_grezzo(
                Path(temporanea),
                matching=matching_di_prova(
                    "EAN_AMBIGUO",
                    usable=[{"source_row": 100, "ean": "8001", "description": "PRODOTTO NUOVO",
                             "unit_price_net": 0.80, "order_multiplier": 6, "usable": True}],
                ),
                shortlists=[{
                    "gestionale_source_row": 12,
                    "supplier": "betulla",
                    "candidates": [{"source_row": 100, "ean": "8001", "description": "PRODOTTO VECCHIO",
                                    "unit_price_net": 2.18, "score": 0.72}],
                }],
                decisions=ACCETTA_LA_100,
                normalized={"betulla": [{
                    "source_row": 100, "ean": "8001", "description": "PRODOTTO NUOVO",
                    "unit_price_net": 0.80, "order_multiplier": 6,
                }]},
            )
        self.assertEqual(esito.returncode, USCITA_LISTINO_DISALLINEATO)
        riepilogo = json.loads(esito.stdout)
        self.assertEqual(riepilogo["disallineamenti"][0]["mostrato_al_modello"], "PRODOTTO VECCHIO")
        self.assertEqual(riepilogo["disallineamenti"][0]["trovato_nel_listino"], "PRODOTTO NUOVO")

    def test_un_ean_ambiguo_coerente_passa_e_si_riconosce(self) -> None:
        """The control: when the shortlist and the price list agree, an
        ambiguous EAN resolved by the AI stays distinguishable from a plain
        semantic match."""
        candidato = {"source_row": 100, "ean": "8001", "description": "PRODOTTO",
                     "unit_price_net": 2.18, "score": 0.72}
        with tempfile.TemporaryDirectory() as temporanea:
            risolti, _riepilogo = esegui(
                Path(temporanea),
                matching=matching_di_prova("EAN_AMBIGUO", usable=[dict(candidato, order_multiplier=6)]),
                shortlists=[{"gestionale_source_row": 12, "supplier": "betulla", "candidates": [candidato]}],
                decisions=ACCETTA_LA_100,
            )
        risultato = risolti[0]["suppliers"]["betulla"]
        self.assertEqual(risultato["status"], "EAN_AMBIGUO_RISOLTO_AI")
        self.assertEqual(risultato["method"], "EAN_AI")

    def test_gli_esempi_si_troncano_a_dieci_ma_il_conteggio_e_intero(self) -> None:
        punteggi = [0.7] * 12
        decisioni = [{
            "gestionale_source_row": 12 + indice,
            "supplier": "betulla",
            "action": "ACCEPT",
            "source_row": 100 + indice,
            "confidence": "ALTA",
            "rationale": "",
        } for indice in range(12)]
        matching = [{
            "gestionale": {"source_row": 12 + indice, "description": f"P{indice}", "ean": ""},
            "suppliers": {"betulla": {"status": "EAN_ASSENTE", "usable_candidates": []}},
        } for indice in range(12)]
        shortlists = [{
            "gestionale_source_row": 12 + indice,
            "supplier": "betulla",
            "candidates": [{"source_row": 100 + indice, "description": f"CANDIDATO {indice}",
                            "unit_price_net": 1.0, "score": punteggi[indice]}],
        } for indice in range(12)]
        with tempfile.TemporaryDirectory() as temporanea:
            esito, _percorso = esegui_grezzo(
                Path(temporanea),
                matching=matching,
                shortlists=shortlists,
                decisions=decisioni,
                normalized={"betulla": []},
            )
        riepilogo = json.loads(esito.stdout)
        self.assertEqual(riepilogo["decisioni_scartate_per_disallineamento"], 12)
        self.assertEqual(len(riepilogo["disallineamenti"]), 10)


class RigaCheIlModelloNonHaVistoTests(unittest.TestCase):
    """The model can't name a row it was never shown. This must fail with a
    distinct, reported outcome, not a crash indistinguishable from any
    other."""

    def test_una_riga_fuori_shortlist_ha_un_esito_suo(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            esito, percorso = esegui_grezzo(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=shortlist_di_prova([0.72]),
                decisions=[{**ACCETTA_LA_100[0], "source_row": 9999}],
            )
            risolti = json.loads(percorso.read_text(encoding="utf-8"))
        self.assertEqual(esito.returncode, USCITA_RIGA_INVENTATA)
        riepilogo = json.loads(esito.stdout)
        self.assertEqual(riepilogo["decisioni_scartate_per_riga_inventata"], 1)
        self.assertEqual(riepilogo["righe_inventate"][0]["righe_mostrate"], [100])
        self.assertEqual(risolti[0]["suppliers"]["betulla"]["status"], "DA_VERIFICARE")

    def test_su_un_ean_ambiguo_la_riga_deve_avere_l_ean_del_prodotto(self) -> None:
        """For an `EAN_AMBIGUO` case, the right row isn't a matter of opinion:
        the supplier has several rows with the reorder-list EAN, and the
        choice is among those. But the shortlist never looks at the EAN — it
        ranks by description token — so it can show the model a row that
        doesn't carry that EAN. With `ALTA` it would enter the order
        unconfirmed."""
        candidato_senza_ean = {"source_row": 100, "ean": "9999", "description": "SOMIGLIA MOLTO",
                               "unit_price_net": 1.0, "score": 0.95}
        with tempfile.TemporaryDirectory() as temporanea:
            esito, percorso = esegui_grezzo(
                Path(temporanea),
                matching=matching_di_prova("EAN_AMBIGUO", usable=[
                    {"source_row": 200, "ean": "8001", "description": "IL VERO A", "unit_price_net": 1.0, "usable": True},
                    {"source_row": 201, "ean": "8001", "description": "IL VERO B", "unit_price_net": 1.0, "usable": True},
                ]),
                shortlists=[{"gestionale_source_row": 12, "supplier": "betulla", "candidates": [candidato_senza_ean]}],
                decisions=ACCETTA_LA_100,
            )
            risolti = json.loads(percorso.read_text(encoding="utf-8"))
        self.assertEqual(esito.returncode, USCITA_RIGA_INVENTATA)
        riepilogo = json.loads(esito.stdout)
        self.assertEqual(riepilogo["decisioni_scartate_per_ean_non_rispettato"], 1)
        self.assertEqual(riepilogo["ean_non_rispettato"][0]["righe_con_l_ean_del_gestionale"], [200, 201])
        self.assertEqual(risolti[0]["suppliers"]["betulla"]["ai_decisione_scartata"], "EAN_NON_RISPETTATO")

    def test_su_un_ean_assente_nessun_vincolo_di_ean(self) -> None:
        """The control: where there's no EAN, the constraint must not exist —
        otherwise it would discard every semantic match, which is 100% of
        the AI's work on real data."""
        with tempfile.TemporaryDirectory() as temporanea:
            _risolti, riepilogo = esegui(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=shortlist_di_prova([0.72]),
                decisions=ACCETTA_LA_100,
            )
        self.assertEqual(riepilogo["decisioni_scartate_per_ean_non_rispettato"], 0)

    def test_una_riga_dei_candidati_ean_non_e_una_riga_mostrata(self) -> None:
        """Rows from `usable_candidates` must not be accepted here: they never
        reach the model, since the AI step builds its cases from the
        shortlists alone. A row that's accepted but never shown is an open
        door."""
        with tempfile.TemporaryDirectory() as temporanea:
            esito, _percorso = esegui_grezzo(
                Path(temporanea),
                matching=matching_di_prova(
                    "EAN_AMBIGUO",
                    usable=[{"source_row": 777, "ean": "8001", "description": "MAI MOSTRATO",
                             "unit_price_net": 1.0, "order_multiplier": 6, "usable": True}],
                ),
                shortlists=shortlist_di_prova([0.72]),
                decisions=[{**ACCETTA_LA_100[0], "source_row": 777}],
            )
        self.assertEqual(esito.returncode, USCITA_RIGA_INVENTATA)


class LAssenzaDiDecisioniNonEUnSuccessoTests(unittest.TestCase):
    """The same lesson as the benchmark that once reported "0 wrong `ALTA`"
    for a pass where no answer had ever come back: a failure must not be
    able to look like a perfect score."""

    def test_decisions_attese_e_obbligatorio(self) -> None:
        """Making it optional closed nothing: forgetting it was enough."""
        with tempfile.TemporaryDirectory() as temporanea:
            esito, percorso = esegui_grezzo(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=shortlist_di_prova([0.72]),
                decisions=None,
                attese=None,
            )
            self.assertFalse(percorso.exists())
        self.assertNotEqual(esito.returncode, USCITA_OK)
        self.assertIn("decisions-attese", esito.stderr)

    def test_meno_decisioni_di_quante_dichiarate_fermano_la_catena(self) -> None:
        """The real scenario: the AI phase decided 900, the file carries 12
        because the write was interrupted halfway through."""
        with tempfile.TemporaryDirectory() as temporanea:
            esito, percorso = esegui_grezzo(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=shortlist_di_prova([0.72]),
                decisions=[],
                attese=1,
            )
            self.assertFalse(percorso.exists())
        self.assertEqual(esito.returncode, USCITA_DECISIONI_NON_RICONCILIATE)

    def test_piu_decisioni_di_quante_dichiarate_fermano_la_catena(self) -> None:
        """The opposite, and dangerous, direction: the caller declares the AI
        phase degraded, but yesterday's file is still on disk. An `ALTA`
        `ACCEPT` from the previous run would enter the order unconfirmed."""
        with tempfile.TemporaryDirectory() as temporanea:
            esito, percorso = esegui_grezzo(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=shortlist_di_prova([0.72]),
                decisions=ACCETTA_LA_100,
                attese=0,
            )
            self.assertFalse(percorso.exists())
        self.assertEqual(esito.returncode, USCITA_DECISIONI_NON_RICONCILIATE)

    def test_un_file_indicato_e_mancante_e_un_guasto(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            cartella = Path(temporanea)
            esito = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "merge_match_decisions.py"),
                    "--matching", str(scrivi(cartella, "matching.json", matching_di_prova())),
                    "--normalized", str(scrivi(cartella, "normalized.json", {"betulla": []})),
                    "--shortlists", str(scrivi(cartella, "shortlists.json", [])),
                    "--decisions", str(cartella / "che-non-c-e.json"),
                    "--decisions-attese", "0",
                    "--output", str(cartella / "resolved.json"),
                ],
                capture_output=True, text=True, encoding="utf-8",
            )
            self.assertFalse((cartella / "resolved.json").exists())
        # Even with degradation declared: "the file is missing because we're
        # degraded" and "missing because the write failed" are two different
        # things.
        self.assertEqual(esito.returncode, USCITA_DECISIONI_NON_RICONCILIATE)

    def test_il_degrado_dichiarato_resta_legittimo(self) -> None:
        """"If OpenRouter doesn't answer, the program keeps going" is a
        deliberate choice, not a bug: zero declared decisions is a valid run,
        and the cases stay pending review."""
        with tempfile.TemporaryDirectory() as temporanea:
            risolti, riepilogo = esegui(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=shortlist_di_prova([0.72]),
                decisions=None,
                attese=0,
            )
        self.assertEqual(risolti[0]["suppliers"]["betulla"]["status"], "DA_VERIFICARE")
        self.assertEqual(riepilogo["coda_semantica"], 1)
        self.assertEqual(riepilogo["decisioni_lette"], 0)

    def test_la_riconciliazione_vale_anche_a_coda_vuota(self) -> None:
        """Declaring 900 decisions on a run entirely resolved by EAN matching
        is a fault like any other: the check isn't skipped just because
        there was no work to do."""
        with tempfile.TemporaryDirectory() as temporanea:
            esito, _percorso = esegui_grezzo(
                Path(temporanea),
                matching=[{
                    "gestionale": {"source_row": 12, "description": "PRODOTTO", "ean": "8001"},
                    "suppliers": {"betulla": {
                        "status": "EAN_ESATTO",
                        "usable_candidates": [{"source_row": 100, "unit_price_net": 1.0, "description": "PRODOTTO"}],
                    }},
                }],
                shortlists=[],
                decisions=[],
                attese=900,
            )
        self.assertEqual(esito.returncode, USCITA_DECISIONI_NON_RICONCILIATE)


class ContareNonDimostraAppartenenzaTests(unittest.TestCase):
    """A decisions file from another run can reconcile perfectly — 942
    declared, 942 found — while not a single one is actually applied."""

    def test_le_decisioni_di_un_altra_run_non_passano_per_applicate(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            esito, _percorso = esegui_grezzo(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=shortlist_di_prova([0.72]),
                decisions=[{**ACCETTA_LA_100[0], "gestionale_source_row": 777}],
            )
        self.assertEqual(esito.returncode, USCITA_DECISIONI_NON_RICONCILIATE)
        riepilogo = json.loads(esito.stdout)
        self.assertEqual(riepilogo["decisioni_lette"], 1)
        self.assertEqual(riepilogo["decisioni_con_riscontro"], 0)
        self.assertEqual(riepilogo["decisioni_senza_riscontro"], 1)

    def test_una_coppia_che_l_ean_ha_risolto_nel_frattempo_non_ferma_niente(self) -> None:
        """This happens whenever the deterministic steps rerun after the AI
        phase: a pair moves to `EAN_ESATTO` and its decision no longer
        applies. The result is better than what the AI proposed — the
        EAN wins — so stopping the chain would mean redoing, i.e. paying
        for, the AI phase on a healthy run."""
        with tempfile.TemporaryDirectory() as temporanea:
            risolti, riepilogo = esegui(
                Path(temporanea),
                matching=matching_di_prova("EAN_ESATTO", usable=[
                    {"source_row": 100, "ean": "8001", "description": "IL VERO", "unit_price_net": 1.0},
                ]),
                shortlists=shortlist_di_prova([0.72]),
                decisions=ACCETTA_LA_100,
            )
        self.assertEqual(risolti[0]["suppliers"]["betulla"]["status"], "EAN_ESATTO")
        self.assertEqual(riepilogo["decisioni_superate_dall_ean"], 1)
        self.assertEqual(riepilogo["decisioni_senza_riscontro"], 0)

    def test_una_coppia_risolta_dall_ean_senza_decisione_non_si_conta(self) -> None:
        """The counter must say how many decisions the EAN match superseded,
        not how many pairs the EAN resolved: those are the majority of
        products, and counting them would make the number meaningless
        without changing the outcome."""
        with tempfile.TemporaryDirectory() as temporanea:
            _risolti, riepilogo = esegui(
                Path(temporanea),
                matching=matching_di_prova("EAN_ESATTO", usable=[
                    {"source_row": 100, "ean": "8001", "description": "IL VERO", "unit_price_net": 1.0},
                ]),
                shortlists=[],
                decisions=None,
                attese=0,
            )
        self.assertEqual(riepilogo["decisioni_superate_dall_ean"], 0)

    def test_un_fornitore_scritto_diverso_non_si_lega(self) -> None:
        """`BETULLA` instead of `betulla`: the count checks out, the work doesn't."""
        with tempfile.TemporaryDirectory() as temporanea:
            esito, _percorso = esegui_grezzo(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=shortlist_di_prova([0.72]),
                decisions=[{**ACCETTA_LA_100[0], "supplier": "BETULLA"}],
            )
        self.assertEqual(esito.returncode, USCITA_DECISIONI_NON_RICONCILIATE)

    def test_una_source_row_scritta_come_stringa_si_lega_lo_stesso(self) -> None:
        """JSON written by another program can carry `"12"`: that isn't a
        fault, and treating it as one would discard a good decision. It
        applies to both rows in the pair, not just this one: on the other
        row the consequence is worse — exit code 5, "the model answered
        outside the allowed rows", just for a number spelled differently."""
        with tempfile.TemporaryDirectory() as temporanea:
            _risolti, riepilogo = esegui(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=shortlist_di_prova([0.72]),
                decisions=[{**ACCETTA_LA_100[0], "gestionale_source_row": "12", "source_row": "100"}],
            )
        self.assertEqual(riepilogo["decisioni_con_riscontro"], 1)
        self.assertEqual(riepilogo["decisioni_scartate_per_riga_inventata"], 0)

    def test_un_accept_senza_riga_e_una_decisione_malformata(self) -> None:
        """The client's schema allows `source_row: null`. Reporting it as a
        hallucination would send the reader looking for the bug in the
        model."""
        for riga in (None, "non un numero"):
            with self.subTest(riga=riga), tempfile.TemporaryDirectory() as temporanea:
                esito, _percorso = esegui_grezzo(
                    Path(temporanea),
                    matching=matching_di_prova(),
                    shortlists=shortlist_di_prova([0.72]),
                    decisions=[{**ACCETTA_LA_100[0], "source_row": riga}],
                )
                self.assertEqual(esito.returncode, USCITA_INGRESSO_NON_UTILIZZABILE)

    def test_due_decisioni_sulla_stessa_coppia_restano_un_errore(self) -> None:
        """The last row in the file wins: an `ACCEPT` would silently become a
        `REJECT`, and the product would vanish from the comparison depending
        on row order."""
        with tempfile.TemporaryDirectory() as temporanea:
            esito, percorso = esegui_grezzo(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=shortlist_di_prova([0.72]),
                decisions=[
                    ACCETTA_LA_100[0],
                    {**ACCETTA_LA_100[0], "action": "REJECT", "source_row": None},
                ],
                # Declared as one: reconciliation alone wouldn't stop this
                # case, so it needs the duplicate check, not just the row count.
                attese=1,
            )
            self.assertFalse(percorso.exists())
        self.assertEqual(esito.returncode, USCITA_DECISIONI_NON_RICONCILIATE)
        self.assertIn("duplicata", esito.stdout)

    def test_un_file_di_decisioni_che_non_e_una_lista(self) -> None:
        """`{"decisions": [...]}` (an object instead of a list) must be
        reported cleanly, not raise a `TypeError` with a traceback."""
        with tempfile.TemporaryDirectory() as temporanea:
            cartella = Path(temporanea)
            esito = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "merge_match_decisions.py"),
                    "--matching", str(scrivi(cartella, "matching.json", matching_di_prova())),
                    "--normalized", str(scrivi(cartella, "normalized.json", {"betulla": []})),
                    "--shortlists", str(scrivi(cartella, "shortlists.json", [])),
                    "--decisions", str(scrivi(cartella, "decisions.json", {"decisions": []})),
                    "--decisions-attese", "0",
                    "--output", str(cartella / "resolved.json"),
                ],
                capture_output=True, text=True, encoding="utf-8",
            )
        self.assertEqual(esito.returncode, USCITA_INGRESSO_NON_UTILIZZABILE)
        self.assertEqual(esito.stderr, "", "un traceback non è un messaggio")
        # The message names the problem. Without it, the exit code is right
        # only by chance — the per-decision check downstream would still
        # catch it, but reporting something else.
        self.assertIn("lista", esito.stdout)

    def test_un_azione_non_valida_e_un_ingresso_non_utilizzabile(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            esito, _percorso = esegui_grezzo(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=shortlist_di_prova([0.72]),
                decisions=[{**ACCETTA_LA_100[0], "action": "FORSE"}],
                attese=1,
            )
        self.assertEqual(esito.returncode, USCITA_INGRESSO_NON_UTILIZZABILE)


class LaCodaSemanticaTests(unittest.TestCase):
    def test_un_ean_ambiguo_conta_nella_coda(self) -> None:
        """An `EAN_AMBIGUO` case is AI work like any other: excluding it would
        let a run where the AI resolved nothing exit as a success."""
        with tempfile.TemporaryDirectory() as temporanea:
            _risolti, riepilogo = esegui(
                Path(temporanea),
                matching=matching_di_prova("EAN_AMBIGUO", usable=[
                    {"source_row": 100, "description": "A", "unit_price_net": 1.0, "usable": True},
                    {"source_row": 101, "description": "B", "unit_price_net": 1.0, "usable": True},
                ]),
                shortlists=shortlist_di_prova([0.72]),
                decisions=None,
                attese=0,
            )
        self.assertEqual(riepilogo["coda_semantica"], 1)

    def test_la_coda_valutabile_toglie_i_casi_senza_candidati(self) -> None:
        """The client refuses to send the model a case with no candidates: six
        out of 948 in the real run. Without this number, "found 942 out of a
        948 queue" reads as "six are missing"."""
        with tempfile.TemporaryDirectory() as temporanea:
            _risolti, riepilogo = esegui(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=[{"gestionale_source_row": 12, "supplier": "betulla", "candidates": []}],
                decisions=None,
                attese=0,
            )
        self.assertEqual(riepilogo["coda_semantica"], 1)
        self.assertEqual(riepilogo["coda_valutabile"], 0)


def _tre_guasti_insieme() -> dict[str, Any]:
    """A single run with all three failures at once.

    12/betulla: fabricated row. 13/betulla: shifted price list. 777/betulla: a
    decision that finds no matching pair."""
    matching = [
        {"gestionale": {"source_row": 12, "description": "P12", "ean": ""},
         "suppliers": {"betulla": {"status": "EAN_ASSENTE", "usable_candidates": []}}},
        {"gestionale": {"source_row": 13, "description": "P13", "ean": ""},
         "suppliers": {"betulla": {"status": "EAN_ASSENTE", "usable_candidates": []}}},
    ]
    shortlists = [
        {"gestionale_source_row": 12, "supplier": "betulla",
         "candidates": [{"source_row": 100, "description": "CANDIDATO 0", "unit_price_net": 1.0, "score": 0.7}]},
        {"gestionale_source_row": 13, "supplier": "betulla",
         "candidates": [{"source_row": 101, "description": "CANDIDATO 1", "unit_price_net": 1.0, "score": 0.7}]},
    ]
    decisions = [
        {**ACCETTA_LA_100[0], "gestionale_source_row": 12, "source_row": 9999},
        {**ACCETTA_LA_100[0], "gestionale_source_row": 13, "source_row": 101},
        {**ACCETTA_LA_100[0], "gestionale_source_row": 777, "source_row": 100},
    ]
    normalized = {"betulla": [
        {"source_row": 100, "ean": "", "description": "CANDIDATO 0", "unit_price_net": 1.0, "order_multiplier": 6},
        {"source_row": 101, "ean": "", "description": "TUTT ALTRO", "unit_price_net": 1.0, "order_multiplier": 6},
    ]}
    return {"matching": matching, "shortlists": shortlists, "decisions": decisions, "normalized": normalized}


class LaPrecedenzaFraGliEsitiTests(unittest.TestCase):
    """Three failures at once must exit with one code, and it has to be
    the most severe. If the priority is reversed, a fabricated row — the
    model answering outside the allowed rows — gets reported as merely
    "decisions not reconciled"."""

    def test_la_riga_inventata_vince_su_tutto_il_resto(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            esito, _percorso = esegui_grezzo(Path(temporanea), attese=3, **_tre_guasti_insieme())
        riepilogo = json.loads(esito.stdout)
        self.assertEqual(riepilogo["decisioni_scartate_per_riga_inventata"], 1)
        self.assertEqual(riepilogo["decisioni_scartate_per_disallineamento"], 1)
        self.assertEqual(riepilogo["decisioni_senza_riscontro"], 1)
        self.assertEqual(esito.returncode, USCITA_RIGA_INVENTATA)

    def test_il_listino_disallineato_vince_sulle_decisioni_senza_riscontro(self) -> None:
        dati = _tre_guasti_insieme()
        dati["decisions"] = [d for d in dati["decisions"] if d["source_row"] != 9999]
        with tempfile.TemporaryDirectory() as temporanea:
            esito, _percorso = esegui_grezzo(Path(temporanea), attese=2, **dati)
        self.assertEqual(esito.returncode, USCITA_LISTINO_DISALLINEATO)


class UnaDecisioneScartataHaComunqueTrovatoLaSuaCoppiaTests(unittest.TestCase):
    """"Not applied" means "the two files don't describe the same run",
    which is the diagnosis that sends someone to look for the wrong file. A
    decision discarded because the model named the wrong row did find its
    matching pair: counting it under "unmatched pairs" would report a pair
    that actually exists."""

    def test_una_riga_inventata_non_e_una_decisione_senza_riscontro(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            esito, _percorso = esegui_grezzo(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=shortlist_di_prova([0.72]),
                decisions=[{**ACCETTA_LA_100[0], "source_row": 9999}],
            )
        riepilogo = json.loads(esito.stdout)
        self.assertEqual(riepilogo["decisioni_con_riscontro"], 1)
        self.assertEqual(riepilogo["decisioni_senza_riscontro"], 0)
        self.assertNotIn("coppie_senza_riscontro", riepilogo)


class IlFileSiScriveSempreTests(unittest.TestCase):
    """This also applies to exit code 3 (unmatched decisions), the only one of
    the three "downstream" failures where nothing else checks the output
    file. If it isn't written, the previous run's file stays on disk to be
    read as fresh."""

    def test_il_file_si_scrive_anche_con_decisioni_senza_riscontro(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            esito, percorso = esegui_grezzo(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=shortlist_di_prova([0.72]),
                decisions=[{**ACCETTA_LA_100[0], "gestionale_source_row": 777}],
            )
            self.assertEqual(esito.returncode, USCITA_DECISIONI_NON_RICONCILIATE)
            self.assertTrue(percorso.exists(), "un artefatto fresco e degradato è onesto; uno vecchio no")
            risolti = json.loads(percorso.read_text(encoding="utf-8"))
        self.assertEqual(risolti[0]["suppliers"]["betulla"]["status"], "DA_VERIFICARE")


class UnaDecisioneSenzaConfidenzaTests(unittest.TestCase):
    """The field can be missing: this script is the last gate and can't trust
    whoever wrote the file. A missing value must fall on the
    confirmation-required side, never on the side that orders unattended."""

    def test_una_decisione_senza_confidenza_chiede_conferma(self) -> None:
        senza = {chiave: valore for chiave, valore in ACCETTA_LA_100[0].items() if chiave != "confidence"}
        with tempfile.TemporaryDirectory() as temporanea:
            risolti, riepilogo = esegui(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=shortlist_di_prova([0.72]),
                decisions=[senza],
            )
        self.assertIs(risolti[0]["suppliers"]["betulla"]["requires_user_confirmation"], True)
        self.assertEqual(riepilogo["accettati_senza_conferma"], 0)


class LaFormaDeiTreEsitiNonAiTests(unittest.TestCase):
    """`build_review_data.py` reads these fields to decide what to show and
    what to treat as already confirmed, so their shape is a contract."""

    def _solo_ean(self) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as temporanea:
            risolti, _riepilogo = esegui(
                Path(temporanea),
                matching=[{
                    "gestionale": {"source_row": 12, "description": "PRODOTTO", "ean": "8001"},
                    "suppliers": {"betulla": {
                        "status": "EAN_ESATTO",
                        "usable_candidates": [{"source_row": 100, "unit_price_net": 1.0, "description": "PRODOTTO"}],
                    }},
                }],
                shortlists=[],
                decisions=[],
            )
        return risolti[0]["suppliers"]["betulla"]

    def test_il_match_per_ean_esatto_e_certo_e_non_chiede_conferma(self) -> None:
        """These are the majority of products: making them all confirmed by
        hand would mean hundreds of clicks, and exiting 0 would let it go
        unnoticed."""
        risultato = self._solo_ean()
        self.assertEqual(risultato["status"], "EAN_ESATTO")
        self.assertEqual(risultato["method"], "EAN")
        self.assertEqual(risultato["confidence"], "CERTA")
        self.assertIs(risultato["requires_user_confirmation"], False)

    def test_il_rifiuto_dell_ai_si_riconosce_dallo_stato_e_dal_metodo(self) -> None:
        """`build_review_data.py` triggers the "rejected, but similar" warning
        by filtering on `method == "AI_RIFIUTATO"`, and `NON_TROVATO` is what
        tells the page that supplier doesn't carry the product."""
        with tempfile.TemporaryDirectory() as temporanea:
            risolti, _riepilogo = esegui(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=shortlist_di_prova([0.72]),
                decisions=[{**ACCETTA_LA_100[0], "action": "REJECT", "source_row": None}],
            )
        risultato = risolti[0]["suppliers"]["betulla"]
        self.assertEqual(risultato["status"], "NON_TROVATO")
        self.assertEqual(risultato["method"], "AI_RIFIUTATO")
        self.assertIs(risultato["requires_user_confirmation"], False)

    def test_il_caso_mai_deciso_chiede_conferma(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            risolti, _riepilogo = esegui(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=shortlist_di_prova([0.72]),
                decisions=None,
                attese=0,
            )
        risultato = risolti[0]["suppliers"]["betulla"]
        self.assertEqual(risultato["status"], "DA_VERIFICARE")
        self.assertIs(risultato["requires_user_confirmation"], True)


class UnResolvedEUnaDecisioneNormaleTests(unittest.TestCase):
    """`app/ai_client.py` downgrades to `UNRESOLVED` every `ACCEPT` the
    adversarial check doesn't confirm: dozens of these arrive in a real run.
    There was no test line for this, and rejecting them would exit 2 — i.e.
    stop the whole chain — right when the safeguard did its job."""

    def test_un_unresolved_passa_e_finisce_da_verificare(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            risolti, riepilogo = esegui(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=shortlist_di_prova([0.72]),
                decisions=[{
                    "gestionale_source_row": 12,
                    "supplier": "betulla",
                    "action": "UNRESOLVED",
                    "source_row": None,
                    "confidence": "MEDIA",
                    "rationale": "la verifica avversariale non conferma",
                }],
            )
        risultato = risolti[0]["suppliers"]["betulla"]
        self.assertEqual(risultato["status"], "DA_VERIFICARE")
        self.assertIs(risultato["requires_user_confirmation"], True)
        self.assertIn("verifica avversariale", risultato["rationale"])
        self.assertEqual(riepilogo["decisioni_con_riscontro"], 1)


class IlPrezzoSiConfrontaAlCentesimoTests(unittest.TestCase):
    def test_un_centesimo_di_differenza_e_un_altro_prodotto(self) -> None:
        """Earlier tests used 2.18 vs 99.99: rounding to the nearest euro let
        them all pass, and in real price lists two batches of the same item
        differ by cents."""
        self.assertNotEqual(
            identita({"ean": "8001", "description": "X", "unit_price_net": 2.18}),
            identita({"ean": "8001", "description": "X", "unit_price_net": 2.19}),
        )

    def test_un_centesimo_di_differenza_scarta_la_decisione(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            esito, _percorso = esegui_grezzo(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=shortlist_di_prova([0.72]),
                decisions=ACCETTA_LA_100,
                normalized={"betulla": [{
                    "source_row": 100, "ean": "", "description": "CANDIDATO 0",
                    "unit_price_net": 1.01, "order_multiplier": 6,
                }]},
            )
        self.assertEqual(esito.returncode, USCITA_LISTINO_DISALLINEATO)


class LaSogliaEUnNumeroMisuratoTests(unittest.TestCase):
    """Like the exit codes, this value comes from the measurement that chose
    it, and `build_review_data.py` imports it from here to trigger its
    warning. Changing it silently changes how many wrong rejections surface."""

    def test_e_quella_misurata_sul_banco(self) -> None:
        self.assertAlmostEqual(SOGLIA_RIFIUTO_SOSPETTO, 0.65)


class QuelloCheIlRevisoreSiVedeArrivareTests(unittest.TestCase):
    """A degraded pair goes back to a person, who has to be able to decide:
    without the candidates and the rationale, they're looking at an empty
    row with no way to decide short of reopening the price lists by hand."""

    def test_la_coppia_scartata_porta_i_candidati_mostrati_al_modello(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            esito, percorso = esegui_grezzo(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=shortlist_di_prova([0.72]),
                decisions=[{**ACCETTA_LA_100[0], "source_row": 9999}],
            )
            self.assertEqual(esito.returncode, USCITA_RIGA_INVENTATA)
            risolti = json.loads(percorso.read_text(encoding="utf-8"))
        alternative = risolti[0]["suppliers"]["betulla"]["alternatives"]
        self.assertEqual([candidato["source_row"] for candidato in alternative], [100])

    def test_l_accettato_porta_le_alternative_e_la_motivazione(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            risolti, _riepilogo = esegui(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=shortlist_di_prova([0.72, 0.61]),
                decisions=ACCETTA_LA_100,
            )
        risultato = risolti[0]["suppliers"]["betulla"]
        self.assertEqual(risultato["rationale"], "stesso prodotto")
        self.assertEqual([candidato["source_row"] for candidato in risultato["alternatives"]], [100, 101])


class DichiarareDecisioniSenzaPassareIlFileTests(unittest.TestCase):
    """The mirror case of "--decisions-attese is mandatory": the flag is there
    and says 900, but `--decisions` isn't. Without this check the chain
    reads zero decisions and treats it as a deliberate degradation."""

    def test_dichiarare_decisioni_senza_il_file_ferma_la_catena(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            esito, _percorso = esegui_grezzo(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=shortlist_di_prova([0.72]),
                decisions=None,
                attese=900,
            )
        self.assertEqual(esito.returncode, USCITA_DECISIONI_NON_RICONCILIATE)


class IlRiepilogoSiStampaAncheQuandoLaCatenaSiFermaTests(unittest.TestCase):
    """"No one reads the logs" matters most here: if the chain stops
    without numbers, whoever restarts it doesn't know if two were missing
    or nine hundred."""

    def test_la_mancata_riconciliazione_dice_i_numeri(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            esito, _percorso = esegui_grezzo(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=shortlist_di_prova([0.72]),
                decisions=[],
                attese=900,
            )
        self.assertEqual(esito.returncode, USCITA_DECISIONI_NON_RICONCILIATE)
        riepilogo = json.loads(esito.stdout)
        self.assertEqual(riepilogo["dichiarate"], 900)
        self.assertEqual(riepilogo["trovate"], 0)
        self.assertEqual(riepilogo["coda_semantica"], 1)
        self.assertEqual(riepilogo["coda_valutabile"], 1)

    def test_il_riepilogo_dice_quali_coppie_non_hanno_riscontro(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            esito, _percorso = esegui_grezzo(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=shortlist_di_prova([0.72]),
                decisions=[{**ACCETTA_LA_100[0], "gestionale_source_row": 777}],
            )
        riepilogo = json.loads(esito.stdout)
        self.assertEqual(riepilogo["coppie_senza_riscontro"], [[777, "betulla"]])


class UnaRigaSenzaNomeNelRiepilogoTests(unittest.TestCase):
    """A person reads the summary: `null` next to `null` says nothing, and
    rows with neither a description nor an EAN do exist in real price
    lists."""

    def test_una_riga_senza_descrizione_ne_ean_si_dice_a_parole(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            esito, _percorso = esegui_grezzo(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=[{
                    "gestionale_source_row": 12, "supplier": "betulla",
                    "candidates": [{"source_row": 100, "ean": "", "description": "",
                                    "unit_price_net": 1.0, "score": 0.72}],
                }],
                decisions=ACCETTA_LA_100,
                normalized={"betulla": [{
                    "source_row": 100, "ean": "", "description": "", "unit_price_net": 9.99,
                    "order_multiplier": 6,
                }]},
            )
        self.assertEqual(esito.returncode, USCITA_LISTINO_DISALLINEATO)
        riepilogo = json.loads(esito.stdout)
        self.assertEqual(riepilogo["disallineamenti"][0]["mostrato_al_modello"],
                         "(riga senza descrizione né EAN)")


class LeRigheMostrateNelRiepilogoTests(unittest.TestCase):
    """A candidate without `source_row` must not crash the summary that
    explains why the row was rejected: that would be a traceback in place of
    the message, on the one exit code that actually carries a message."""

    def test_un_candidato_senza_riga_non_rompe_il_riepilogo(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            esito, _percorso = esegui_grezzo(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=[{
                    "gestionale_source_row": 12, "supplier": "betulla",
                    "candidates": [
                        {"source_row": 100, "description": "CANDIDATO 0", "unit_price_net": 1.0, "score": 0.7},
                        {"description": "SENZA RIGA", "unit_price_net": 1.0, "score": 0.6},
                    ],
                }],
                decisions=[{**ACCETTA_LA_100[0], "source_row": 9999}],
                normalized={"betulla": [{
                    "source_row": 100, "ean": "", "description": "CANDIDATO 0",
                    "unit_price_net": 1.0, "order_multiplier": 6,
                }]},
            )
        self.assertEqual(esito.returncode, USCITA_RIGA_INVENTATA)
        self.assertEqual(esito.stderr, "", "un traceback non è un messaggio")
        self.assertEqual(json.loads(esito.stdout)["righe_inventate"][0]["righe_mostrate"], [100])


class UnaDecisioneDichiaraIlCasoSuCuiEStataPresaTests(unittest.TestCase):
    """A gap that the earlier row/price-list guards left open.

    Every other guard compares artifacts of the current run against each
    other — the accepted row against the shortlist, the shortlist against the
    price list — so against a decisions file from another run they're blind
    by construction: the reorder list is the same file week to week, the
    `(row, supplier)` pairs mostly overlap, and the counts reconcile. The
    fingerprint is the only field that carries what the model actually had
    in front of it."""

    def test_una_decisione_senza_impronta_non_viene_applicata(self) -> None:
        """Accepting it would let the guard be bypassed just by forgetting a
        field, which is exactly the gap `--decisions-attese` closed."""
        with tempfile.TemporaryDirectory() as temporanea:
            esito, percorso = esegui_grezzo(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=shortlist_di_prova([0.72]),
                decisions=ACCETTA_LA_100,
                con_impronta=False,
            )
            # Inside the `with`: outside it, the temp directory is gone and
            # any assertion on the file would pass for the wrong reason.
            risolti = json.loads(percorso.read_text(encoding="utf-8"))
        self.assertEqual(esito.returncode, USCITA_DECISIONI_NON_RICONCILIATE)
        match = risolti[0]["suppliers"]["betulla"]
        self.assertEqual(match["status"], "DA_VERIFICARE")
        # A distinct cause from a stale file: here the fault is a producer
        # that forgot a field, and pointing at "a file from another run"
        # would send whoever reads it looking in the wrong place.
        self.assertEqual(match["ai_decisione_scartata"], "DECISIONE_SENZA_IMPRONTA")
        self.assertIn("non dichiara su quale caso", match["rationale"])

    def test_un_impronta_che_non_corrisponde_butta_la_decisione(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            esito, percorso = esegui_grezzo(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=shortlist_di_prova([0.72]),
                decisions=[{**ACCETTA_LA_100[0], "ai_impronta_caso": "0000000000000000"}],
            )
            risolti = json.loads(percorso.read_text(encoding="utf-8"))
        self.assertEqual(esito.returncode, USCITA_DECISIONI_NON_RICONCILIATE)
        riepilogo = json.loads(esito.stdout)
        self.assertEqual(riepilogo["decisioni_scartate_perche_di_un_altra_run"], 1)
        self.assertEqual(riepilogo["di_un_altra_run"][0]["impronta_dichiarata"], "0000000000000000")
        self.assertIsNone(risolti[0]["suppliers"]["betulla"]["selected"])

    def test_vale_anche_per_un_rifiuto(self) -> None:
        """A stale `REJECT` makes the product vanish from that supplier with
        nothing saying so: the worst kind of silent failure. It must go back
        to the reviewer, not become `NON_TROVATO`."""
        with tempfile.TemporaryDirectory() as temporanea:
            esito, percorso = esegui_grezzo(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=shortlist_di_prova([0.72]),
                decisions=[{
                    **ACCETTA_LA_100[0], "action": "REJECT", "source_row": None,
                    "ai_impronta_caso": "0000000000000000",
                }],
            )
            risolti = json.loads(percorso.read_text(encoding="utf-8"))
        self.assertEqual(esito.returncode, USCITA_DECISIONI_NON_RICONCILIATE)
        match = risolti[0]["suppliers"]["betulla"]
        self.assertEqual(match["status"], "DA_VERIFICARE")
        self.assertEqual(match["ai_decisione_scartata"], "DECISIONE_DI_UNA_ALTRA_RUN")

    def test_una_decisione_stantia_non_si_racconta_come_riga_inventata(self) -> None:
        """The remedy differs: here it's rerunning the AI phase, there it's
        looking at the model. If the case is from another run, the row it
        names says nothing about the model."""
        with tempfile.TemporaryDirectory() as temporanea:
            esito, _percorso = esegui_grezzo(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=shortlist_di_prova([0.72]),
                decisions=[{
                    **ACCETTA_LA_100[0], "source_row": 9999,
                    "ai_impronta_caso": "0000000000000000",
                }],
            )
        self.assertEqual(esito.returncode, USCITA_DECISIONI_NON_RICONCILIATE)
        riepilogo = json.loads(esito.stdout)
        self.assertEqual(riepilogo["decisioni_scartate_per_riga_inventata"], 0)
        self.assertEqual(riepilogo["decisioni_scartate_perche_di_un_altra_run"], 1)

    def test_su_un_ean_ambiguo_la_decisione_stantia_non_diventa_ean_non_rispettato(self) -> None:
        """The counterpart of the test above, on the branch that matters more:
        on an `EAN_AMBIGUO` case an applied decision becomes
        `EAN_AMBIGUO_RISOLTO_AI` and, at `ALTA`, enters the order without
        confirmation. Every other test in this class runs on `EAN_ASSENTE`,
        so a mutation disabling the guard only on the ambiguous branch would
        go unnoticed — a two-branch safeguard needs coverage on both
        branches."""
        with tempfile.TemporaryDirectory() as temporanea:
            esito, percorso = esegui_grezzo(
                Path(temporanea),
                matching=matching_di_prova("EAN_AMBIGUO", usable=[
                    {"source_row": 101, "ean": "8001", "description": "CANDIDATO 1", "unit_price_net": 1.0},
                ]),
                shortlists=shortlist_di_prova([0.72, 0.5]),
                decisions=[{**ACCETTA_LA_100[0], "ai_impronta_caso": "0000000000000000"}],
            )
            risolti = json.loads(percorso.read_text(encoding="utf-8"))
        self.assertEqual(esito.returncode, USCITA_DECISIONI_NON_RICONCILIATE)
        riepilogo = json.loads(esito.stdout)
        self.assertEqual(riepilogo["decisioni_scartate_per_ean_non_rispettato"], 0)
        self.assertEqual(riepilogo["decisioni_scartate_perche_di_un_altra_run"], 1)
        match = risolti[0]["suppliers"]["betulla"]
        self.assertEqual(match["ai_decisione_scartata"], "DECISIONE_DI_UNA_ALTRA_RUN")
        self.assertIsNone(match["selected"])

    def test_una_decisione_stantia_non_butta_via_quelle_buone(self) -> None:
        """Every test in this class uses a single product, while the real case
        has 942. Reading "the file is from another run" tempts a
        implementation to discard the whole file: discarding the good
        decisions too would send hundreds of already-decided pairs back to
        the reviewer, and this test guards against that regression."""
        with tempfile.TemporaryDirectory() as temporanea:
            esito, percorso = esegui_grezzo(
                Path(temporanea),
                matching=DUE_PRODOTTI,
                shortlists=DUE_SHORTLIST,
                decisions=[
                    {"gestionale_source_row": 12, "supplier": "betulla", "action": "ACCEPT",
                     "source_row": 100, "confidence": "ALTA", "rationale": "della settimana scorsa",
                     "ai_impronta_caso": "0000000000000000"},
                    {"gestionale_source_row": 13, "supplier": "betulla", "action": "ACCEPT",
                     "source_row": 200, "confidence": "ALTA", "rationale": "stesso prodotto"},
                ],
            )
            risolti = json.loads(percorso.read_text(encoding="utf-8"))
        self.assertEqual(esito.returncode, USCITA_DECISIONI_NON_RICONCILIATE)
        per_riga = {r["gestionale"]["source_row"]: r["suppliers"]["betulla"] for r in risolti}
        self.assertEqual(per_riga[12]["status"], "DA_VERIFICARE")
        self.assertEqual(per_riga[12]["ai_decisione_scartata"], "DECISIONE_DI_UNA_ALTRA_RUN")
        self.assertEqual(per_riga[13]["status"], "SEMANTICO_PROPOSTO")
        self.assertEqual(per_riga[13]["selected"]["source_row"], 200)
        riepilogo = json.loads(esito.stdout)
        self.assertEqual(riepilogo["decisioni_scartate_perche_di_un_altra_run"], 1)
        self.assertEqual(riepilogo["accettati_senza_conferma"], 1)

    def test_il_conteggio_degli_scarti_e_un_conteggio_non_una_spia(self) -> None:
        """A single-item scenario can't tell a `len()` mistakenly turned into
        a `bool()` apart from a list truncated to one entry: both would
        still pass. The project's invariant is "whatever gets discarded is
        counted", and two is the smallest number that tells a count apart
        from a flag."""
        with tempfile.TemporaryDirectory() as temporanea:
            esito, _percorso = esegui_grezzo(
                Path(temporanea),
                matching=DUE_PRODOTTI,
                shortlists=DUE_SHORTLIST,
                decisions=[
                    {"gestionale_source_row": 12, "supplier": "betulla", "action": "ACCEPT",
                     "source_row": 100, "confidence": "ALTA", "rationale": "x",
                     "ai_impronta_caso": "0000000000000000"},
                    {"gestionale_source_row": 13, "supplier": "betulla", "action": "REJECT",
                     "source_row": None, "confidence": "ALTA", "rationale": "y",
                     "ai_impronta_caso": "1111111111111111"},
                ],
            )
        riepilogo = json.loads(esito.stdout)
        self.assertEqual(riepilogo["decisioni_scartate_perche_di_un_altra_run"], 2)
        self.assertEqual(len(riepilogo["di_un_altra_run"]), 2)

    def test_una_decisione_stantia_su_una_coppia_risolta_dall_ean_non_ferma_niente(self) -> None:
        """The `EAN_ESATTO` branch doesn't check the fingerprint, and that's
        correct — the EAN wins, which beats anything the model says — but no
        test covered it. Both directions matter: the stale decision must not
        apply, and must not stop the chain either, because rerunning the AI
        phase on a healthy run means paying for it again."""
        with tempfile.TemporaryDirectory() as temporanea:
            esito, percorso = esegui_grezzo(
                Path(temporanea),
                matching=matching_di_prova("EAN_ESATTO", usable=[
                    {"source_row": 100, "ean": "8001", "description": "IL VERO", "unit_price_net": 1.0},
                ]),
                shortlists=shortlist_di_prova([0.72]),
                decisions=[{**ACCETTA_LA_100[0], "ai_impronta_caso": "0000000000000000"}],
            )
            risolti = json.loads(percorso.read_text(encoding="utf-8"))
        match = risolti[0]["suppliers"]["betulla"]
        self.assertEqual(match["status"], "EAN_ESATTO")
        self.assertEqual(match["method"], "EAN")
        self.assertEqual(esito.returncode, USCITA_OK)

    def test_il_riepilogo_dice_di_che_articolo_si_parlava(self) -> None:
        """A count without the descriptions sends the reader to two JSON files
        to work out what happened. The decision's item name shows up only
        when it differs from the current one: in the case this guard
        actually catches — the candidates changed — the two would always
        be equal, and two fields
        that always match look like a bug rather than information."""
        with tempfile.TemporaryDirectory() as temporanea:
            esito, _percorso = esegui_grezzo(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=[{**shortlist_di_prova([0.72])[0], "description": "PANTERA DI OGGI"}],
                decisions=[{
                    **ACCETTA_LA_100[0],
                    "ai_impronta_caso": "0000000000000000",
                    "ai_articolo_mostrato": "PANTERA DELLA SETTIMANA SCORSA",
                }],
            )
        esempio = json.loads(esito.stdout)["di_un_altra_run"][0]
        self.assertEqual(esempio["articolo_della_decisione"], "PANTERA DELLA SETTIMANA SCORSA")
        self.assertEqual(esempio["articolo"], "PANTERA DI OGGI")

    def test_quando_cambiano_solo_i_candidati_non_si_ripete_lo_stesso_articolo(self) -> None:
        with tempfile.TemporaryDirectory() as temporanea:
            esito, _percorso = esegui_grezzo(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=[{**shortlist_di_prova([0.72])[0], "description": "PANTERA DI OGGI"}],
                decisions=[{
                    **ACCETTA_LA_100[0],
                    "ai_impronta_caso": "0000000000000000",
                    "ai_articolo_mostrato": "PANTERA DI OGGI",
                }],
            )
        esempio = json.loads(esito.stdout)["di_un_altra_run"][0]
        self.assertEqual(esempio["articolo"], "PANTERA DI OGGI")
        self.assertNotIn("articolo_della_decisione", esempio)


class LImprontaDelCasoTests(unittest.TestCase):
    """What changes the case fingerprint, and what doesn't."""

    def caso(self, **modifiche: Any) -> str:
        base = {
            "gestionale_source_row": 12,
            "supplier": "betulla",
            "description": "PANTERA SHAMPOO 250ML",
            "candidati": [(100, "PANTERA SH.250", 0.7), (101, "PANTERA BALSAMO", 0.4)],
        }
        base.update(modifiche)
        return impronta_caso(
            base["gestionale_source_row"], base["supplier"], base["description"], base["candidati"]
        )

    def test_e_lunga_sedici_cifre(self) -> None:
        self.assertRegex(self.caso(), r"^[0-9a-f]{16}$")

    def test_cambia_se_cambia_qualcosa_che_il_modello_vede(self) -> None:
        base = self.caso()
        diverse = {
            "altro articolo": self.caso(description="PANTERA SHAMPOO 500ML"),
            "altro fornitore": self.caso(supplier="larice"),
            "altra riga del gestionale": self.caso(gestionale_source_row=13),
            "un candidato in meno": self.caso(candidati=[(100, "PANTERA SH.250", 0.7)]),
            "un candidato diverso": self.caso(candidati=[(100, "PANTERA SH.250", 0.7), (999, "DASY", 0.4)]),
            "una descrizione diversa": self.caso(candidati=[(100, "PANTERA SH.500", 0.7), (101, "PANTERA BALSAMO", 0.4)]),
            "un punteggio diverso": self.caso(candidati=[(100, "PANTERA SH.250", 0.9), (101, "PANTERA BALSAMO", 0.4)]),
            "un ordine diverso": self.caso(candidati=[(101, "PANTERA BALSAMO", 0.4), (100, "PANTERA SH.250", 0.7)]),
        }
        for nome, impronta in diverse.items():
            with self.subTest(nome):
                self.assertNotEqual(impronta, base)

    def test_non_cambia_se_un_numero_e_scritto_come_testo(self) -> None:
        """The two callers computing this fingerprint read data from different
        sources: one passes what it sent the model, the other what it finds
        in the file. `441` and `"441"` are the same row number, and treating
        them as different would discard a healthy run."""
        self.assertEqual(
            self.caso(gestionale_source_row="12", candidati=[(100, "PANTERA SH.250", "0.7"), ("101", "PANTERA BALSAMO", 0.4)]),
            self.caso(),
        )

    def test_non_solleva_mai_su_un_valore_storto(self) -> None:
        """The whole point is to say "this file isn't right": crashing while
        saying it would be a traceback in place of the message. The huge
        number isn't a textbook case: a 401-digit integer can make
        `float()` raise `OverflowError`, which would take the merge down
        before `resolved_matches.json` is written — leaving the previous
        run's file on disk to be read as fresh."""
        for storto in (
            (None, None, {"non": "un testo"}, [(None, ["lista"], "molto")]),
            (10 ** 400, "betulla", "X", [(1, "Y", 0.5)]),
            (12, "betulla", "X", [(10 ** 400, "Y", 10 ** 400)]),
            (float("inf"), "betulla", "X", [(1, "Y", float("nan"))]),
        ):
            with self.subTest(str(storto)[:40]):
                self.assertRegex(impronta_caso(*storto), r"^[0-9a-f]{16}$")

    def test_lo_stesso_numero_scritto_in_due_modi_da_la_stessa_impronta(self) -> None:
        """`test_non_cambia_se_un_numero_e_scritto_come_testo` would still pass
        even if `_confrontabile` just did `str(valore)`: `12` and `"12"`
        both become `"12"`. What the function actually does — normalize to a
        number — is only proven by a pair that differs as text but matches
        as a number."""
        self.assertEqual(self.caso(), self.caso(
            gestionale_source_row=12.0,
            candidati=[(100.0, "PANTERA SH.250", 0.70), (101, "PANTERA BALSAMO", 0.4)]))
        self.assertEqual(self.caso(), self.caso(
            gestionale_source_row="12.0",
            candidati=[(100, "PANTERA SH.250", ".7"), (101, "PANTERA BALSAMO", 0.4)]))



# A real observed case, with real numbers: the reorder list calls the
# product 8009405394204, while NOCE and LARICE both carry it as 8009496220932.
LINES_GESTIONALE = {"source_row": 273, "description": "LINDA SETA ULTRA LUNGO ALI 18PZ", "ean": "8009405394204"}
LINES_NOCE = {"source_row": 7463, "ean": "8009496220932", "description": "LINDA SETA ULTRA LUNGO ALI PZ.18",
                  "unit_price_net": 2.31, "pieces_per_carton": 12, "usable": True}
LINES_LARICE = {"source_row": 1975, "ean": "8009496220932", "description": "ASS. LINDA SETAMORBI X 18 LUNGO",
                "unit_price_net": 2.25, "pieces_per_carton": 12, "usable": True}
LINES_LARICE_X9 = {"source_row": 8, "ean": "8009549285017", "description": "ASS. LINDA SETAMORBI X  9 LUNGO ALI",
                   "unit_price_net": 1.15, "pieces_per_carton": 24, "usable": True}


def candidato(riga: dict[str, Any], punteggio: float) -> dict[str, Any]:
    return {**{chiave: riga[chiave] for chiave in ("source_row", "ean", "description", "unit_price_net")},
            "score": punteggio}


class UnAbbinamentoPortaIlSuoCodiceAgliAltriFornitoriTests(unittest.TestCase):
    """The barcode of an AI-accepted row also applies to other suppliers.

    In a real run, a product went to NOCE at 2.31 while LARICE had it at 2.25
    under the same barcode: the AI had accepted NOCE and rejected LARICE's
    abbreviated listing. Here the same situation runs through the real
    script.
    """

    def lancia(self, *, larice: list[dict[str, Any]] | None = None, decisione_larice: str = "REJECT",
               confidenza_noce: str = "ALTA") -> tuple[list[dict[str, Any]], dict[str, Any]]:
        larice = larice if larice is not None else [LINES_LARICE_X9, LINES_LARICE]
        matching = [{
            "gestionale": LINES_GESTIONALE,
            "suppliers": {
                "noce": {"status": "EAN_ASSENTE", "usable_candidates": []},
                "larice": {"status": "EAN_ASSENTE", "usable_candidates": []},
            },
        }]
        shortlists = [
            {"gestionale_source_row": 273, "supplier": "noce", "description": LINES_GESTIONALE["description"],
             "candidates": [candidato(LINES_NOCE, 0.84)]},
            {"gestionale_source_row": 273, "supplier": "larice", "description": LINES_GESTIONALE["description"],
             # Above the suspicious-rejection threshold on purpose: see
             # `test_un_rifiuto_sostituito_non_conta_fra_i_rifiuti_da_guardare`.
             "candidates": [candidato(riga, 0.7) for riga in larice]},
        ]
        decisioni = [
            {"gestionale_source_row": 273, "supplier": "noce", "action": "ACCEPT",
             "source_row": 7463, "confidence": confidenza_noce, "rationale": "Stesso prodotto."},
            {"gestionale_source_row": 273, "supplier": "larice", "action": decisione_larice,
             "confidence": "ALTA", "rationale": "Formato non chiaro."},
        ]
        with tempfile.TemporaryDirectory() as cartella:
            return esegui(
                Path(cartella), matching=matching, shortlists=shortlists, decisions=decisioni,
                normalized={"noce": [LINES_NOCE], "larice": larice},
            )

    def test_la_riga_larice_con_lo_stesso_codice_entra_da_confermare(self) -> None:
        risolti, riepilogo = self.lancia()
        larice = risolti[0]["suppliers"]["larice"]

        self.assertEqual(larice["status"], "SEMANTICO_PROPOSTO")
        self.assertEqual(larice["method"], METODO_STESSO_CODICE)
        self.assertEqual(larice["selected"]["source_row"], 1975)
        self.assertEqual(larice["selected"]["unit_price_net"], 2.25)
        self.assertTrue(larice["requires_user_confirmation"])
        self.assertNotEqual(larice["confidence"], CONFIDENZA_SENZA_CONFERMA)
        self.assertEqual(larice["propagato_da"], ["noce"])
        self.assertEqual(larice["prima"]["method"], "AI_RIFIUTATO")
        self.assertIn("NOCE", larice["rationale"])
        self.assertIn("8009496220932", larice["rationale"])
        self.assertEqual(riepilogo["abbinamenti_per_stesso_codice"], 1)
        self.assertEqual(riepilogo["supplier_results"], {"SEMANTICO_PROPOSTO": 2})

    def test_la_fonte_resta_com_era(self) -> None:
        risolti, _ = self.lancia()
        noce = risolti[0]["suppliers"]["noce"]
        self.assertEqual(noce["method"], "SEMANTICO_AI")
        self.assertEqual(noce["selected"]["source_row"], 7463)
        self.assertFalse(noce["requires_user_confirmation"])

    def test_chiede_conferma_anche_se_la_fonte_era_media(self) -> None:
        risolti, _ = self.lancia(confidenza_noce="MEDIA")
        self.assertTrue(risolti[0]["suppliers"]["larice"]["requires_user_confirmation"])

    def test_vale_anche_quando_l_ai_non_aveva_deciso(self) -> None:
        risolti, _ = self.lancia(decisione_larice="UNRESOLVED")
        self.assertEqual(risolti[0]["suppliers"]["larice"]["method"], METODO_STESSO_CODICE)
        self.assertEqual(risolti[0]["suppliers"]["larice"]["prima"]["status"], "DA_VERIFICARE")

    def test_un_rifiuto_sostituito_non_conta_fra_i_rifiuti_da_guardare(self) -> None:
        _, riepilogo = self.lancia()
        self.assertEqual(riepilogo["rifiuti_con_candidato_forte"], 0)

    def test_senza_la_riga_col_codice_non_succede_niente(self) -> None:
        risolti, riepilogo = self.lancia(larice=[LINES_LARICE_X9])
        self.assertEqual(risolti[0]["suppliers"]["larice"]["method"], "AI_RIFIUTATO")
        self.assertEqual(riepilogo["abbinamenti_per_stesso_codice"], 0)


def esito_ai(riga: dict[str, Any], confidenza: str = "ALTA") -> dict[str, Any]:
    return {"status": "SEMANTICO_PROPOSTO", "method": "SEMANTICO_AI", "selected": riga,
            "confidence": confidenza, "requires_user_confirmation": confidenza != "ALTA",
            "rationale": "", "alternatives": []}


RIFIUTO = {"status": "NON_TROVATO", "method": "AI_RIFIUTATO", "selected": None, "confidence": "ALTA",
           "requires_user_confirmation": False, "rationale": "", "alternatives": [], "ai_reject_best_score": 0.4}


class LaRegolaDelloStessoCodiceNeiCasiLimiteTests(unittest.TestCase):
    """`propaga_lo_stesso_codice` in isolation: the cases where it must leave
    everything untouched."""

    def prova(self, esiti: dict[str, dict[str, Any]], listini: dict[str, list[dict[str, Any]]],
              stati: dict[str, str] | None = None, ean_prodotto: str = "8009405394204"):
        prodotto = {
            "gestionale": {**LINES_GESTIONALE, "ean": ean_prodotto},
            "suppliers": {chi: {"status": (stati or {}).get(chi, "EAN_ASSENTE")} for chi in esiti},
        }
        risolto = {"gestionale": prodotto["gestionale"], "suppliers": json.loads(json.dumps(esiti))}
        propagati, ambigui = propaga_lo_stesso_codice(prodotto, risolto, indice_per_codice(listini))
        return risolto["suppliers"], propagati, ambigui

    def test_senza_un_accettato_dall_ai_non_cambia_niente(self) -> None:
        esiti = {"noce": RIFIUTO, "larice": RIFIUTO}
        dopo, propagati, _ = self.prova(esiti, {"noce": [LINES_NOCE], "larice": [LINES_LARICE]})
        self.assertEqual(dopo, esiti)
        self.assertEqual(propagati, [])

    def test_non_sovrascrive_mai_una_riga_gia_scelta(self) -> None:
        esatto = {"status": "EAN_ESATTO", "method": "EAN", "selected": LINES_LARICE_X9, "confidence": "CERTA",
                  "requires_user_confirmation": False, "rationale": "", "alternatives": []}
        for nome, esito_larice in (("per EAN", esatto), ("dall'AI", esito_ai(LINES_LARICE_X9))):
            with self.subTest(nome):
                dopo, propagati, _ = self.prova(
                    {"noce": esito_ai(LINES_NOCE), "larice": esito_larice},
                    {"noce": [LINES_NOCE], "larice": [LINES_LARICE, LINES_LARICE_X9]},
                )
                self.assertEqual(dopo["larice"], esito_larice)
                self.assertEqual(propagati, [])

    def test_non_tocca_una_decisione_scartata(self) -> None:
        scartata = {**RIFIUTO, "status": "DA_VERIFICARE", "method": "REVISIONE",
                    "ai_decisione_scartata": "LISTINO_DISALLINEATO"}
        dopo, _, _ = self.prova({"noce": esito_ai(LINES_NOCE), "larice": scartata},
                                {"noce": [LINES_NOCE], "larice": [LINES_LARICE]})
        self.assertEqual(dopo["larice"], scartata)

    def test_se_il_fornitore_ha_gia_il_codice_del_prodotto_vale_il_suo(self) -> None:
        for stato in ("EAN_AMBIGUO", "EAN_PRESENTE_NON_UTILIZZABILE"):
            with self.subTest(stato):
                dopo, propagati, _ = self.prova(
                    {"noce": esito_ai(LINES_NOCE), "larice": RIFIUTO},
                    {"noce": [LINES_NOCE], "larice": [LINES_LARICE]},
                    stati={"larice": stato},
                )
                self.assertEqual(dopo["larice"], RIFIUTO)
                self.assertEqual(propagati, [])

    def test_due_righe_col_codice_sono_ambigue_e_non_si_propone_niente(self) -> None:
        doppia = {**LINES_LARICE, "source_row": 1976, "description": "ASS. LINDA SETAMORBI X 18 LUNGO PROMO"}
        dopo, propagati, ambigui = self.prova(
            {"noce": esito_ai(LINES_NOCE), "larice": RIFIUTO},
            {"noce": [LINES_NOCE], "larice": [LINES_LARICE, doppia]},
        )
        self.assertEqual(dopo["larice"], RIFIUTO)
        self.assertEqual(propagati, [])
        self.assertEqual(ambigui[0]["righe"], ["1975", "1976"])

    def test_una_riga_non_utilizzabile_non_si_propone(self) -> None:
        omaggio = {**LINES_LARICE, "usable": False}
        dopo, propagati, _ = self.prova({"noce": esito_ai(LINES_NOCE), "larice": RIFIUTO},
                                        {"noce": [LINES_NOCE], "larice": [omaggio]})
        self.assertEqual(dopo["larice"], RIFIUTO)

    def test_un_codice_che_non_e_un_ean_non_e_una_prova(self) -> None:
        for codice in ("", "0", "0000000000000", "8100", "ABC1480408340", "8009496220932.5"):
            with self.subTest(codice=codice):
                fonte = {**LINES_NOCE, "ean": codice}
                dopo, propagati, _ = self.prova({"noce": esito_ai(fonte), "larice": RIFIUTO},
                                                {"noce": [fonte], "larice": [{**LINES_LARICE, "ean": codice}]})
                self.assertEqual(propagati, [])

    def test_il_codice_del_prodotto_stesso_non_si_propaga(self) -> None:
        """If the accepted row already carries the product's own barcode, the
        other suppliers were already searched by EAN and don't have it:
        there's nothing to propagate."""
        dopo, propagati, _ = self.prova(
            {"noce": esito_ai(LINES_NOCE), "larice": RIFIUTO},
            {"noce": [LINES_NOCE], "larice": [LINES_LARICE]},
            ean_prodotto="8009496220932",
        )
        self.assertEqual(propagati, [])

    def test_l_ordine_dei_fornitori_non_cambia_niente_e_non_ci_sono_catene(self) -> None:
        betulla = {**LINES_LARICE, "source_row": 55, "description": "LINDA SETA ULTRA 18 LUNGHI", "unit_price_net": 2.40}
        listini = {"noce": [LINES_NOCE], "larice": [LINES_LARICE], "betulla": [betulla]}
        esiti = {"noce": esito_ai(LINES_NOCE), "larice": RIFIUTO, "betulla": RIFIUTO}
        dritto, propagati, _ = self.prova(esiti, listini)
        rovescio, _, _ = self.prova(dict(reversed(list(esiti.items()))), listini)

        self.assertEqual(dritto, rovescio)
        self.assertEqual(sorted(voce["supplier"] for voce in propagati), ["betulla", "larice"])
        self.assertEqual(dritto["betulla"]["propagato_da"], ["noce"])

    def test_due_fonti_con_lo_stesso_codice_si_nominano_tutte_e_due(self) -> None:
        betulla = {**LINES_NOCE, "source_row": 55, "description": "LINDA SETA Ultra Lunghi 18 Pz"}
        listini = {"noce": [LINES_NOCE], "betulla": [betulla], "larice": [LINES_LARICE]}
        esiti = {"noce": esito_ai(LINES_NOCE), "betulla": esito_ai(betulla), "larice": RIFIUTO}
        dritto, _, _ = self.prova(esiti, listini)
        rovescio, _, _ = self.prova(dict(reversed(list(esiti.items()))), listini)

        self.assertEqual(dritto["larice"]["propagato_da"], ["betulla", "noce"])
        self.assertTrue(dritto["larice"]["rationale"].startswith("BETULLA e NOCE hanno questo prodotto"))
        self.assertIn("«LINDA SETA Ultra Lunghi 18 Pz»", dritto["larice"]["rationale"])
        self.assertEqual(dritto["larice"]["rationale"], rovescio["larice"]["rationale"])

    def test_due_fonti_con_codici_diversi_sono_ambigue_se_il_fornitore_li_ha_tutti_e_due(self) -> None:
        altra = {**LINES_NOCE, "source_row": 90, "ean": "8009440034554"}
        seconda_fonte = {**altra, "source_row": 91}
        listini = {"noce": [LINES_NOCE], "betulla": [seconda_fonte],
                   "larice": [LINES_LARICE, {**altra, "source_row": 1990}]}
        dopo, propagati, ambigui = self.prova(
            {"noce": esito_ai(LINES_NOCE), "betulla": esito_ai(seconda_fonte), "larice": RIFIUTO}, listini)
        self.assertEqual(dopo["larice"], RIFIUTO)
        self.assertEqual(len(ambigui), 1)


if __name__ == "__main__":
    unittest.main()
