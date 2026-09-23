#!/usr/bin/env python3
"""Le condizioni commerciali si dichiarano dalla mappatura, non a mano.

⚠ Il buco misurato su QUERCIA il 17 agosto 2026: **oggi un fornitore nuovo si
legge e si compila, le sue offerte no.** `commercial_conditions` si scrive a
mano dentro `references/adapters.json` e ce l'ha solo LARICE; la mappatura
guidata non aveva un campo per dichiararla (`CAMPI_FORNITORE`) e
`impara_adattatore` non la scriveva. QUERCIA ha quindici testi promozionali — sei
in colonna A, nove in colonna Q — e nessuno sarebbe mai diventato una regola.

Qui si prova la catena intera lato servizio: la mappatura la accetta, il
registro la impara, il motore delle offerte la ritrova. La pagina che la fa
scegliere è di un'altra fase: qui si costruiscono i dati che mostrerà.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

RADICE = Path(__file__).resolve().parents[1]
for cartella in (RADICE / "app", RADICE / "scripts"):
    if str(cartella) not in sys.path:
        sys.path.insert(0, str(cartella))

import impara_adattatore  # noqa: E402
import schema_mapping  # noqa: E402
from promotion_bridge import LAYOUT_BLOCCHI, LAYOUT_RIGA, colonne_richieste_dal_layout  # noqa: E402


# Un listino con la forma di QUERCIA: le offerte scritte nella colonna del codice
# articolo, sopra l'elenco dei prodotti.
INTESTAZIONI = ["Articolo", "EAN", "Descrizione Prodotto", "Imballo", "Prezzo Listino", "Premio", "Codice riga"]
RIGHE = [
    {"row": 1, "values": INTESTAZIONI},
    {"row": 2, "values": ["OGNI 1500 EURO OMAGGIO 1 CARTONE", None, None, None, None, None, None]},
    {"row": 3, "values": ["FAT7033", "8009373258508", "AXO LAVANDA", 4, 4.58, None, "TP"]},
    {"row": 4, "values": ["FAT7034", "8009583384318", "AXO CLASSICA", 4, 4.58, None, "TP"]},
]


def profilo_finto() -> dict[str, Any]:
    return {
        "profile_id": "p1",
        "file_name": "LISTINO QUERCIA.xlsx",
        "content_format": "xlsx",
        "declared_suffix": ".xlsx",
        "sha256": "a" * 64,
        "details": {
            "format": "xlsx",
            "sheet_count": 1,
            "sheets": [{
                "name": "Sheet",
                "active_range": {"min_row": 1, "max_row": 4, "min_column": 1, "max_column": 7,
                                 "nonempty_rows": 4},
                "header_candidates": [{"row": 1, "score": 10, "values": INTESTAZIONI}],
                "header_rows": RIGHE,
                "section_breaks": [],
                "section_rows": [],
                "samples": {"initial": RIGHE, "middle": [], "final": []},
                "columns": [],
            }],
        },
    }


def scelta(**cambiamenti: Any) -> dict[str, Any]:
    grezza: dict[str, Any] = {
        "role": "supplier",
        "sheet": "Sheet",
        "headerRow": 1,
        "dataStartRow": 3,
        # Un fornitore nuovo, che è il caso di cui parla tutto questo file:
        # si dichiara col nome, non scegliendolo da un registro dove non c'è.
        "supplierName": "QUERCIA",
        "orderColumn": 8,
        "columns": {
            "supplier_code": 1,
            "ean": 2,
            "description": 3,
            "pieces_per_carton": 4,
            "unit_price_net": 5,
        },
    }
    grezza.update(cambiamenti)
    return grezza


class LaMappaturaSaDichiarareLeOfferteTests(unittest.TestCase):
    def decisione(self, **cambiamenti: Any) -> dict[str, Any]:
        return schema_mapping.decisione_da_mappatura(profilo_finto(), scelta(**cambiamenti), [])

    def test_senza_dichiarazione_non_nasce_niente(self) -> None:
        """Il silenzio resta silenzio: nessuna forma inventata per nessuno."""

        decisione = self.decisione()

        self.assertNotIn("commercial_conditions", decisione["field_mapping"])

    def test_la_colonna_delle_offerte_diventa_una_dichiarazione(self) -> None:
        decisione = self.decisione(
            columns={**scelta()["columns"], "promotion_text": 1},
            commercialConditions={},
        )

        condizioni = decisione["field_mapping"]["commercial_conditions"]
        self.assertEqual(condizioni["layout"], LAYOUT_RIGA)
        self.assertEqual(condizioni["fields"]["text"], "promotion_text")
        self.assertEqual(condizioni["sheet"], "Sheet")
        # ⚠ Dalla prima riga: su QUERCIA le condizioni stanno **sopra** l'elenco
        # dei prodotti, e partire dalla prima riga dei dati le taglierebbe via.
        self.assertEqual(condizioni["data_start_row"], 1)

    def test_la_colonna_delle_offerte_puo_stare_sopra_a_un_altra(self) -> None:
        """⚠ Su QUERCIA i testi stanno nella colonna del codice articolo, su BETULLA
        dentro la descrizione. La regola «una colonna, un campo» qui non vale:
        non è una seconda lettura, è un'altra domanda sullo stesso testo."""

        decisione = self.decisione(
            columns={**scelta()["columns"], "promotion_text": 1},
            commercialConditions={"layout": LAYOUT_RIGA},
        )

        self.assertEqual(decisione["field_mapping"]["columns"]["promotion_text"], "Articolo")
        self.assertEqual(decisione["field_mapping"]["columns"]["supplier_code"], "Articolo")

    def test_due_campi_veri_sulla_stessa_colonna_restano_un_errore(self) -> None:
        with self.assertRaises(ValueError) as errore:
            self.decisione(columns={**scelta()["columns"], "vat": 5})

        self.assertIn("assegnata sia a", str(errore.exception))

    def test_dichiarare_la_forma_senza_la_colonna_e_un_errore(self) -> None:
        """Una casella spuntata a vuoto non è una dichiarazione."""

        with self.assertRaises(ValueError) as errore:
            self.decisione(commercialConditions={"layout": LAYOUT_RIGA})

        self.assertIn("serve la colonna in cui le scrive", str(errore.exception))

    def test_una_forma_sconosciuta_si_rifiuta(self) -> None:
        with self.assertRaises(ValueError) as errore:
            self.decisione(
                columns={**scelta()["columns"], "promotion_text": 1},
                commercialConditions={"layout": "a fantasia"},
            )

        self.assertIn("non è un modo di scrivere le condizioni", str(errore.exception))

    def test_i_blocchi_pretendono_le_loro_colonne(self) -> None:
        """⚠ Fallisce **adesso**, non ogni settimana.

        Accettare `blocchi` senza il premio, il codice a barre e i codici di
        riga scriverebbe nel registro una dichiarazione che poi produce «non so
        più dove il listino tiene …» a ogni lettura, per sempre.
        """

        with self.assertRaises(ValueError) as errore:
            self.decisione(
                columns={**scelta()["columns"], "promotion_text": 1},
                commercialConditions={"layout": LAYOUT_BLOCCHI},
            )

        self.assertIn("il nome dell'articolo in omaggio", str(errore.exception))
        self.assertIn("i codici delle righe", str(errore.exception))

    def test_i_blocchi_con_le_loro_colonne_si_dichiarano(self) -> None:
        decisione = self.decisione(
            columns={**scelta()["columns"], "promotion_text": 3, "reward_description": 6, "discount": 7},
            commercialConditions={"layout": LAYOUT_BLOCCHI},
        )

        campi = decisione["field_mapping"]["commercial_conditions"]["fields"]
        self.assertEqual(
            set(campi),
            set(colonne_richieste_dal_layout(LAYOUT_BLOCCHI)),
        )
        self.assertEqual(campi["reward"], "reward_description")
        self.assertEqual(campi["row_code"], "discount")


