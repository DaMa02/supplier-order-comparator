#!/usr/bin/env python3
"""Due codici a barre dichiarati uguali, e che cosa succede alla catena.

Il caso da cui nasce, misurato il 17 agosto 2026 sul confronto vero
`2026-08-17_1746`. `LUXA SAPONE LIQ. EROG.250ML` sta nel gestionale col codice
4009428623194, che **solo CIPRESSO** usa, a 1,28 €/pz. Lo stesso articolo sta su
NOCE, LARICE e BETULLA sotto **8729721830575**, a 1,15, 1,1625 e 1,19.

Non è un difetto della selezione automatica: la shortlist di BETULLA aveva la riga
giusta, **seconda**, a 0,572 contro lo 0,578 della variante sbagliata. La parola
che decide è `ORIGINAL` contro `SETA`, e nel nome del gestionale non c'è —
`EROG.` sta per erogatore. È indecidibile dal testo, quindi nessun punteggio e
nessun modello lo risolveranno mai; una persona col listino davanti sì.

Le due direzioni dell'errore non hanno lo stesso prezzo, e le prove sono scritte
su questo:

* **non dichiarare un'uguaglianza** costa un prodotto comprato più caro.
* **dichiararne una sbagliata** costa un ordine sbagliato, presso **tutti** i
  fornitori, **ogni settimana**, finché qualcuno non la toglie.

Per questo la prova più importante qui dentro non è che l'uguaglianza funzioni:
è che **senza uguaglianze dichiarate la catena faccia esattamente quello che
faceva prima**, byte per byte.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

RADICE = Path(__file__).resolve().parents[1]
SCRIPTS = RADICE / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(RADICE / "app"))

import prepare_sources  # noqa: E402
from prepare_manifest_sources import leggi_uguaglianze  # noqa: E402

EAN_GESTIONALE = "4009428623194"
EAN_FORNITORI = "8729721830575"


def prodotto(source_row: int, ean: str, descrizione: str) -> dict[str, Any]:
    return {"source_row": source_row, "ean": ean, "description": descrizione}


def riga(source_row: int, ean: str, descrizione: str, prezzo: str = "1.15",
         **extra: Any) -> dict[str, Any]:
    return {
        "source_row": source_row,
        "ean": ean,
        "description": descrizione,
        "unit_price_net": prezzo,
        "pieces_per_carton": "6",
        "usable": True,
        **extra,
    }


# Il caso vero in miniatura: il gestionale con un codice, tre fornitori con
# l'altro, e CIPRESSO che usa lo stesso del gestionale.
MASTER = [prodotto(330, EAN_GESTIONALE, "LUXA SAPONE LIQ. EROG.250ML")]
FORNITORI = {
    "cipresso": [riga(900, EAN_GESTIONALE, "LUXA SAPONE LIQUIDO EROGATORE 250", "1.28")],
    "noce": [riga(4794, EAN_FORNITORI, "LUXA SAPONE EROGATORE ORIGINAL ML.250", "1.15")],
    "betulla": [riga(1751, EAN_FORNITORI, "LUXA Sapone Liquido Original 250 Ml", "1.19")],
    "larice": [riga(1056, EAN_FORNITORI, "SAP. LIQ. LUXA BASE 250 TRADIZ.LE", "1.1625")],
}


def stati(matching: list[dict[str, Any]]) -> dict[str, str]:
    return {nome: voce["status"] for nome, voce in matching[0]["suppliers"].items()}


class SenzaDichiarazioniNienteCambiaTests(unittest.TestCase):
    """La prova che protegge i 933 abbinamenti che già funzionano."""

    def test_il_risultato_e_identico_a_quello_di_prima(self) -> None:
        senza_argomento = prepare_sources.build_matching(MASTER, FORNITORI)
        con_none = prepare_sources.build_matching(MASTER, FORNITORI, uguaglianze=None)
        con_vuoto = prepare_sources.build_matching(MASTER, FORNITORI, uguaglianze=[])

        self.assertEqual(stati(senza_argomento[0]), {
            "cipresso": "EAN_ESATTO",
            "noce": "EAN_ASSENTE",
            "betulla": "EAN_ASSENTE",
            "larice": "EAN_ASSENTE",
        })
        self.assertEqual(senza_argomento[0], con_none[0])
        self.assertEqual(senza_argomento[0], con_vuoto[0])
        self.assertEqual(senza_argomento[1], con_vuoto[1])

    def test_un_gruppo_di_un_codice_solo_non_e_una_dichiarazione(self) -> None:
        matching, _, audit = prepare_sources.build_matching(
            MASTER, FORNITORI, uguaglianze=[[EAN_GESTIONALE], []],
        )

        self.assertEqual(stati(matching)["noce"], "EAN_ASSENTE")
        self.assertEqual(audit["declared_equivalences"], 0)


class UnaDichiarazioneValeSuTuttiIFornitoriTests(unittest.TestCase):
    def esegui(self, uguaglianze: Any = ((EAN_GESTIONALE, EAN_FORNITORI),)):
        return prepare_sources.build_matching(
            MASTER, FORNITORI, uguaglianze=[list(gruppo) for gruppo in uguaglianze],
        )

    def test_i_tre_fornitori_diventano_abbinamenti_per_codice_esatto(self) -> None:
        """È il punto di tutto: a valle non serve nessun caso speciale, perché a
        valle non si vede nessuna differenza."""

        matching, _, _ = self.esegui()

        self.assertEqual(stati(matching), {
            "cipresso": "EAN_ESATTO",
            "noce": "EAN_ESATTO",
            "betulla": "EAN_ESATTO",
            "larice": "EAN_ESATTO",
        })

    def test_la_riga_dichiara_da_dove_viene(self) -> None:
        """Chi guarda un ordine sbagliato deve poter risalire alla decisione
        umana che l'ha prodotto: senza, una riga entrata per una dichiarazione è
        indistinguibile da una trovata dal codice a barre."""

        matching, _, _ = self.esegui()
        noce = matching[0]["suppliers"]["noce"]

        self.assertEqual(noce["via_uguaglianza"], [EAN_FORNITORI])
        self.assertNotIn("via_uguaglianza", matching[0]["suppliers"]["cipresso"])

    def test_i_tre_escono_dalla_coda_dell_ai(self) -> None:
        """Non è solo un prodotto abbinato: sono tre domande in meno al modello,
        e tre rischi in meno di una risposta sbagliata."""

        _, coda_senza, _ = prepare_sources.build_matching(MASTER, FORNITORI)
        _, coda_con, _ = self.esegui()

        self.assertEqual(len(coda_senza), 3)
        self.assertEqual(coda_con, [])

    def test_il_guadagno_si_conta(self) -> None:
        """Un guadagno che non si conta non è una scelta più di uno scarto che
        non si conta: è il solo posto da cui si vede se quelle dichiarazioni
        stanno ancora servendo."""

        _, _, audit = self.esegui()

        self.assertEqual(audit["matched_by_declared_equivalence"], {"noce": 1, "betulla": 1, "larice": 1})
        self.assertEqual(audit["declared_equivalences"], 1)
        self.assertEqual(audit["exact_unique_usable"]["noce"], 1)

    def test_la_catena_di_dichiarazioni_arriva_fino_in_fondo(self) -> None:
        """A≡B e B≡C: cercando A si trova la riga che porta C."""

        fornitori = {"noce": [riga(4794, "999", "LUXA SAPONE EROGATORE ORIGINAL ML.250")]}
        matching, _, _ = prepare_sources.build_matching(
            MASTER, fornitori, uguaglianze=[[EAN_GESTIONALE, EAN_FORNITORI, "999"]],
        )

        self.assertEqual(stati(matching)["noce"], "EAN_ESATTO")

    def test_un_codice_scritto_in_un_altro_modo_si_ritrova_lo_stesso(self) -> None:
        """Il gestionale scrive gli EAN a mano e i fogli li rileggono come
        numeri: senza la riduzione a cifre, la dichiarazione di lunedì non
        varrebbe martedì e nessuno saprebbe perché."""

        fornitori = {"noce": [riga(4794, f" {EAN_FORNITORI} ", "LUXA ORIGINAL")]}
        matching, _, _ = prepare_sources.build_matching(
            MASTER, fornitori, uguaglianze=[[f"{EAN_GESTIONALE}.0", EAN_FORNITORI]],
        )

        self.assertEqual(stati(matching)["noce"], "EAN_ESATTO")

    def test_lo_stesso_codice_ripetuto_non_duplica_la_riga(self) -> None:
        """La riga deve restare una: contarla due volte la farebbe diventare
        `EAN_AMBIGUO`, cioè una domanda all'AI al posto di un abbinamento.

        ⚠ A garantirlo è `mappa_delle_uguaglianze`, che toglie i codici ripetuti
        e il codice del prodotto stesso — e una riga di listino ha un solo EAN.
        La guardia contro i doppioni che stava in `build_matching` è stata tolta
        proprio perché una controprova l'ha trovata **inerte**: questa prova
        controlla la proprietà, non la riga di codice che la produceva.
        """

        fornitori = {"noce": [riga(4794, EAN_FORNITORI, "LUXA ORIGINAL")]}
        matching, _, _ = prepare_sources.build_matching(
            MASTER, fornitori,
            uguaglianze=[
                [EAN_GESTIONALE, EAN_FORNITORI],
                [EAN_GESTIONALE, f" {EAN_FORNITORI} "],
                [EAN_FORNITORI, EAN_FORNITORI],
            ],
        )

        self.assertEqual(stati(matching)["noce"], "EAN_ESATTO")
        self.assertEqual(len(matching[0]["suppliers"]["noce"]["candidates"]), 1)
        atteso = {EAN_GESTIONALE: [EAN_FORNITORI], EAN_FORNITORI: [EAN_GESTIONALE]}
        # Il codice ripetuto dentro il gruppo, e la stessa coppia dichiarata due
        # volte in due gruppi: sono i due modi in cui un doppione può arrivare.
        self.assertEqual(
            prepare_sources.mappa_delle_uguaglianze([[EAN_GESTIONALE, EAN_FORNITORI, EAN_GESTIONALE]]),
            atteso,
        )
        self.assertEqual(
            prepare_sources.mappa_delle_uguaglianze(
                [[EAN_GESTIONALE, EAN_FORNITORI], [EAN_GESTIONALE, f" {EAN_FORNITORI}"]],
            ),
            atteso,
        )

    def test_due_righe_diverse_restano_una_domanda_da_fare(self) -> None:
        """Il ripiego giusto: se la dichiarazione porta due righe utilizzabili
        presso lo stesso fornitore, la catena non sceglie da sola — chiede."""

        fornitori = {"noce": [
            riga(4794, EAN_FORNITORI, "LUXA SAPONE EROGATORE ORIGINAL ML.250"),
            riga(4795, EAN_GESTIONALE, "LUXA SAPONE EROGATORE SETA ML.250"),
        ]}
        matching, coda, _ = prepare_sources.build_matching(
            MASTER, fornitori, uguaglianze=[[EAN_GESTIONALE, EAN_FORNITORI]],
        )

        self.assertEqual(stati(matching)["noce"], "EAN_AMBIGUO")
        self.assertEqual([voce["reason"] for voce in coda], ["EAN_AMBIGUO"])


class IlFileDelleUguaglianzeTests(unittest.TestCase):
    """Come la dichiarazione arriva dallo SQLite del servizio fino allo script."""

    def scrivi(self, contenuto: Any) -> Path:
        cartella = tempfile.TemporaryDirectory()
        self.addCleanup(cartella.cleanup)
        percorso = Path(cartella.name) / "uguaglianze.json"
        percorso.write_text(json.dumps(contenuto), encoding="utf-8")
        return percorso

    def test_non_chiederlo_e_legittimo(self) -> None:
        """Vuol dire che per questa run non ce ne sono, ed è il caso normale."""

        self.assertEqual(leggi_uguaglianze(None), [])

    def test_chiederlo_e_non_trovarlo_no(self) -> None:
        """Sono dichiarazioni umane: farle sparire in silenzio riporterebbe i
        prodotti abbinati a mano allo stato di prima senza che niente lo dica."""

        with self.assertRaises(ValueError) as errore:
            leggi_uguaglianze(Path("questo-file-non-esiste.json"))

        self.assertIn("abbinati a mano", str(errore.exception))

    def test_si_legge_con_la_chiave_e_senza(self) -> None:
        """Un file scritto a mano per una prova non deve sbagliare per una chiave."""

        con_chiave = self.scrivi({"classi": [[EAN_GESTIONALE, EAN_FORNITORI]]})
        senza = self.scrivi([[EAN_GESTIONALE, EAN_FORNITORI]])

        atteso = [[EAN_GESTIONALE, EAN_FORNITORI]]
        self.assertEqual(leggi_uguaglianze(con_chiave), atteso)
        self.assertEqual(leggi_uguaglianze(senza), atteso)

    def test_i_gruppi_di_uno_non_arrivano_alla_catena(self) -> None:
        percorso = self.scrivi({"classi": [[EAN_GESTIONALE], [EAN_GESTIONALE, EAN_FORNITORI]]})

        self.assertEqual(leggi_uguaglianze(percorso), [[EAN_GESTIONALE, EAN_FORNITORI]])


class LoScriptAccettaLIngressoTests(unittest.TestCase):
    """L'argomento esiste davvero sulla riga di comando, non solo nel codice."""

    def test_equivalenze_e_un_argomento_dichiarato(self) -> None:
        esito = subprocess.run(
            [sys.executable, str(SCRIPTS / "prepare_manifest_sources.py"), "--help"],
            capture_output=True, text=True, encoding="utf-8",
        )

        self.assertEqual(esito.returncode, 0, esito.stderr)
        self.assertIn("--equivalenze", esito.stdout)


if __name__ == "__main__":
    unittest.main()
