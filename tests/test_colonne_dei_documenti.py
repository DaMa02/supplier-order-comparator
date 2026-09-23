"""Quali colonne il programma legge in ogni documento, e chi puo' correggerle.

Due cose che prima non esistevano e che vanno insieme:

* `schema_mapping.mappatura_effettiva` dice, per un documento **riconosciuto**,
  quale colonna diventa il prezzo, quale l'EAN, quale i pezzi per collo. Prima
  l'assegnazione si poteva vedere solo quando il programma NON riconosceva il
  file, cioe' nel caso raro; nella settimana normale era invisibile;
* i tre lettori cablati (gestionale, BETULLA, Larice) accettano colonne diverse
  da quelle predefinite, cosi' una correzione fatta a mano puo' raggiungerli
  **senza cambiare lettore** — mandare Larice al lettore generico vuol dire
  perdere espositori e soglie con omaggio.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook


ROOT = Path(__file__).resolve().parents[1]
for cartella in (ROOT / "app", ROOT / "scripts"):
    if str(cartella) not in sys.path:
        sys.path.insert(0, str(cartella))

import prepare_sources  # noqa: E402
import schema_mapping  # noqa: E402
from inspect_sources import profile_file  # noqa: E402


def adattatori_veri() -> dict[str, dict]:
    return {
        str(voce.get("id")): voce
        for voce in schema_mapping.carica_adattatori(ROOT / "references" / "adapters.json")
    }


class MappaturaEffettivaTests(unittest.TestCase):
    """La tabella dice quello che il lettore fa, non quello che sembra."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.adattatori = adattatori_veri()

    def foglio(self, nome: str, righe: list[list[object]], foglio: str = "Sheet1") -> dict:
        percorso = self.root / nome
        libro = Workbook()
        pagina = libro.active
        pagina.title = foglio
        for riga in righe:
            pagina.append(riga)
        libro.save(percorso)
        libro.close()
        return profile_file(percorso)

    def profilo_betulla(self) -> dict:
        return self.foglio("betulla.xlsx", [
            ["EAN", "CodArt", "ORDINE", "Descr.Commerciale", "PzCt", "Cessione", "Pedana", "Iva"],
            ["8000000000001", "1300634", None, "VAPO Emanatore", 12, 3.98, 64, "Iva 22%"],
        ])

    def test_dice_da_quale_colonna_arriva_ogni_campo(self) -> None:
        profilo = self.profilo_betulla()
        decisione = {"role": "supplier", "adapter_id": "betulla_v1", "supplier_id": "betulla"}

        esito = schema_mapping.mappatura_effettiva(profilo, self.adattatori["betulla_v1"], decisione)
        per_campo = {voce["campo"]: voce for voce in esito["columns"]}

        # Le stesse posizioni che legge `read_betulla`: prezzo da row[5], cioe' F.
        self.assertEqual(per_campo["ean"]["lettera"], "A")
        self.assertEqual(per_campo["description"]["lettera"], "D")
        self.assertEqual(per_campo["pieces_per_carton"]["lettera"], "E")
        self.assertEqual(per_campo["unit_price_net"]["lettera"], "F")
        self.assertEqual(per_campo["unit_price_net"]["intestazione"], "Cessione")
        self.assertEqual(esito["orderColumn"]["lettera"], "C")
        self.assertEqual(esito["origin"], "registro")

    def test_porta_un_valore_vero_della_colonna_e_non_solo_l_intestazione(self) -> None:
        # Un'intestazione puo' mentire, un valore no: e' l'unico modo di
        # rispondere a «e' davvero questa la colonna del prezzo?».
        profilo = self.profilo_betulla()
        decisione = {"role": "supplier", "adapter_id": "betulla_v1"}

        esito = schema_mapping.mappatura_effettiva(profilo, self.adattatori["betulla_v1"], decisione)
        per_campo = {voce["campo"]: voce for voce in esito["columns"]}

        self.assertEqual(per_campo["unit_price_net"]["esempio"], "3.98")
        self.assertEqual(per_campo["ean"]["esempio"], "8000000000001")

    def test_le_colonne_escono_nell_ordine_del_documento(self) -> None:
        profilo = self.profilo_betulla()
        esito = schema_mapping.mappatura_effettiva(
            profilo, self.adattatori["betulla_v1"], {"role": "supplier", "adapter_id": "betulla_v1"}
        )

        indici = [voce["colonna"] for voce in esito["columns"]]
        self.assertEqual(indici, sorted(indici))

    def test_una_colonna_dichiarata_che_non_c_e_piu_si_dichiara(self) -> None:
        # E' il caso per cui la tabella esiste: il registro promette una colonna
        # e il documento non ce l'ha. Saltarla in silenzio sarebbe la malattia
        # che si voleva curare.
        profilo = self.foglio("betulla-senza-prezzo.xlsx", [
            ["EAN", "CodArt", "ORDINE", "Descr.Commerciale", "PzCt", "Pedana", "Iva"],
            ["8000000000001", "1300634", None, "VAPO Emanatore", 12, 64, 22],
        ])

        esito = schema_mapping.mappatura_effettiva(
            profilo, self.adattatori["betulla_v1"], {"role": "supplier", "adapter_id": "betulla_v1"}
        )
        per_campo = {voce["campo"]: voce for voce in esito["columns"]}

        self.assertFalse(per_campo["unit_price_net"]["trovata"])
        self.assertEqual(per_campo["unit_price_net"]["dichiarata"], "Cessione")
        self.assertIsNone(per_campo["unit_price_net"]["colonna"])

    def test_il_gestionale_dichiara_i_colli_che_la_proposta_automatica_perde(self) -> None:
        # `suggerisci_colonne` si ferma perche' «Colli» e «Quantita» sono due
        # alias dello stesso campo e la proposta rifiuta l'ambiguita': va bene
        # per proporre, non per raccontare quello che il lettore ha gia' fatto.
        profilo = self.foglio("gestionale.xlsx", [
            ["T", "OF", "33", "del", "14/08/2026"],
            [None, "Codice", "Cod. Int.", "Descrizione", "UM", "Colli", "Quantità", "Prezzo", "Sconto", "IVA"],
            ["C", "8009408415265", "", "GILLARDO VENEX", "PZ", 2, "10,000", "7,430", "", 22],
        ], foglio="Foglio1")
        decisione = {"role": "master", "adapter_id": "gestionale_v1"}

        esito = schema_mapping.mappatura_effettiva(profilo, self.adattatori["gestionale_v1"], decisione)
        per_campo = {voce["campo"]: voce for voce in esito["columns"]}

        self.assertEqual(per_campo["suggested_colli"]["lettera"], "F")
        self.assertEqual(per_campo["last_unit_price"]["lettera"], "H")
        self.assertEqual(per_campo["ean"]["lettera"], "B")
        self.assertIsNone(esito["orderColumn"])

    def test_una_mappatura_confermata_vince_sul_registro_e_lo_dichiara(self) -> None:
        profilo = self.profilo_betulla()
        decisione = {
            "role": "supplier",
            "adapter_id": "betulla_v1",
            "field_mapping": {"sheet": "Sheet1", "header_row": 1, "data_start_row": 2,
                              "columns": {"unit_price_net": "G"}},
            "user_confirmation": {"required": True, "status": "CONFIRMED"},
        }

        esito = schema_mapping.mappatura_effettiva(profilo, self.adattatori["betulla_v1"], decisione)
        per_campo = {voce["campo"]: voce for voce in esito["columns"]}

        self.assertEqual(per_campo["unit_price_net"]["lettera"], "G")
        self.assertEqual(esito["origin"], "confermata")

    def test_un_documento_che_nessun_adattatore_descrive_non_solleva(self) -> None:
        profilo = self.foglio("ignoto.xlsx", [["A", "B"], [1, 2]])

        esito = schema_mapping.mappatura_effettiva(profilo, None, {})

        self.assertEqual(esito["columns"], [])
        self.assertFalse(esito["recognised"])
        self.assertEqual(esito["origin"], "sconosciuta")