class IlRegistroLaImparaTests(unittest.TestCase):
    """Senza questo passaggio la dichiarazione muore nella decisione della run."""

    def setUp(self) -> None:
        temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(temporanea.cleanup)
        self.radice = Path(temporanea.name)

    def adattatore(self, **cambiamenti: Any) -> dict[str, Any]:
        decisione = schema_mapping.decisione_da_mappatura(profilo_finto(), scelta(**cambiamenti), [])
        mappatura = decisione["field_mapping"]
        return impara_adattatore.voce_da_scrivere(
            {"file_name": "LISTINO QUERCIA.xlsx", "sha256": "a" * 64, "declared_suffix": ".xlsx"},
            decisione,
            mappatura,
            {},
            "quercia_v1",
            {"name": "Sheet"},
            1,
            ["articolo", "ean"],
            [schema_mapping.normalizza(voce) for voce in INTESTAZIONI],
            "2026-08-17T10:00:00+00:00",
            posizioni={schema_mapping.normalizza(nome): indice
                       for indice, nome in enumerate(INTESTAZIONI, start=1)},
        )

    def test_la_dichiarazione_entra_nell_adattatore(self) -> None:
        voce = self.adattatore(
            columns={**scelta()["columns"], "promotion_text": 1},
            commercialConditions={"layout": LAYOUT_RIGA},
        )

        self.assertEqual(voce["commercial_conditions"]["fields"]["text"], "promotion_text")
        # ⚠ E la colonna che quel nome indica sta in `column_map`: è l'unica
        # forma che `promotion_bridge._dove_sta_la_colonna` sa risolvere, la
        # stessa che segue il lettore dei prezzi. Senza, la dichiarazione
        # nominerebbe una colonna che nessuno sa trovare.
        self.assertEqual(voce["column_map"]["promotion_text"], "A")

    def test_senza_dichiarazione_l_adattatore_non_ne_inventa_una(self) -> None:
        voce = self.adattatore()

        self.assertNotIn("commercial_conditions", voce)

    def test_una_variazione_di_schema_non_cancella_le_offerte_gia_note(self) -> None:
        """Cambiare dove stanno le colonne non vuol dire smettere di fare offerte."""

        prima = {"commercial_conditions": {"layout": LAYOUT_RIGA, "fields": {"text": "description"}}}
        decisione = schema_mapping.decisione_da_mappatura(profilo_finto(), scelta(), [])
        voce = impara_adattatore.voce_da_scrivere(
            {"file_name": "LISTINO QUERCIA.xlsx", "sha256": "a" * 64, "declared_suffix": ".xlsx"},
            decisione,
            decisione["field_mapping"],
            prima,
            "quercia_v1",
            {"name": "Sheet"},
            1,
            ["articolo"],
            [schema_mapping.normalizza(voce) for voce in INTESTAZIONI],
            "2026-08-17T10:00:00+00:00",
            posizioni={schema_mapping.normalizza(nome): indice
                       for indice, nome in enumerate(INTESTAZIONI, start=1)},
        )

        self.assertEqual(voce["commercial_conditions"], prima["commercial_conditions"])


