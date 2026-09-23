"""Le decisioni della Fase 5b e le quattro porte della 6a, in `merge_match_decisions.py`.

Questo script non aveva nessun test, ed e' quello che decide se un match
proposto da un modello entra in un ordine senza che nessuno lo guardi.

Della 5b si difendono qui due regole:

- un `ACCEPT` con confidenza `ALTA` **non chiede conferma**, tutto il resto si';
- ogni `REJECT` porta con se' il punteggio del suo miglior candidato, che e'
  l'unica traccia che resta di un rifiuto sbagliato.

Della 6a, quattro:

- la riga accettata si verifica contro **la shortlist**, che e' l'unica cosa che
  il modello ha visto, e non contro `matching_result.json`, che e' scritto
  insieme al listino e quindi non puo' disallinearsi da esso;
- una riga che al modello non e' stata mostrata non entra in un ordine;
- `--decisions-attese` e' obbligatorio, e zero e' un valore legittimo;
- contare le decisioni non dimostra che appartengano a questa run.
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


# `attese` non passato affatto e' una cosa, `attese=None` e' un'altra: la
# seconda serve a provare che il flag e' obbligatorio.
DERIVA = object()


def scrivi(cartella: Path, nome: str, documento: Any) -> Path:
    percorso = cartella / nome
    percorso.write_text(json.dumps(documento, ensure_ascii=False), encoding="utf-8")
    return percorso


def normalizzato_dalle_shortlist(shortlists: list[dict[str, Any]]) -> dict[str, Any]:
    """Il listino normalizzato coerente con le shortlist che ne derivano.

    Nella catena vera `semantic_shortlists.json` si costruisce **leggendo**
    `normalized_sources.json`, quindi i due file combaciano sempre riga per
    riga. Le prove partono da qui: il caso interessante e' quando smettono di
    combaciare, e allora il disallineamento lo si costruisce apposta."""
    per_fornitore: dict[str, dict[int, dict[str, Any]]] = {}
    for shortlist in shortlists:
        righe = per_fornitore.setdefault(shortlist["supplier"], {})
        for candidato in shortlist.get("candidates", []):
            righe[candidato["source_row"]] = {
                "source_row": candidato["source_row"],
                "ean": candidato.get("ean", ""),
                "description": candidato.get("description", ""),
                "unit_price_net": candidato.get("unit_price_net", 1.0),
                # Il campo che nella shortlist non c'e' e che il confronto usa:
                # e' la ragione per cui il record completo va preso dal listino.
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
    """Mette su ogni decisione l'impronta del caso, come fa `valuta_shortlist.py`.

    Nella catena vera la scrive chi ha chiamato il modello, e senza di lei la
    decisione non viene applicata. Qui la mette il banco, cosi' ogni prova che
    parla d'altro non deve occuparsene — e chi vuole provare **l'assenza** o
    un'impronta sbagliata passa `timbra=False`, o se la scrive da se': una
    decisione che ce l'ha gia' non viene toccata."""
    def coppia(riga: Any, fornitore: Any) -> tuple[Any, Any]:
        # Come `indicizza_decisioni`: `"12"` e `12` sono la stessa coppia, e un
        # test scrive apposta la riga come stringa.
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
    """Lancia lo script davvero, come fa la pipeline, e restituisce quello che
    la pipeline vede: codice d'uscita, uscita a schermo e il percorso del file.

    `decisions=None` vuol dire che l'argomento `--decisions` non si passa
    affatto, che e' il caso in cui la fase AI non e' mai stata eseguita.
    `attese=None` vuol dire che non si passa `--decisions-attese`.
    `con_impronta=False` scrive le decisioni cosi' come sono, cioe' senza
    l'impronta che la fase AI ci mette sempre."""
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
    """Come sopra, ma pretende che sia andata bene: esito e riepilogo."""
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