class ColonneCorretteAiLettoriCablatiTests(unittest.TestCase):
    """Una correzione delle colonne raggiunge i lettori senza cambiarli."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def salva(self, nome: str, righe: list[list[object]], foglio: str = "Sheet1") -> Path:
        percorso = self.root / nome
        libro = Workbook()
        pagina = libro.active
        pagina.title = foglio
        for riga in righe:
            pagina.append(riga)
        libro.save(percorso)
        libro.close()
        return percorso

    def test_senza_correzioni_si_leggono_le_colonne_di_sempre(self) -> None:
        risultato = prepare_sources.colonne_con_predefinite(
            prepare_sources.COLONNE_PREDEFINITE_BETULLA, None
        )
        self.assertEqual(risultato, prepare_sources.COLONNE_PREDEFINITE_BETULLA)

    def test_una_correzione_sostituisce_solo_il_campo_indicato(self) -> None:
        risultato = prepare_sources.colonne_con_predefinite(
            prepare_sources.COLONNE_PREDEFINITE_BETULLA, {"unit_price_net": "G"}
        )

        self.assertEqual(risultato["unit_price_net"], 7)
        self.assertEqual(risultato["ean"], prepare_sources.COLONNE_PREDEFINITE_BETULLA["ean"])

    def test_una_colonna_indicata_male_ferma_invece_di_leggere_quella_di_prima(self) -> None:
        # Ignorarla vorrebbe dire tornare in silenzio alla predefinita: chi ha
        # corretto crederebbe di aver corretto, e i prezzi resterebbero quelli.
        with self.assertRaises(ValueError):
            prepare_sources.colonne_con_predefinite(
                prepare_sources.COLONNE_PREDEFINITE_BETULLA, {"unit_price_net": "non una colonna"}
            )

    def test_betulla_legge_il_prezzo_dalla_colonna_corretta(self) -> None:
        percorso = self.salva("betulla.xlsx", [
            ["EAN", "CodArt", "ORDINE", "Descr.Commerciale", "PzCt", "Cessione", "Pedana", "Iva"],
            ["8000000000001", "1300634", None, "VAPO Emanatore", 12, 3.98, 9.99, 22],
        ])

        di_serie = prepare_sources.read_betulla(percorso)
        corretto = prepare_sources.read_betulla(percorso, colonne={"unit_price_net": "G"})

        self.assertEqual(di_serie[0]["unit_price_net"], "3.9800")
        self.assertEqual(corretto[0]["unit_price_net"], "9.9900")
        # Il resto del record non cambia: si e' corretta una colonna, non il lettore.
        self.assertEqual(corretto[0]["ean"], di_serie[0]["ean"])
        self.assertEqual(corretto[0]["description"], di_serie[0]["description"])

    def test_la_colonna_d_ordine_di_betulla_si_puo_correggere(self) -> None:
        percorso = self.salva("betulla.xlsx", [
            ["EAN", "CodArt", "ORDINE", "Descr.Commerciale", "PzCt", "Cessione", "Pedana", "Iva"],
            ["8000000000001", "1300634", None, "VAPO Emanatore", 12, 3.98, 64, 22],
        ])

        self.assertEqual(prepare_sources.read_betulla(percorso)[0]["order_column"], "C")
        self.assertEqual(
            prepare_sources.read_betulla(percorso, order_column="J")[0]["order_column"], "J"
        )

    def test_il_gestionale_legge_i_colli_dalla_colonna_corretta(self) -> None:
        percorso = self.salva("gestionale.xlsx", [
            ["T", "OF", "33"],
            [None, "Codice", "Cod. Int.", "Descrizione", "UM", "Colli", "Quantità", "Prezzo", "Sconto", "IVA"],
            ["C", "8009408415265", "", "GILLARDO VENEX", "PZ", 2, "10,000", "7,430", "", 22],
        ], foglio="Foglio1")

        di_serie = prepare_sources.read_gestionale(percorso)
        corretto = prepare_sources.read_gestionale(percorso, colonne={"suggested_colli": "G"})

        self.assertEqual(di_serie[0]["suggested_colli"], 2)
        self.assertEqual(corretto[0]["suggested_colli"], "10,000")
        self.assertEqual(corretto[0]["ean"], "8009408415265")


class ColonneDelManifestTests(unittest.TestCase):
    """Solo la decisione corregge: la field_mapping del registro non è una correzione."""

    def test_la_field_mapping_dell_adattatore_non_arriva_al_lettore_cablato(self) -> None:
        from prepare_manifest_sources import colonne_corrette

        adattatore = {"field_mapping": {"columns": {"unit_price_net": "Z"}}}
        self.assertEqual(colonne_corrette({}, adattatore, "betulla_v1"), {})

    def test_la_mappatura_confermata_arriva_al_lettore_cablato(self) -> None:
        from prepare_manifest_sources import colonne_corrette

        decisione = {"field_mapping": {"columns": {"unit_price_net": "G"}, "order_column": "J"}}
        esito = colonne_corrette(decisione, {}, "betulla_v1")

        self.assertEqual(esito["colonne"], {"unit_price_net": "G"})
        self.assertEqual(esito["order_column"], "J")

    def test_il_csv_di_noce_indica_le_colonne_per_nome_e_resta_fuori(self) -> None:
        from prepare_manifest_sources import colonne_corrette

        decisione = {"field_mapping": {"columns": {"unit_price_net": "G"}}}
        self.assertEqual(colonne_corrette(decisione, {}, "noce_csv_v1"), {})

    def test_il_gestionale_non_riceve_una_colonna_d_ordine(self) -> None:
        # Il gestionale non si compila: passargli `order_column` sarebbe un
        # argomento che quel lettore non ha.
        from prepare_manifest_sources import colonne_corrette

        decisione = {"field_mapping": {"columns": {"ean": "C"}, "order_column": "J"}}
        esito = colonne_corrette(decisione, {}, "gestionale_v1")

        self.assertEqual(esito, {"colonne": {"ean": "C"}})


if __name__ == "__main__":
    unittest.main()