class IlMotoreDelleOfferteLaRitrovaTests(unittest.TestCase):
    """La prova che chiude la catena: dichiarato dalla pagina, letto dal motore.

    Se `_colonne_delle_condizioni` non risolve i nomi che la mappatura ha
    scritto, la dichiarazione è una decorazione: il registro la porta e ogni
    lettura risponde «non so più dove il listino tiene le descrizioni».
    """

    def test_le_colonne_dichiarate_si_risolvono(self) -> None:
        from promotion_bridge import _colonne_delle_condizioni

        decisione = schema_mapping.decisione_da_mappatura(
            profilo_finto(),
            scelta(
                columns={**scelta()["columns"], "promotion_text": 1},
                commercialConditions={"layout": LAYOUT_RIGA},
            ),
            [],
        )
        voce = impara_adattatore.voce_da_scrivere(
            {"file_name": "LISTINO QUERCIA.xlsx", "sha256": "a" * 64, "declared_suffix": ".xlsx"},
            decisione,
            decisione["field_mapping"],
            {},
            "quercia_v1",
            {"name": "Sheet"},
            1,
            ["articolo"],
            [schema_mapping.normalizza(nome) for nome in INTESTAZIONI],
            "2026-08-17T10:00:00+00:00",
            posizioni={schema_mapping.normalizza(nome): indice
                       for indice, nome in enumerate(INTESTAZIONI, start=1)},
        )

        forma, colonne, mancanti = _colonne_delle_condizioni(voce, voce["commercial_conditions"])

        self.assertEqual(mancanti, [])
        self.assertEqual(forma, LAYOUT_RIGA)
        self.assertEqual(colonne["text"], 1)


class LaPaginaSaDireDaDoveVengonoLeOfferteTests(unittest.TestCase):
    """`mappatura_effettiva` porta la dichiarazione, per chi disegnerà la pagina.

    ⚠ `None` non è un vuoto: è la differenza fra «questo fornitore non fa
    offerte» e «le fa e non gliele stiamo leggendo», che oggi è il caso di
    tutti tranne LARICE.
    """

    def test_un_fornitore_senza_dichiarazione_lo_dice(self) -> None:
        effettiva = schema_mapping.mappatura_effettiva(profilo_finto(), {}, None)

        self.assertIsNone(effettiva["commercialConditions"])

    def test_un_fornitore_con_la_dichiarazione_dice_anche_dove(self) -> None:
        decisione = schema_mapping.decisione_da_mappatura(
            profilo_finto(),
            scelta(
                columns={**scelta()["columns"], "promotion_text": 1},
                commercialConditions={"layout": LAYOUT_RIGA},
            ),
            [],
        )

        effettiva = schema_mapping.mappatura_effettiva(profilo_finto(), {}, decisione)

        condizioni = effettiva["commercialConditions"]
        self.assertEqual(condizioni["layout"], LAYOUT_RIGA)
        self.assertEqual(condizioni["fields"]["text"]["lettera"], "A")


class LeCondizioniDiLariceRestanoQuelleTests(unittest.TestCase):
    """Non regressione sul solo fornitore che le ha davvero, letto dal registro."""

    def test_il_registro_vero_dichiara_ancora_i_blocchi_di_larice(self) -> None:
        registro = json.loads((RADICE / "references" / "adapters.json").read_text(encoding="utf-8"))
        larice = next(voce for voce in registro["adapters"] if voce["id"] == "larice_v1")

        condizioni = larice["commercial_conditions"]

        self.assertEqual(condizioni["layout"], LAYOUT_BLOCCHI)
        self.assertEqual(set(condizioni["fields"]), set(colonne_richieste_dal_layout(LAYOUT_BLOCCHI)))
        # E ogni nome che dichiara è una colonna che il registro sa trovare.
        for campo in condizioni["fields"].values():
            self.assertIn(campo, larice["column_map"], campo)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