# Due prodotti, non uno: il file vero ne ha 942, e con un prodotto solo un
# guasto che dipende dall'ordine non ha modo di farsi vedere.
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
    """I numeri stanno scritti in `SKILL.md` e l'orchestratore ci reagisce in
    modo diverso. Se due guasti collassano sullo stesso numero, la reazione
    giusta diventa impossibile e niente lo segnala."""

    def test_sono_i_numeri_promessi(self) -> None:
        self.assertEqual(USCITA_OK, 0)
        self.assertEqual(USCITA_INGRESSO_NON_UTILIZZABILE, 2)
        self.assertEqual(USCITA_DECISIONI_NON_RICONCILIATE, 3)
        self.assertEqual(USCITA_LISTINO_DISALLINEATO, 4)
        self.assertEqual(USCITA_RIGA_INVENTATA, 5)

    def test_l_uscita_e_utf8_anche_con_le_accentate(self) -> None:
        """Il riepilogo porta le descrizioni vere dei prodotti. Su Windows un
        processo che scrive su una pipe usa cp1252: basta un `CAFFÈ` perché chi
        legge si trovi davanti a byte che non sono UTF-8, e l'orchestratore
        della 6c leggerà proprio questa uscita."""
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
        """Non si legge `candidates[0]`: l'ordinamento appartiene a chi ha
        scritto la shortlist, e questa funzione non deve dipenderne."""
        shortlist = {"candidates": [{"score": 0.30}, {"score": 0.91}, {"score": 0.44}]}
        self.assertAlmostEqual(miglior_punteggio(shortlist), 0.91)

    def test_senza_punteggi_e_none_non_zero(self) -> None:
        """«Non lo so» e «somigliava zero» sono due cose diverse: la seconda
        farebbe passare per innocuo un rifiuto di cui non si sa niente."""
        self.assertIsNone(miglior_punteggio({"candidates": [{"description": "senza punteggio"}]}))
        self.assertIsNone(miglior_punteggio({"candidates": []}))
        self.assertIsNone(miglior_punteggio({}))

    def test_un_booleano_non_e_un_punteggio(self) -> None:
        """In Python `True` e' un intero e passerebbe per 1,0: sarebbe il
        punteggio piu' alto possibile, prodotto da un dato malformato."""
        self.assertIsNone(miglior_punteggio({"candidates": [{"score": True}]}))


class IdentitaTests(unittest.TestCase):
    """Che cosa prova che due righe sono lo stesso prodotto."""

    def test_spazi_e_maiuscole_non_contano(self) -> None:
        """L'identita' non deve essere piu' severa di quanto serve: due
        scritture della stessa descrizione sono lo stesso prodotto."""
        self.assertEqual(
            identita({"ean": " 8001 ", "description": "pantera  shampoo", "unit_price_net": 1.0}),
            identita({"ean": "8001", "description": "PANTERA SHAMPOO", "unit_price_net": 1.0}),
        )

    def test_un_ean_nullo_e_uno_vuoto_sono_lo_stesso_prodotto(self) -> None:
        """Nei listini veri l'EAN mancante arriva dal JSON come `null`, non
        come chiave assente: `null` contro `""` sarebbe un disallineamento
        inventato che ferma una run buona."""
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
        """Due lotti dello stesso articolo con prezzo diverso non sono
        intercambiabili: la riga che finisce in ordine e' quella da cui si
        prende il prezzo."""
        self.assertNotEqual(
            identita({"ean": "8001", "description": "X", "unit_price_net": 2.18}),
            identita({"ean": "8001", "description": "X", "unit_price_net": 99.99}),
        )

    def test_un_prezzo_assente_non_diventa_zero(self) -> None:
        self.assertIsNone(identita({"ean": "8001", "description": "X"})[2])
        self.assertIsNone(identita({"ean": "8001", "description": "X", "unit_price_net": None})[2])
        self.assertIsNone(identita({"ean": "8001", "description": "X", "unit_price_net": True})[2])

    def test_un_prezzo_in_stringa_e_un_prezzo(self) -> None:
        """⚠ `prepare_sources.py` scrive i prezzi **come stringhe**: la prima
        versione accettava solo numeri e sui dati veri 0 righe su 25.093 avevano
        un prezzo nell'identita'. Il campo c'era e non confrontava niente."""
        self.assertEqual(prezzo_confrontabile("2.1000"), 2.1)
        self.assertNotEqual(
            identita({"ean": "8001", "description": "X", "unit_price_net": "1.0000"}),
            identita({"ean": "8001", "description": "X", "unit_price_net": "9.9900"}),
        )

    def test_lo_stesso_numero_scritto_in_due_modi_e_lo_stesso_prezzo(self) -> None:
        """Il verso opposto conta quanto l'altro: se un artefatto porta `1.0` e
        l'altro `"1.0"`, due scritture dello stesso numero non devono diventare
        due prodotti diversi e fermare una run sana."""
        self.assertEqual(
            identita({"ean": "8001", "description": "X", "unit_price_net": 1.0}),
            identita({"ean": "8001", "description": "X", "unit_price_net": "1.0000"}),
        )

    def test_un_prezzo_che_non_e_un_numero_resta_none(self) -> None:
        self.assertIsNone(prezzo_confrontabile("n.d."))
        self.assertIsNone(prezzo_confrontabile(""))


class ConfermaDegliAccettatiTests(unittest.TestCase):
    """La decisione di Daniele dell'11 agosto 2026, misurata sul banco nella 5b."""

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
        """Il valore che non si riconosce non e' `ALTA`: nel dubbio si chiede.
        E' l'unico verso in cui questo confronto puo' sbagliare senza danno."""
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
        """Il conteggio dice quanto pesa la fiducia data ad `ALTA`: se conta
        anche i `MEDIA`, che una conferma la chiedono, non dice piu' niente."""
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
    """Un rifiuto sbagliato non lascia traccia: il punteggio del miglior
    candidato e' l'unica che si puo' avere senza pagare altre chiamate."""

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
        """La soglia la applica chi mostra i dati, in un posto solo: qui il
        numero si scrive sempre, altrimenti cambiare soglia vorrebbe dire
        rifare la run."""
        risultato, _riepilogo = self._rifiuta_con([0.11])
        self.assertAlmostEqual(risultato["ai_reject_best_score"], 0.11)

    def test_sopra_soglia_il_riepilogo_lo_conta(self) -> None:
        _risultato, riepilogo = self._rifiuta_con([SOGLIA_RIFIUTO_SOSPETTO + 0.05])
        self.assertEqual(riepilogo["rifiuti_con_candidato_forte"], 1)

    def test_sotto_soglia_non_lo_conta(self) -> None:
        _risultato, riepilogo = self._rifiuta_con([SOGLIA_RIFIUTO_SOSPETTO - 0.05])
        self.assertEqual(riepilogo["rifiuti_con_candidato_forte"], 0)

    def test_esattamente_sulla_soglia_conta(self) -> None:
        """Il confronto e' `>=`: un caso al limite deve stare da una parte
        sola e dichiarata, non dipendere da come e' scritto il codice."""
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
        """Il campo esiste solo dove significa qualcosa: metterlo ovunque
        renderebbe impossibile distinguere «rifiutato dall'AI» da tutto il resto."""
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
    """La prima porta della 6a.

    Il numero di riga identifica una posizione, non un prodotto. Se il listino
    viene riletto e ha una riga in piu' in testa, la riga 100 e' un altro
    articolo — e una decisione `ALTA` lo metterebbe in ordine senza chiedere
    niente a nessuno."""

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
        """La controprova positiva: quando i due file combaciano il match entra,
        e `selected` e' il record del listino, non il candidato della shortlist
        — che il moltiplicatore d'ordine non ce l'ha."""
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
        """Il caso che fa il danno: stessa riga 100, prodotto diverso."""
        esito, percorso, _cartella = self._con_listino([{
            "source_row": 100, "ean": "", "description": "TUTT ALTRO PRODOTTO",
            "unit_price_net": 9.99, "order_multiplier": 6,
        }])
        self.assertEqual(esito.returncode, USCITA_LISTINO_DISALLINEATO)
        riepilogo = json.loads(esito.stdout)
        self.assertEqual(riepilogo["decisioni_scartate_per_disallineamento"], 1)
        self.assertEqual(riepilogo["disallineamenti"][0]["mostrato_al_modello"], "CANDIDATO 0")
        self.assertEqual(riepilogo["disallineamenti"][0]["trovato_nel_listino"], "TUTT ALTRO PRODOTTO")
        # Il file si scrive lo stesso, con la coppia degradata: a valle
        # `build_review_data` non distingue un file assente da una lista vuota,
        # e il file della run precedente resterebbe li' a farsi leggere.
        risolti = json.loads(percorso.read_text(encoding="utf-8"))
        risultato = risolti[0]["suppliers"]["betulla"]
        self.assertEqual(risultato["status"], "DA_VERIFICARE")
        self.assertIsNone(risultato["selected"])
        self.assertIs(risultato["requires_user_confirmation"], True)

    def test_una_decisione_scartata_non_conta_fra_gli_accettati_senza_conferma(self) -> None:
        """Il riepilogo non deve vantarsi di un match che ha buttato."""
        esito, _percorso, _cartella = self._con_listino([{
            "source_row": 100, "ean": "", "description": "TUTT ALTRO",
            "unit_price_net": 9.99, "order_multiplier": 6,
        }])
        riepilogo = json.loads(esito.stdout)
        self.assertEqual(riepilogo["accettati_senza_conferma"], 0)
        self.assertEqual(riepilogo["supplier_results"], {"DA_VERIFICARE": 1})

    def test_un_ean_diverso_basta_da_solo(self) -> None:
        """Stessa descrizione, EAN diverso: e' un altro articolo, e nei listini
        veri succede — le descrizioni si ripetono, gli EAN no."""
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
        """Stesso EAN, stessa descrizione, prezzo diverso: nei listini veri sono
        due lotti, e quello che finisce in ordine dev'essere quello giusto."""
        esito, _percorso, _cartella = self._con_listino([{
            "source_row": 100, "ean": "", "description": "CANDIDATO 0",
            "unit_price_net": 99.99, "order_multiplier": 6,
        }])
        self.assertEqual(esito.returncode, USCITA_LISTINO_DISALLINEATO)

    def test_una_riga_sparita_dal_listino_non_ripiega_sulla_shortlist(self) -> None:
        """Prima il codice ripiegava sul candidato della shortlist, che non ha
        il moltiplicatore d'ordine: l'offerta finiva nel confronto come non
        disponibile, senza che niente dicesse perche'."""
        esito, _percorso, _cartella = self._con_listino([])
        self.assertEqual(esito.returncode, USCITA_LISTINO_DISALLINEATO)
        self.assertIsNone(json.loads(esito.stdout)["disallineamenti"][0]["trovato_nel_listino"])

    def test_un_listino_senza_ean_non_fa_morire_la_run(self) -> None:
        """`null` nel listino e `""` nella shortlist sono lo stesso prodotto:
        un EAN mancante e' la norma, non un guasto."""
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
        """Il difetto che la revisione avversariale ha trovato nella prima
        versione della 6a.

        Per un `EAN_AMBIGUO` il candidato veniva preso da
        `matching_result.json`, che `prepare_sources.py` scrive **insieme** al
        listino normalizzato: confrontarli e' confrontare il listino con se
        stesso, e la guardia non scattava mai. Il modello, invece, vede solo la
        shortlist. Qui la shortlist e' vecchia e il listino no: la decisione
        dev'essere buttata."""
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
        """La controprova: quando shortlist e listino combaciano, l'ambiguo
        risolto dall'AI resta distinguibile da un match semantico."""
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
    """La seconda porta: il modello non puo' nominare una riga che non gli e'
    stata mostrata. La regola c'era gia' ma finiva in uno schianto, e uno
    schianto non si distingue da un altro."""

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
        """Su un `EAN_AMBIGUO` la riga giusta non è un'opinione: il fornitore ha
        più righe con l'EAN del gestionale e la scelta sta fra quelle. Ma la
        shortlist l'EAN non lo guarda mai — ordina per token della descrizione —
        quindi può mostrare al modello una riga che l'EAN non ce l'ha. Con
        `ALTA` finirebbe in ordine senza conferma."""
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
        """La controprova: dove l'EAN non c'è, il vincolo non deve esistere —
        altrimenti butterebbe tutti i match semantici, che sono il 100% del
        lavoro dell'AI sui dati veri."""
        with tempfile.TemporaryDirectory() as temporanea:
            _risolti, riepilogo = esegui(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=shortlist_di_prova([0.72]),
                decisions=ACCETTA_LA_100,
            )
        self.assertEqual(riepilogo["decisioni_scartate_per_ean_non_rispettato"], 0)

    def test_una_riga_dei_candidati_ean_non_e_una_riga_mostrata(self) -> None:
        """Prima erano ammesse anche le righe di `usable_candidates`, che pero'
        al modello non arrivano: il passo AI costruisce i suoi casi dalle sole
        shortlist. Una riga ammessa e mai mostrata e' una porta aperta."""
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
    """La terza porta. E' la stessa lezione del banco di prova della 5a, che
    dichiarava «ALTA sbagliati 0» su una passata in cui nessuna risposta era
    mai arrivata: un fallimento non deve poter somigliare a un punteggio pieno."""

    def test_decisions_attese_e_obbligatorio(self) -> None:
        """Facoltativo non chiudeva niente: bastava dimenticarlo."""
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
        """Il caso vero: la fase AI ne ha decise 900, il file ne porta 12
        perche' la scrittura si e' interrotta a meta'."""
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
        """Il verso opposto, che e' quello pericoloso: il chiamante dichiara che
        la fase AI e' degradata, ma sul disco c'e' il file di ieri. Un `ACCEPT`
        `ALTA` della run precedente entrerebbe in ordine senza conferma."""
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
        # Anche col degrado dichiarato: «il file non c'e' perche' siamo
        # degradati» e «non c'e' perche' la scrittura e' fallita» sono due cose.
        self.assertEqual(esito.returncode, USCITA_DECISIONI_NON_RICONCILIATE)

    def test_il_degrado_dichiarato_resta_legittimo(self) -> None:
        """«Se OpenRouter non risponde il programma tira dritto» e' una
        decisione commerciale, non un difetto: zero decisioni **dichiarate**
        passano, e i casi restano da verificare."""
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
        """Dichiarare 900 decisioni su una run risolta tutta dall'EAN e' un
        guasto come gli altri: il controllo non si salta perche' non c'era
        lavoro da fare."""
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
    """La quarta porta. Un file di decisioni di un'altra run riconcilia
    benissimo — dichiarate 942, trovate 942 — e non se ne applica nemmeno una."""

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
        """Succede ogni volta che si rifanno i passi deterministici dopo la fase
        AI: una coppia passa a `EAN_ESATTO` e la sua decisione non è più
        applicabile. Il risultato è **migliore** di quello che l'AI proponeva —
        vince l'EAN — e fermare la catena manderebbe a rifare, cioè a pagare,
        la fase AI su una run sana."""
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
        """Il contatore deve dire quante **decisioni** l'EAN ha superato, non
        quante coppie l'EAN ha risolto: quelle sono la maggioranza dei prodotti,
        e contarle renderebbe il numero inventato senza che l'esito cambi."""
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
        """`BETULLA` invece di `betulla`: il numero torna, il lavoro no."""
        with tempfile.TemporaryDirectory() as temporanea:
            esito, _percorso = esegui_grezzo(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=shortlist_di_prova([0.72]),
                decisions=[{**ACCETTA_LA_100[0], "supplier": "BETULLA"}],
            )
        self.assertEqual(esito.returncode, USCITA_DECISIONI_NON_RICONCILIATE)

    def test_una_source_row_scritta_come_stringa_si_lega_lo_stesso(self) -> None:
        """JSON scritto da un altro programma può portare `"12"`: non e' un
        guasto, e trattarlo come tale butterebbe via una decisione buona. Vale
        per tutte e due le righe, non solo per quella della coppia: sull'altra
        la conseguenza era peggiore — esito 5, cioe' «il modello ha risposto
        fuori dal recinto», per un numero scritto in un altro modo."""
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
        """Lo schema del client ammette `source_row: null`. Raccontarlo come
        allucinazione manderebbe chi legge a cercare il guasto nel modello."""
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
        """Vince l'ultima riga del file: un `ACCEPT` diventerebbe un `REJECT` e
        il prodotto sparirebbe dal confronto per l'ordine delle righe."""
        with tempfile.TemporaryDirectory() as temporanea:
            esito, percorso = esegui_grezzo(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=shortlist_di_prova([0.72]),
                decisions=[
                    ACCETTA_LA_100[0],
                    {**ACCETTA_LA_100[0], "action": "REJECT", "source_row": None},
                ],
                # Dichiarata **una**: e' il caso che la riconciliazione da sola
                # non fermerebbe, cioe' quello in cui serve il controllo sui
                # duplicati e non la conta delle righe.
                attese=1,
            )
            self.assertFalse(percorso.exists())
        self.assertEqual(esito.returncode, USCITA_DECISIONI_NON_RICONCILIATE)
        self.assertIn("duplicata", esito.stdout)

    def test_un_file_di_decisioni_che_non_e_una_lista(self) -> None:
        """`{"decisions": [...]}` produceva un `TypeError` e un traceback."""
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
        # Il messaggio dice qual è il problema. Senza, l'esito è giusto per
        # caso — il controllo a valle su ogni singola decisione lo intercetta
        # comunque, ma dicendo un'altra cosa.
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
        """Un `EAN_AMBIGUO` e' lavoro per l'AI come gli altri: escluderlo
        farebbe uscire con successo una run in cui l'AI non ha risolto niente."""
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
        """Il client rifiuta di mandare al modello un caso senza candidati: sono
        sei su 948 nella run vera, e senza questo numero «trovate 942 su una
        coda di 948» si legge come «ne mancano sei»."""
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
    """Una sola esecuzione con tutti e tre i guasti addosso.

    12/betulla: riga inventata. 13/betulla: listino slittato. 777/betulla: decisione
    che non trova la sua coppia."""
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
    """Tre guasti insieme escono con **un** numero solo, e dev'essere il piu'
    grave. Se l'ordine si rovescia, una riga inventata — il modello che risponde
    fuori dal recinto — si annuncia come «decisioni non riconciliate»."""

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
    """«Non applicata» vuol dire «i due file non parlano della stessa run», ed e'
    la diagnosi che manda a cercare il file sbagliato. Una decisione buttata
    perche' il modello ha sbagliato riga la sua coppia l'ha trovata eccome:
    contarla di la' fa comparire nel riepilogo una coppia che esiste, sotto il
    titolo «coppie senza riscontro»."""

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
    """Vale anche per l'esito 3 da decisioni senza riscontro, che e' l'unico dei
    tre guasti «a valle» in cui nessuno guardava il file. Se non si scrive,
    quello della run precedente resta sul disco a farsi leggere come fresco."""

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
    """Il campo puo' mancare: questo script e' l'ultimo cancello e non puo'
    fidarsi di chi ha scritto il file. Il valore mancante deve cadere dalla
    parte che chiede conferma, mai da quella che ordina da sola."""

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
    """`build_review_data.py` legge questi campi per decidere che cosa mostrare e
    che cosa dare per confermato. Nessuno li fissava."""

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
        """Sono la maggioranza dei prodotti: farli confermare uno per uno
        vorrebbe dire centinaia di clic, e uscendo 0 nessuno se ne accorge."""
        risultato = self._solo_ean()
        self.assertEqual(risultato["status"], "EAN_ESATTO")
        self.assertEqual(risultato["method"], "EAN")
        self.assertEqual(risultato["confidence"], "CERTA")
        self.assertIs(risultato["requires_user_confirmation"], False)

    def test_il_rifiuto_dell_ai_si_riconosce_dallo_stato_e_dal_metodo(self) -> None:
        """`build_review_data.py` accende l'avviso «scartato, ma somigliava»
        filtrando su `method == "AI_RIFIUTATO"`, e `NON_TROVATO` e' cio' che dice
        alla pagina che presso quel fornitore il prodotto non c'e'."""
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
    """`app/ai_client.py` declassa a `UNRESOLVED` **ogni** `ACCEPT` che la
    verifica avversariale non conferma: nella run vera ne arrivano a decine.
    Qui non c'era una riga di prova, e rifiutarli farebbe uscire 2 — cioe'
    fermerebbe tutta la catena — proprio quando la difesa ha funzionato."""

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
        """Le prove di prima usavano 2,18 contro 99,99: un arrotondamento
        all'euro le lasciava passare tutte, e nei listini veri i due lotti dello
        stesso articolo differiscono di centesimi."""
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
    """Come i codici d'uscita: sta scritta nel piano archiviato con la misura
    che l'ha scelta, e `build_review_data.py` la importa da qui per accendere
    l'avviso. Cambiarla di nascosto cambia quanti rifiuti sbagliati si vedono."""

    def test_e_quella_misurata_sul_banco(self) -> None:
        self.assertAlmostEqual(SOGLIA_RIFIUTO_SOSPETTO, 0.65)


class QuelloCheIlRevisoreSiVedeArrivareTests(unittest.TestCase):
    """La coppia degradata torna a una persona, e quella persona deve poter
    scegliere: senza i candidati e senza la motivazione ha davanti una riga
    vuota e nessun modo di decidere se non riaprire i listini a mano."""

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
    """Il buco simmetrico a «--decisions-attese obbligatorio»: il flag c'e' e
    dice 900, ma `--decisions` no. Senza questo controllo la catena legge zero
    decisioni e le dichiara un degrado voluto."""

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
    """«Nessuno leggera' i log» vale soprattutto qui: se la catena si ferma
    senza numeri, chi la riavvia non sa se ne mancavano due o novecento."""

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
    """Il riepilogo lo legge una persona: `null` contro `null` non dice niente,
    e nei listini veri le righe senza descrizione ne' EAN esistono."""

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
    """Un candidato senza `source_row` non deve far schiantare il riepilogo che
    spiega perche' la riga e' stata rifiutata: sarebbe un traceback al posto del
    messaggio, sull'unico esito che il messaggio ce l'ha."""

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
    """La porta che la 6a aveva lasciato aperta e la 6b chiude.

    Tutte le altre guardie confrontano fra loro artefatti della **run
    corrente** — la riga accettata con la shortlist, la shortlist con il listino
    — quindi rispetto a un file di decisioni di un'altra run sono cieche per
    costruzione: il gestionale e' lo stesso file di settimana in settimana, le
    coppie `(riga, fornitore)` si sovrappongono quasi tutte e i conteggi
    riconciliano. L'impronta e' l'unico campo che porta con se' che cosa il
    modello aveva davanti."""

    def test_una_decisione_senza_impronta_non_viene_applicata(self) -> None:
        """Ammetterla renderebbe la guardia aggirabile dimenticandosi un campo,
        che e' esattamente il difetto che `--decisions-attese` ha chiuso."""
        with tempfile.TemporaryDirectory() as temporanea:
            esito, percorso = esegui_grezzo(
                Path(temporanea),
                matching=matching_di_prova(),
                shortlists=shortlist_di_prova([0.72]),
                decisions=ACCETTA_LA_100,
                con_impronta=False,
            )
            # Dentro il `with`: fuori, la cartella temporanea non c'e' piu' e
            # ogni prova sul file passerebbe per la ragione sbagliata.
            risolti = json.loads(percorso.read_text(encoding="utf-8"))
        self.assertEqual(esito.returncode, USCITA_DECISIONI_NON_RICONCILIATE)
        match = risolti[0]["suppliers"]["betulla"]
        self.assertEqual(match["status"], "DA_VERIFICARE")
        # Causa distinta da quella del file vecchio: qui il guasto è un
        # produttore che ha dimenticato un campo, e mandare a cercare un file
        # di un'altra run vorrebbe dire mandare dalla parte sbagliata.
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
        """Un `REJECT` vecchio fa sparire il prodotto da quel fornitore, e
        niente lo direbbe: e' il genere di guasto silenzioso peggiore di uno
        rumoroso. Deve tornare al revisore, non diventare `NON_TROVATO`."""
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
        """La cosa da fare e' diversa: qui si rifa' la fase AI, li' si guarda il
        modello. Se il caso e' di un'altra run, la riga che nomina non dice
        niente sul modello."""
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
        """Il gemello del test qui sopra, dall'altro lato del bivio, ed è il
        ramo che conta di più: su un `EAN_AMBIGUO` una decisione applicata
        diventa `EAN_AMBIGUO_RISOLTO_AI` e con `ALTA` entra in ordine **senza
        conferma**. Tutte le altre prove di questa classe girano su
        `EAN_ASSENTE`, e la mutazione che spegneva la guardia sui soli ambigui
        restava verde: una difesa con due rami, provata su un ramo solo — lo
        stesso difetto della 6a."""
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
        """Ogni prova di questa classe ha un prodotto solo, e il caso vero ne ha
        942. Chi legge «il file è di un'altra run» è tentato di buttarlo tutto:
        buttare anche le decisioni buone rimanderebbe al revisore centinaia di
        coppie già decise, e la mutazione che le contagia restava verde."""
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
        """Il numero non era mai stato visto sopra 1: un `len()` diventato
        `bool()` e un elenco troncato a uno passavano lisci. Il vincolo del
        progetto è «ciò che viene scartato va **contato**», e due è il più
        piccolo numero che distingue un conteggio da una spia."""
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
        """Il ramo `EAN_ESATTO` non guarda l'impronta, ed è giusto — vince
        l'EAN, che è meglio di qualunque cosa dica il modello — ma nessuna
        prova lo diceva. Va fissato nei due sensi: la decisione vecchia non si
        applica **e** non ferma la catena, perché rifare la fase AI su una run
        sana vuol dire ripagarla."""
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
        """Un conteggio senza le descrizioni manda a leggere due file JSON per
        capire che cosa e' successo. L'articolo della decisione compare **solo
        quando e' diverso**: nel caso che questa guardia intercetta davvero —
        sono cambiati i candidati — i due sarebbero sempre uguali, e due campi
        sempre uguali sembrano un difetto invece di un'informazione."""
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
    """Che cosa fa cambiare l'impronta, e che cosa no."""

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
        """I due che la calcolano leggono dati di provenienza diversa: uno
        passa quello che ha mandato al modello, l'altro quello che trova nel
        file. `441` e `"441"` sono lo stesso numero di riga, e trattarli come
        due farebbe buttare via una run sana."""
        self.assertEqual(
            self.caso(gestionale_source_row="12", candidati=[(100, "PANTERA SH.250", "0.7"), ("101", "PANTERA BALSAMO", 0.4)]),
            self.caso(),
        )

    def test_non_solleva_mai_su_un_valore_storto(self) -> None:
        """Serve proprio a dire «questo file non va bene»: schiantarsi mentre lo
        dice sarebbe un traceback al posto del messaggio. ⚠ Il numero enorme
        non è un caso di scuola: un intero da 401 cifre faceva sollevare
        `OverflowError` da `float()`, e con lui moriva il merge **prima** di
        scrivere `resolved_matches.json` — lasciando sul disco quello della run
        precedente, da farsi leggere come fresco."""
        for storto in (
            (None, None, {"non": "un testo"}, [(None, ["lista"], "molto")]),
            (10 ** 400, "betulla", "X", [(1, "Y", 0.5)]),
            (12, "betulla", "X", [(10 ** 400, "Y", 10 ** 400)]),
            (float("inf"), "betulla", "X", [(1, "Y", float("nan"))]),
        ):
            with self.subTest(str(storto)[:40]):
                self.assertRegex(impronta_caso(*storto), r"^[0-9a-f]{16}$")

    def test_lo_stesso_numero_scritto_in_due_modi_da_la_stessa_impronta(self) -> None:
        """`test_non_cambia_se_un_numero_e_scritto_come_testo` passava anche se
        `_confrontabile` si fosse limitata a `str(valore)`: `12` e `"12"`
        diventano tutti e due `"12"`. Quello che la funzione fa davvero —
        portare tutto a numero — lo prova solo una coppia che come testo è
        diversa e come numero è uguale."""
        self.assertEqual(self.caso(), self.caso(
            gestionale_source_row=12.0,
            candidati=[(100.0, "PANTERA SH.250", 0.70), (101, "PANTERA BALSAMO", 0.4)]))
        self.assertEqual(self.caso(), self.caso(
            gestionale_source_row="12.0",
            candidati=[(100, "PANTERA SH.250", ".7"), (101, "PANTERA BALSAMO", 0.4)]))



# Il caso vero del 18 settembre 2026, coi numeri veri: il gestionale chiama le
# Lines 8009405394204, NOCE e LARICE 8009496220932.
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
    """La regola 7: il codice di una riga accettata dall'AI vale anche altrove.

    Il 18 settembre 2026 le Lines sono andate a NOCE a 2,31 mentre LARICE le
    aveva a 2,25 con lo stesso codice a barre: l'AI aveva accettato NOCE e
    rifiutato LARICE, scritto abbreviato. Qui la stessa situazione passa per lo
    script vero.
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
             # Sopra la soglia del rifiuto sospetto, apposta: vedi
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
    """`propaga_lo_stesso_codice` da sola: quando non deve toccare niente."""

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
        """Se la riga accettata ha gia' il codice del prodotto, gli altri fornitori
        lo hanno cercato per EAN e non l'hanno: non c'e' niente da portare."""
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
