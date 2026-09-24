"""Schema memory: fingerprints, recognition, versioned writes.

The engine runs unattended, so when a supplier's layout changes the rule must
live in the adapter registry, not in a function's if-branches. These tests
check that recognition comes from the registry and that an unrecognized file
is reported as such instead of silently accepted.

Tests against real price lists are read-only: profiling never rewrites them.
Tests that write work on a copy of the registry in a temp directory.
"""

from __future__ import annotations

import csv
import io
import itertools
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
APP = SKILL_ROOT / "app"
# These tests use a frozen copy of the registry, not `references/adapters.json`:
# the engine rewrites the live one whenever it learns a schema, and pinning
# tests to its exact values would make them fail on correct behavior. What's
# tested here is a property of the shipped registry — that the native
# adapters recognize the historical price lists — so it runs against that.
ADAPTERS = SKILL_ROOT / "tests" / "fixtures" / "adapters_nativi.json"
ADAPTERS_CONSEGNATO = SKILL_ROOT / "references" / "adapters.json"
for cartella in (SCRIPTS, APP):
    if str(cartella) not in sys.path:
        sys.path.insert(0, str(cartella))

import inspect_sources  # noqa: E402
import registro  # noqa: E402
import prepare_sources  # noqa: E402


_REGISTRO_VERO = registro.REGISTRO

# The dedicated readers file, to check that a supplier's headers aren't
# hardcoded in it: the rule belongs to the registry.
PERCORSO_LETTORI = SCRIPTS / "prepare_sources.py"


def setUpModule() -> None:
    """Point calls without an explicit path at the frozen copy too.

    `registro.riconosci(profilo)` with no path argument reads the default
    registry, so this also has to be repointed, or those calls would read the
    live registry while everything else reads the frozen copy.
    """

    registro.REGISTRO = ADAPTERS


def tearDownModule() -> None:
    registro.REGISTRO = _REGISTRO_VERO


# Real price lists live outside the project; point at another folder with
# this environment variable without touching the test code.
LISTINI = Path(os.environ.get("LISTINI_STORICI", str(SKILL_ROOT / "listini-storici")))

# Noce price-list headers, in the order they appear in the real file: column
# A is empty, the EAN starts at B.
INTESTAZIONI_NOCE = [
    None, "codice_a_barre", "codice", "descrizione_articolo", "pezzi_x_cartone",
    "cartoni_x_stra", "strati_x_pal", "prezzo", "quantita", "offerta",
    "Importo", "descrizione_reparto", "cat", "ragione_sociale", "variato",
    "descrizione_offerta", "Iva",
]

INTESTAZIONI_BETULLA = ["EAN", "CodArt", "ORDINE", "Descr.Commerciale", "PzCt",
                      "Cessione", "Pedana", "Iva", "TOTALI"]

# The adapters hand-written into the project. Not the full registry content:
# the registry can (and should be able to) hold more, since learning a new
# adapter is the point of it. A test pinning the registry's exact contents
# would fail the moment the engine does its job.
ADATTATORI_NATIVI = ("gestionale_v1", "betulla_v1", "cipresso_v1", "larice_v1",
                     "noce_xls_v1", "noce_csv_v1", "offerte_v1")

_PROFILI: dict[Path, dict[str, Any]] = {}


def profilo_del_file(percorso: Path) -> dict[str, Any]:
    """Return the inspector's `details` for a file, computed once and cached.

    This is what `riconosci` receives in production; building one by hand
    would decouple the test from the real profile, making it pointless.
    """

    chiave = percorso.resolve()
    if chiave not in _PROFILI:
        _PROFILI[chiave] = inspect_sources.profile_file(percorso)["details"]
    return _PROFILI[chiave]


def scrivi_foglio(percorso: Path, righe: list[list[Any]], nome: str = "Foglio1") -> Path:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = nome
    for riga in righe:
        sheet.append(riga)
    workbook.save(percorso)
    workbook.close()
    return percorso


def foglio_noce(percorso: Path, *, intestazioni: list[Any] | None = None,
                    riga_intestazione: int = 5, prezzi_testuali: bool = False,
                    nome_foglio: str = "Foglio1", righe_dati: int = 3) -> Path:
    """Build a sheet shaped like the Noce price list: a note on top, data below."""

    intestazioni = list(intestazioni if intestazioni is not None else INTESTAZIONI_NOCE)
    righe: list[list[Any]] = [[None] for _ in range(riga_intestazione - 1)]
    if riga_intestazione >= 2:
        righe[-1] = [None, None, None, "i prezzi offerta sono in grassetto"]
    righe.append(intestazioni)
    for numero in range(righe_dati):
        prezzo: Any = f"1,2{numero}" if prezzi_testuali else 1.20 + numero
        righe.append([
            None, f"800000000000{numero}", f"C{numero}", f"PRODOTTO {numero}", 6,
            10, 4, prezzo, 0, "NO", 0.0, "REPARTO", "NO FOOD", "FORNITORE", "",
            "", 22,
        ])
    return scrivi_foglio(percorso, righe, nome_foglio)


def foglio_larice(percorso: Path, nome: str = "Canvass 99 01-05set", righe_dati: int = 120) -> Path:
    """Build a sheet shaped like the Larice price list: no header row, 18 columns."""

    righe = []
    for numero in range(righe_dati):
        riga: list[Any] = [None] * 18
        riga[1] = "I"
        riga[2] = f"C{numero:05d}"
        riga[6] = f"BAGNO VIDOR 500 ML GUSTO {numero}"
        riga[14] = 2.50 + numero / 100
        riga[15] = 0.05
        riga[17] = f"80000000{numero:05d}"
        righe.append(riga)
    return scrivi_foglio(percorso, righe, nome)


def scrivi_valori_calcolati(percorso: Path, valori: dict[str, Any]) -> Path:
    """Add pre-computed formula results to an .xlsx file.

    A workbook saved by openpyxl carries only the formula: opening it with
    `data_only=True` yields `None`, the shape of a file Excel has never
    opened. A real price list arrives already computed, with both the
    formula and its cached value (`<f>` and `<v>` together) — that's the
    shape the reader works on, and the one these tests need to reproduce.
    """

    import re as _re
    import zipfile

    grezzo = percorso.read_bytes()
    with zipfile.ZipFile(io.BytesIO(grezzo)) as archivio:
        contenuti = {nome: archivio.read(nome) for nome in archivio.namelist()}
    nome_foglio = next(nome for nome in contenuti if nome.startswith("xl/worksheets/sheet"))
    xml = contenuti[nome_foglio].decode("utf-8")

    def con_valore(trovato: _re.Match[str]) -> str:
        cella, formula = trovato.group(1), trovato.group(2)
        riferimento = _re.search(r'r="([A-Z]+[0-9]+)"', cella)
        if riferimento is None or riferimento.group(1) not in valori:
            return trovato.group(0)
        valore = valori[riferimento.group(1)]
        if isinstance(valore, str):
            # `t="str"` is how Excel marks a formula's result as text; without
            # it the value would be read back as a number.
            cella = cella.replace(">", ' t="str">', 1) if 't="' not in cella else cella
            return f"{cella}<f>{formula}</f><v>{valore}</v></c>"
        return f"{cella}<f>{formula}</f><v>{valore}</v></c>"

    # openpyxl writes the value cell empty, since nothing has computed it yet
    # — but in one of two forms depending on whether lxml is installed:
    #
    #     with lxml      <c r="E2"><f>…</f><v></v></c>
    #     without lxml   <c r="E2"><f>…</f><v /></c>
    #
    # Both forms come from the same openpyxl version; matching only the first
    # one silently breaks on a Python install with plain openpyxl and no lxml.
    xml = _re.sub(r"(<c [^>]*>)<f>(.*?)</f>(?:<v\s*/>|<v>[^<]*</v>)?</c>", con_valore, xml)
    contenuti[nome_foglio] = xml.encode("utf-8")
    with zipfile.ZipFile(percorso, "w", zipfile.ZIP_DEFLATED) as archivio:
        for nome, dati in contenuti.items():
            archivio.writestr(nome, dati)
    return percorso


def registro_di_prova(percorso: Path, voci: list[dict[str, Any]]) -> Path:
    percorso.write_bytes(
        json.dumps({"schema_version": 1, "adapters": voci}, ensure_ascii=False, indent=2).encode("utf-8")
    )
    return percorso


class ImprontaTests(unittest.TestCase):
    """A schema's fingerprint: what it's made of, and what leaves it unchanged."""

    maxDiff = None

    def test_normalizza_da_lo_stesso_risultato_dell_inspector(self) -> None:
        """`inspect_sources.normalized` must be safely replaceable by this one.

        If the two diverged, a price list the inspector recognizes would no
        longer be found in the registry, silently.
        """

        for valore in ["Cod.Art.", "COD ART", "Descrizione Articolo", "Quantità",
                       "PERCHÉ", "Pz/Ct", "  ", "", None, 0, False, 12, "codice_a_barre",
                       "TOTALE", "n°", "caffè", "ß"]:
            with self.subTest(valore=valore):
                self.assertEqual(registro.normalizza(valore), inspect_sources.normalized(valore))

    def test_le_accentate_perdono_i_segni_e_non_il_resto(self) -> None:
        self.assertEqual(registro.normalizza("Quantità"), "quantita")
        self.assertEqual(registro.normalizza("PERCHÉ"), "perche")

    def test_l_impronta_non_ha_vuoti_ne_doppioni_ed_e_ordinata(self) -> None:
        """An empty cell in the header row is not a header.

        The Noce price list has an empty column A in row 5; if the empty
        value entered the fingerprint, the observed set would never match
        the declared one.
        """

        token = registro.impronta_intestazioni([None, "EAN", "  ", "ean", "CodArt", "", "Cod. Art."])

        self.assertEqual(token, ["codart", "ean"])

    def test_l_ordine_delle_colonne_non_cambia_l_impronta(self) -> None:
        """A supplier reordering columns sends what's still the same price list."""

        prima = registro.impronta("Foglio1", 1, 2, ["EAN", "CodArt", "Cessione"])
        dopo = registro.impronta("Foglio1", 1, 2, ["Cessione", "EAN", "CodArt"])

        self.assertEqual(prima["hash"], dopo["hash"])

    def test_l_hash_e_lo_sha256_del_json_canonico(self) -> None:
        import hashlib

        risultato = registro.impronta("Foglio1", 5, 6, ["Prezzo", "EAN"])

        atteso = hashlib.sha256(json.dumps({
            "sheet": "Foglio1", "header_row": 5, "data_start_row": 6,
            "headers": ["ean", "prezzo"],
        }, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
        self.assertEqual(risultato["hash"], atteso)
        self.assertEqual(len(risultato["hash"]), 64)

    def test_una_colonna_in_piu_cambia_l_hash_ma_non_l_identita(self) -> None:
        """This is why identity isn't the hash: that would false-alarm weekly."""

        prima = registro.impronta("Foglio1", 1, 2, ["EAN", "CodArt"])
        dopo = registro.impronta("Foglio1", 1, 2, ["EAN", "CodArt", "NOTE"])

        self.assertNotEqual(prima["hash"], dopo["hash"])
        self.assertTrue(set(prima["headers"]) <= set(dopo["headers"]))


class RegistroLetturaTests(unittest.TestCase):
    """Reading the registry, and reporting when that isn't possible."""

    maxDiff = None

    def setUp(self) -> None:
        self.radice = Path(tempfile.mkdtemp(prefix="collaudo_impronte_"))
        self.addCleanup(shutil.rmtree, self.radice, True)

    def test_gli_adattatori_dichiarano_tutti_un_impronta(self) -> None:
        """An adapter without a fingerprint would never be recognized.

        This checks properties of the registry, not its exact contents:
        pinning it to an exact set of entries would fail the moment the
        engine learns a new adapter.
        """

        # Unlike the tests against historical price lists, this reads the
        # live registry: these properties must hold even after the engine
        # has learned a schema, and this is the only place that would notice
        # if they didn't.
        voci = registro.adattatori(ADAPTERS_CONSEGNATO)
        identificativi = [voce.get("id") for voce in voci]

        for nativo in ADATTATORI_NATIVI:
            self.assertIn(nativo, identificativi, "un adattatore nativo è sparito dal registro")
        self.assertEqual(len(identificativi), len(set(identificativi)),
                         "due voci con lo stesso identificativo si sovrascriverebbero a vicenda")
        for voce in voci:
            with self.subTest(adattatore=voce["id"]):
                self.assertTrue(str(voce.get("id") or "").strip(), "una voce senza «id» non si cerca")
                self.assertIn(voce.get("kind"), ("master", "supplier"))
                self.assertIsInstance(voce.get("schema_version"), int)
                self.assertGreaterEqual(voce.get("schema_version"), 1)
                self.assertTrue(voce.get("header_signature") or voce.get("column_shape_signature"))
                if voce.get("kind") == "supplier":
                    self.assertTrue(str(voce.get("supplier_id") or "").strip(),
                                    "un adattatore fornitore senza «supplier_id» non lo cercherebbe nessuno")

    def test_chi_sapeva_compilare_un_ordine_continua_a_saperlo(self) -> None:
        """Learning a schema must not drop `order_write` for a supplier.

        Compared against the frozen copy, i.e. what the engine could do when
        shipped: a supplier able to write its order file must not regress.
        """

        def sa_compilare(percorso) -> set[str]:
            return {
                str(voce.get("supplier_id") or "").strip().casefold()
                for voce in registro.adattatori(percorso)
                if registro.scrittura_ordine(voce)
            }

        perduti = sorted(sa_compilare(ADAPTERS) - sa_compilare(ADAPTERS_CONSEGNATO))

        self.assertEqual(perduti, [], "questi fornitori non sanno più scrivere il proprio ordine")

    def test_le_posizioni_dichiarate_sono_scritte_nella_forma_che_si_rilegge(self) -> None:
        """A positional signature written in the wrong form defends nothing.

        `_verifica_posizioni` looks up headers by normalized token: a key
        written "Cod.Art." instead of "codart" would never match anything,
        and the check would always pass — a defense that exists only on
        paper.
        """

        misurati = 0
        for voce in registro.adattatori():
            firma = voce.get("header_signature")
            colonne = firma.get("columns") if isinstance(firma, dict) else None
            if not isinstance(colonne, dict) or not colonne:
                continue
            misurati += 1
            with self.subTest(adattatore=voce["id"]):
                for token, posizione in colonne.items():
                    self.assertIsInstance(posizione, int)
                    self.assertGreaterEqual(posizione, 1)
                    self.assertEqual(registro.normalizza(token), token,
                                     "le posizioni si confrontano su token normalizzati")
        self.assertTrue(misurati, "nessun adattatore dichiara le posizioni: la difesa è sparita")

    def test_un_registro_che_manca_da_un_elenco_vuoto(self) -> None:
        self.assertEqual(registro.adattatori(self.radice / "non_esiste.json"), [])

    def test_un_registro_illeggibile_non_somiglia_a_un_file_sconosciuto(self) -> None:
        """The message must point at the installation, not at the price list."""

        esito = registro.riconosci({"format": "xlsx", "sheets": []}, self.radice / "non_esiste.json")

        self.assertEqual(esito["state"], "AMBIGUO")
        self.assertIn("Registro degli adattatori non leggibile", esito["evidence"][0])
        self.assertNotIn("Nessuna firma nota sufficiente", esito["evidence"])

    def test_un_registro_rovinato_lo_dice_con_parole_proprie(self) -> None:
        rovinato = self.radice / "adapters.json"
        rovinato.write_bytes(b"{questo non e' JSON")

        esito = registro.riconosci({"format": "xlsx", "sheets": []}, rovinato)

        self.assertEqual(esito["state"], "AMBIGUO")
        self.assertIn("non interpretabile", esito["evidence"][0])


class RiconoscimentoTests(unittest.TestCase):
    """Recognition on documents built specifically for these cases."""

    maxDiff = None

    def setUp(self) -> None:
        self.radice = Path(tempfile.mkdtemp(prefix="collaudo_riconosci_"))
        self.addCleanup(shutil.rmtree, self.radice, True)

    def riconosci_file(self, percorso: Path, adapters: Path | None = None) -> dict[str, Any]:
        return registro.riconosci(inspect_sources.profile_file(percorso)["details"], adapters)

    # -- il percorso veloce -------------------------------------------------

    def test_lo_schema_noto_passa_tutte_le_verifiche(self) -> None:
        esito = self.riconosci_file(foglio_noce(self.radice / "qualunque.xlsx"))

        self.assertEqual(esito["state"], "SCHEMA_NOTO")
        self.assertEqual(esito["adapter_id"], "noce_xls_v1")
        self.assertEqual(esito["confidence"], 0.99)
        self.assertTrue(all(verifica["ok"] for verifica in esito["checks"]))
        self.assertEqual([verifica["name"] for verifica in esito["checks"]],
                         ["colonne_attese", "riga_intestazione", "foglio", "righe_dati",
                          "tipi_plausibili", "posizioni_intestazioni"])
        self.assertEqual(esito["signature"]["header_row"], 5)
        self.assertEqual(esito["signature"]["data_start_row"], 6)
        self.assertEqual(esito["missing_headers"], [])

    def test_il_nome_del_file_non_decide_mai(self) -> None:
        """A file name is never evidence, in either direction.

        Here the document is named like a BETULLA price list and is actually
        a Larice one: a misleading name must not hijack recognition.
        """

        esito = self.riconosci_file(foglio_larice(self.radice / "LISTINO BETULLA VALIDO FINO AL 28-07-26.xlsx"))

        self.assertEqual(esito["adapter_id"], "larice_v1")
        self.assertEqual(esito["state"], "SCHEMA_NOTO")
        self.assertEqual(esito["confidence"], 0.97)

    def test_l_impronta_per_forma_riconosce_un_listino_senza_intestazioni(self) -> None:
        """Larice has no header row at all: without shape-based matching its
        rule would have to live in the code."""

        esito = self.riconosci_file(foglio_larice(self.radice / "canvass.xlsx"))

        self.assertEqual(esito["adapter_id"], "larice_v1")
        self.assertEqual(esito["signature"]["header_row"], None)
        self.assertIn("colonna R popolata come EAN", esito["evidence"])
        self.assertIn("indicatore I in colonna B", esito["evidence"])

    def test_il_csv_passa_dallo_stesso_motore(self) -> None:
        """A CSV's profile is flat, with no "sheets": recognition must still work."""

        percorso = self.radice / "noce.csv"
        with percorso.open("w", encoding="utf-8-sig", newline="") as flusso:
            scrittore = csv.writer(flusso)
            scrittore.writerow(["catalog_page", "ean", "product", "packaging",
                                "availability", "variation", "price", "unit"])
            scrittore.writerow(["1", "8000000000001", "PRODOTTO", "x 6", "SI", "", "1,25", "x 6"])

        esito = self.riconosci_file(percorso)

        self.assertEqual(esito["state"], "SCHEMA_NOTO")
        self.assertEqual(esito["adapter_id"], "noce_csv_v1")
        self.assertIsNone(esito["signature"]["sheet"])

    def test_un_profilo_xls_senza_formule_ne_celle_unite_non_rompe_niente(self) -> None:
        """For a .xls file the reader can't count formulas or merged ranges:
        `formula_count` and `merged_ranges_count` are None, not zero."""

        profilo = {
            "format": "xls", "sheet_count": 1,
            "sheets": [{
                "name": "Foglio1", "formula_count": None, "merged_ranges_count": None,
                "values_only": True,
                "active_range": {"min_row": 5, "min_column": 2, "max_row": 900,
                                 "max_column": 17, "nonempty_rows": 896},
                "header_candidates": [{"row": 5, "score": 44, "matched_keywords": [],
                                       "values": list(INTESTAZIONI_NOCE)}],
                "samples": {"initial": [], "middle": [], "final": []},
                "columns": [{"index": indice, "letter": "", "nonempty": 895,
                             "types": {"number": 895}, "examples": []}
                            for indice in range(1, 18)],
            }],
        }

        esito = registro.riconosci(profilo)

        self.assertEqual(esito["state"], "SCHEMA_NOTO")
        self.assertEqual(esito["adapter_id"], "noce_xls_v1")

    # -- what must not downgrade the schema ----------------------------------

    def test_una_colonna_in_piu_non_declassa_lo_schema(self) -> None:
        """A supplier adding a decorative column shouldn't cost an AI call
        every week: it's reported and recognition proceeds."""

        intestazioni = list(INTESTAZIONI_NOCE) + ["NOTE PROMOZIONALI"]
        esito = self.riconosci_file(foglio_noce(self.radice / "noce.xlsx", intestazioni=intestazioni))

        self.assertEqual(esito["state"], "SCHEMA_NOTO")
        self.assertEqual(esito["unknown_headers"], ["notepromozionali"])
        self.assertIn("Intestazioni non dichiarate, che non declassano lo schema: notepromozionali",
                      esito["evidence"])

    def test_il_totale_instabile_di_betulla_non_impedisce_il_riconoscimento(self) -> None:
        """The real price list writes TOTALE, this fixture writes TOTALI: same schema.

        Proves that identity can't be equality of the full header set.
        """

        percorso = scrivi_foglio(self.radice / "betulla.xlsx", [
            INTESTAZIONI_BETULLA,
            ["8000000000001", "C-001", None, "Prodotto Alfa", 6, 1.25, 60, 22, 0],
        ], "Listino")

        esito = self.riconosci_file(percorso)

        self.assertEqual(esito["state"], "SCHEMA_NOTO")
        self.assertEqual(esito["adapter_id"], "betulla_v1")
        self.assertIn("totali", esito["unknown_headers"])

    def test_un_adattatore_senza_mappatura_non_diventa_variato_a_vuoto(self) -> None:
        """BETULLA, Larice and the management export have dedicated readers, not
        a `field_mapping`: requiring mapping-only checks for them would
        always downgrade the schema.

        The checks that do apply to them still run, using
        `header_aliases` and `header_signature.columns` to know where their
        numeric fields and columns are."""

        percorso = scrivi_foglio(self.radice / "betulla.xlsx", [
            INTESTAZIONI_BETULLA,
            ["8000000000001", "C-001", None, "Prodotto Alfa", 6, 1.25, 60, 22, 0],
        ], "Listino")

        esito = self.riconosci_file(percorso)
        per_nome = {verifica["name"]: verifica for verifica in esito["checks"]}

        self.assertEqual(esito["state"], "SCHEMA_NOTO")
        for nome in ("colonne_attese", "riga_intestazione", "foglio"):
            self.assertTrue(per_nome[nome]["ok"])
            self.assertEqual(per_nome[nome]["detail"], registro.NON_APPLICABILE)
        # An empty price list does affect them, though.
        self.assertTrue(per_nome["righe_dati"]["ok"])
        # Same for types and positions, the only defense a positional reader has.
        self.assertTrue(per_nome["tipi_plausibili"]["ok"])
        self.assertIn("unit_price_net (Cessione)", per_nome["tipi_plausibili"]["detail"])
        self.assertTrue(per_nome["posizioni_intestazioni"]["ok"])
        self.assertNotEqual(per_nome["posizioni_intestazioni"]["detail"], registro.NON_APPLICABILE)

    # -- readers that read by position ----------------------------------
    #
    # BETULLA, the management export and Larice have no `field_mapping`: a
    # dedicated reader pulls the price from `row[5]`, not from the column
    # titled "Cessione". The header set is no defense for them, and the two
    # tests below measure the cost of not having positional checks either:
    # on the real price list, the fast path declared SCHEMA_NOTO 0.99 while
    # the reader returned 12.00 instead of 3.98.

    def test_una_colonna_in_piu_declassa_chi_legge_per_posizione(self) -> None:
        """A leading extra column shifts every other one by one: never
        decorative for a positional reader."""

        percorso = scrivi_foglio(self.radice / "betulla_colonna_in_piu.xlsx", [
            ["NOTE", *INTESTAZIONI_BETULLA],
            ["promo", "8000000000001", "C-001", None, "Prodotto Alfa", 6, 1.25, 60, 22, 0],
        ], "Listino")

        esito = self.riconosci_file(percorso)
        per_nome = {verifica["name"]: verifica for verifica in esito["checks"]}

        self.assertEqual(esito["adapter_id"], "betulla_v1")
        self.assertEqual(esito["state"], "SCHEMA_VARIATO")
        self.assertFalse(per_nome["posizioni_intestazioni"]["ok"])
        self.assertIn("«ean» attesa in colonna 1, trovata nella 2",
                      per_nome["posizioni_intestazioni"]["detail"])

    def test_la_colonna_in_piu_in_testa_declassa_anche_noce(self) -> None:
        """Noce's order column (I) is filled in BY POSITION in their `.xls`:
        a leading extra column would shift order quantities by one column
        without changing any header name.
        """

        percorso = foglio_noce(self.radice / "noce_colonna_in_piu.xlsx",
                                   intestazioni=["NOTE", *INTESTAZIONI_NOCE])

        esito = self.riconosci_file(percorso)
        per_nome = {verifica["name"]: verifica for verifica in esito["checks"]}

        self.assertEqual(esito["adapter_id"], "noce_xls_v1")
        self.assertEqual(esito["state"], "SCHEMA_VARIATO")
        self.assertFalse(per_nome["posizioni_intestazioni"]["ok"])

    def test_la_colonna_in_piu_in_testa_declassa_anche_cipresso(self) -> None:
        """Cipresso's order column (G) is positional: the signature declares
        column positions (measured on `3listino_Cipresso.xlsx`), so a
        shifted document is caught at recognition, not at order-file writing.
        """

        intestazioni_cipresso = ["COD.ART.", "DES.ARTICOLO", "UM", "QT",
                                 "LISTINO", "COD.EAN", "ORDINE"]
        intatto = scrivi_foglio(self.radice / "cipresso_intatto.xlsx", [
            intestazioni_cipresso,
            ["E-1", "PRODOTTO", "PZ", 6, 1.25, "8000000000001", None],
        ], "Listino Cipresso")
        slittato = scrivi_foglio(self.radice / "cipresso_colonna_in_piu.xlsx", [
            ["NOTE", *intestazioni_cipresso],
            ["promo", "E-1", "PRODOTTO", "PZ", 6, 1.25, "8000000000001", None],
        ], "Listino Cipresso")

        esito_intatto = self.riconosci_file(intatto)
        self.assertEqual(esito_intatto["adapter_id"], "cipresso_v1")
        self.assertEqual(esito_intatto["state"], "SCHEMA_NOTO")

        esito = self.riconosci_file(slittato)
        per_nome = {verifica["name"]: verifica for verifica in esito["checks"]}
        self.assertEqual(esito["adapter_id"], "cipresso_v1")
        self.assertEqual(esito["state"], "SCHEMA_VARIATO")
        self.assertFalse(per_nome["posizioni_intestazioni"]["ok"])
        self.assertIn("«ordine» attesa in colonna 7, trovata nella 8",
                      per_nome["posizioni_intestazioni"]["detail"])

    def test_due_colonne_scambiate_declassano_chi_legge_per_posizione(self) -> None:
        """The case no name-based check can see: same headers, same column
        count, nothing new — only two columns swapped.

        Without the positional check, `read_betulla` silently returns
        6.00 instead of 1.25, and 1.25 pieces per carton instead of 6."""

        scambiate = list(INTESTAZIONI_BETULLA)
        scambiate[4], scambiate[5] = scambiate[5], scambiate[4]
        com_e = scrivi_foglio(self.radice / "betulla_intatto.xlsx", [
            INTESTAZIONI_BETULLA,
            ["8000000000001", "C-001", None, "Prodotto Alfa", 6, 1.25, 60, 22, 0],
        ], "Listino")
        percorso = scrivi_foglio(self.radice / "betulla_scambiate.xlsx", [
            scambiate,
            ["8000000000001", "C-001", None, "Prodotto Alfa", 1.25, 6, 60, 22, 0],
        ], "Listino")

        intatto = self.riconosci_file(com_e)
        esito = self.riconosci_file(percorso)
        per_nome = {verifica["name"]: verifica for verifica in esito["checks"]}

        self.assertEqual(esito["adapter_id"], "betulla_v1")
        # Both documents have exactly the same headers — that's the point.
        self.assertEqual(esito["signature"]["headers"], intatto["signature"]["headers"])
        self.assertEqual(esito["unknown_headers"], intatto["unknown_headers"])
        self.assertEqual(intatto["state"], "SCHEMA_NOTO")
        self.assertEqual(esito["state"], "SCHEMA_VARIATO")
        self.assertFalse(per_nome["posizioni_intestazioni"]["ok"])
        self.assertIn("«pzct» attesa in colonna 5, trovata nella 6",
                      per_nome["posizioni_intestazioni"]["detail"])

    def test_il_prezzo_diventato_testo_declassa_anche_senza_mappatura(self) -> None:
        """`header_aliases`, not a mapping, says where BETULLA's price column
        is: this type check must run for dedicated readers too, or the
        largest suppliers in the comparison would lose it."""

        percorso = scrivi_foglio(self.radice / "betulla_prezzo_testo.xlsx", [
            INTESTAZIONI_BETULLA,
            ["8000000000001", "C-001", None, "Prodotto Alfa", 6, "EUR 1,25", 60, 22, 0],
            ["8000000000002", "C-002", None, "Prodotto Beta", 12, "EUR 2,50", 30, 22, 0],
        ], "Listino")

        esito = self.riconosci_file(percorso)
        per_nome = {verifica["name"]: verifica for verifica in esito["checks"]}

        self.assertEqual(esito["adapter_id"], "betulla_v1")
        self.assertEqual(esito["state"], "SCHEMA_VARIATO")
        self.assertFalse(per_nome["tipi_plausibili"]["ok"])
        self.assertIn("unit_price_net (Cessione) 0%", per_nome["tipi_plausibili"]["detail"])

    def test_un_listino_corto_non_e_un_listino_sbagliato(self) -> None:
        """The header cell is text and sits in the price column: counting it
        as data would drop the numeric ratio to 50% on a one-row price
        list, wrongly flagging a short list as a variant schema."""

        percorso = scrivi_foglio(self.radice / "betulla_una_riga.xlsx", [
            INTESTAZIONI_BETULLA,
            ["8000000000001", "C-001", None, "Prodotto Alfa", 6, 1.25, 60, 22, 0],
        ], "Listino")

        esito = self.riconosci_file(percorso)
        per_nome = {verifica["name"]: verifica for verifica in esito["checks"]}

        self.assertEqual(esito["state"], "SCHEMA_NOTO")
        self.assertTrue(per_nome["tipi_plausibili"]["ok"])
        self.assertIn("unit_price_net (Cessione) 100%", per_nome["tipi_plausibili"]["detail"])

    # -- what must downgrade the schema -----------------------------------

    def test_un_foglio_di_copertina_davanti_declassa_lo_schema(self) -> None:
        """`sheet: "FIRST"` means "the first sheet", not "whichever one matches":
        the real reader just opens sheet one.

        Without this check the fast path would score 0.99 on a document the
        reader can't even open."""

        percorso = self.radice / "cipresso_con_copertina.xlsx"
        workbook = Workbook()
        copertina = workbook.active
        copertina.title = "Condizioni generali"
        copertina.append(["Listino CIPRESSO - condizioni generali"])
        listino = workbook.create_sheet("Listino al 10-08-2026")
        listino.append(["COD.ART.", "DES.ARTICOLO", "UM", "QT", "LISTINO", "COD.EAN", "ORDINE"])
        listino.append(["E000001", "Prodotto Alfa", "PZ", 6, 1.25, "8000000000001", None])
        workbook.save(percorso)
        workbook.close()

        esito = self.riconosci_file(percorso)
        per_nome = {verifica["name"]: verifica for verifica in esito["checks"]}

        self.assertEqual(esito["adapter_id"], "cipresso_v1")
        self.assertEqual(esito["state"], "SCHEMA_VARIATO")
        self.assertFalse(per_nome["foglio"]["ok"])
        self.assertIn("«Condizioni generali»", per_nome["foglio"]["detail"])

    def test_un_listino_senza_merce_non_entra_in_silenzio_nemmeno_senza_mappatura(self) -> None:
        """BETULLA has a dedicated reader and no `field_mapping`: without
        falling back to `header_signature.data_start_row`, a price list with
        only a header row would pass as SCHEMA_NOTO, and the supplier would
        silently drop out of the comparison."""

        percorso = scrivi_foglio(self.radice / "betulla_vuoto.xlsx", [INTESTAZIONI_BETULLA], "Listino")

        esito = self.riconosci_file(percorso)
        per_nome = {verifica["name"]: verifica for verifica in esito["checks"]}

        self.assertEqual(esito["adapter_id"], "betulla_v1")
        self.assertEqual(esito["state"], "SCHEMA_VARIATO")
        self.assertFalse(per_nome["righe_dati"]["ok"])
        self.assertIn("riga 2", per_nome["righe_dati"]["detail"])

    def test_una_colonna_mappata_che_sparisce_diventa_schema_variato(self) -> None:
        """"Iva" isn't required, but the mapping uses it: without this check
        the reader would look for a column that isn't there."""

        intestazioni = [valore for valore in INTESTAZIONI_NOCE if valore != "Iva"]
        esito = self.riconosci_file(foglio_noce(self.radice / "noce.xlsx", intestazioni=intestazioni))

        per_nome = {verifica["name"]: verifica for verifica in esito["checks"]}
        self.assertEqual(esito["state"], "SCHEMA_VARIATO")
        self.assertEqual(esito["adapter_id"], "noce_xls_v1")
        self.assertEqual(esito["confidence"], 0.98)
        self.assertFalse(per_nome["colonne_attese"]["ok"])
        self.assertIn("iva", per_nome["colonne_attese"]["detail"])
        self.assertIn("Verifica «colonne_attese» non superata: colonne dichiarate e non trovate: iva",
                      esito["evidence"])

    def test_un_prezzo_diventato_testo_diventa_schema_variato(self) -> None:
        """The most damaging kind of variation: the comparison would empty
        out silently."""

        percorso = foglio_noce(self.radice / "noce.xlsx", prezzi_testuali=True)

        esito = self.riconosci_file(percorso)

        per_nome = {verifica["name"]: verifica for verifica in esito["checks"]}
        self.assertEqual(esito["state"], "SCHEMA_VARIATO")
        self.assertFalse(per_nome["tipi_plausibili"]["ok"])
        self.assertIn("unit_price_net", per_nome["tipi_plausibili"]["detail"])

    def test_l_intestazione_spostata_di_riga_diventa_schema_variato(self) -> None:
        """The reader starts from the declared row: a wrong row means it
        reads header labels as if they were products."""

        percorso = foglio_noce(self.radice / "noce.xlsx", riga_intestazione=3)

        esito = self.riconosci_file(percorso)

        per_nome = {verifica["name"]: verifica for verifica in esito["checks"]}
        self.assertEqual(esito["state"], "SCHEMA_VARIATO")
        self.assertFalse(per_nome["riga_intestazione"]["ok"])
        self.assertIn("riga 3", per_nome["riga_intestazione"]["detail"])

    def test_il_foglio_rinominato_diventa_schema_variato(self) -> None:
        percorso = foglio_noce(self.radice / "noce.xlsx", nome_foglio="Listino agosto")

        esito = self.riconosci_file(percorso)

        per_nome = {verifica["name"]: verifica for verifica in esito["checks"]}
        self.assertEqual(esito["state"], "SCHEMA_VARIATO")
        self.assertFalse(per_nome["foglio"]["ok"])

    def test_un_listino_senza_righe_di_dati_non_entra_in_silenzio(self) -> None:
        percorso = foglio_noce(self.radice / "noce.xlsx", righe_dati=0)

        esito = self.riconosci_file(percorso)

        per_nome = {verifica["name"]: verifica for verifica in esito["checks"]}
        self.assertEqual(esito["state"], "SCHEMA_VARIATO")
        self.assertFalse(per_nome["righe_dati"]["ok"])

    # -- columns declared by number -----------------------------------------

    def _listino_con_due_colonne_uguali(self, prezzi: list[Any]) -> Path:
        """A price list with a duplicated header: "COSTO IMPON." in both
        column 3 and column 5.

        With a repeated header the price can't be declared by name — the
        reader would stop with "duplicate header". Declaring it by number is
        the only option.
        """

        righe: list[list[Any]] = [["COD.EAN", "DESCRIZIONE", "COSTO IMPON.", "IVA", "COSTO IMPON."]]
        for numero, prezzo in enumerate(prezzi):
            righe.append([f"800000000000{numero}", f"PRODOTTO {numero}", prezzo, 22, prezzo])
        return scrivi_foglio(self.radice / "duecolonne.xlsx", righe, "Listino")

    def _registro_per_numero(self) -> Path:
        return registro_di_prova(self.radice / "adapters.json", [
            {"id": "duecolonne_v1", "supplier_id": "duecolonne", "kind": "supplier",
             "header_signature": {
                 "kind": "headers", "sheet": "FIRST", "header_row": 1, "data_start_row": 2,
                 "required": ["codean", "descrizione", "costoimpon", "iva"],
                 "known": ["codean", "descrizione", "costoimpon", "iva"]},
             "field_mapping": {
                 "sheet": "FIRST", "header_row": 1, "data_start_row": 2,
                 "columns": {"ean": "COD.EAN", "description": "DESCRIZIONE",
                             "unit_price_net": 3, "vat": "IVA"},
                 "order_column": "F", "assume_available": True}},
        ])

    def test_una_colonna_dichiarata_per_numero_viene_verificata_lo_stesso(self) -> None:
        """With prices in place the check passes, and names which column it read."""

        percorso = self._listino_con_due_colonne_uguali([1.25, 2.50, 3.75])

        esito = self.riconosci_file(percorso, self._registro_per_numero())

        per_nome = {verifica["name"]: verifica for verifica in esito["checks"]}
        self.assertEqual(esito["state"], "SCHEMA_NOTO")
        self.assertTrue(per_nome["tipi_plausibili"]["ok"], per_nome["tipi_plausibili"])
        self.assertIn("colonna 3", per_nome["tipi_plausibili"]["detail"])

    def test_il_prezzo_diventato_testo_declassa_anche_dichiarato_per_numero(self) -> None:
        """A price turned to text must downgrade the schema even when the
        column is declared by number, not by name.

        `_indice_di_colonna` must resolve a numeric column declaration too,
        or `tipi_plausibili` reports "not applicable" and the one check that
        catches a price turned to text stays off — exactly where number is
        the only way to declare the column.
        """

        percorso = self._listino_con_due_colonne_uguali(["1,25", "2,50", "3,75"])

        esito = self.riconosci_file(percorso, self._registro_per_numero())

        per_nome = {verifica["name"]: verifica for verifica in esito["checks"]}
        self.assertEqual(esito["state"], "SCHEMA_VARIATO")
        self.assertFalse(per_nome["tipi_plausibili"]["ok"])
        self.assertIn("unit_price_net (colonna 3)", per_nome["tipi_plausibili"]["detail"])

    def test_una_posizione_booleana_nella_firma_non_passa_per_buona(self) -> None:
        """In Python `True == 1`: a hand-written signature with a boolean
        position must not pass the check as "everything in place", and a
        non-integer position must not be silently skipped as if undeclared.
        """

        percorso = scrivi_foglio(self.radice / "booleano.xlsx", [
            ["COD.EAN", "DESCRIZIONE"],
            ["8000000000001", "PRODOTTO"],
        ], "Listino")
        adapters = registro_di_prova(self.radice / "adapters_bool.json", [
            {"id": "booleano_v1", "supplier_id": "booleano", "kind": "supplier",
             "header_signature": {
                 "kind": "headers", "sheet": "FIRST", "header_row": 1, "data_start_row": 2,
                 "required": ["codean", "descrizione"],
                 "known": ["codean", "descrizione"],
                 "columns": {"codean": True}},
             "field_mapping": {
                 "sheet": "FIRST", "header_row": 1, "data_start_row": 2,
                 "columns": {"ean": "COD.EAN", "description": "DESCRIZIONE"},
                 "order_column": "D", "assume_available": True}},
        ])

        esito = self.riconosci_file(percorso, adapters)

        per_nome = {verifica["name"]: verifica for verifica in esito["checks"]}
        self.assertFalse(per_nome["posizioni_intestazioni"]["ok"],
                         per_nome["posizioni_intestazioni"])
        self.assertIn("non è un numero intero", per_nome["posizioni_intestazioni"]["detail"])

    def test_un_numero_di_colonna_non_si_confonde_con_una_lettera(self) -> None:
        """"UM" is both a real CIPRESSO header and (as a letter) column 559."""

        self.assertEqual(registro._indice_di_colonna(["EAN", "Prezzo", "Note"], 3), 3)
        self.assertEqual(registro._indice_di_colonna(["EAN", "Prezzo", "Note"], "Prezzo"), 2)
        self.assertEqual(registro._indice_di_colonna(["EAN", "Prezzo", "Note"], "C"), 3)
        self.assertIsNone(registro._indice_di_colonna(["EAN"], 0))
        self.assertIsNone(registro._indice_di_colonna(["EAN"], True))

    def test_una_stringa_di_cifre_e_un_nome_e_non_un_numero(self) -> None:
        """The real reader (`column_number`) treats "9" as a header name, not
        a column number; the check must agree, or it could declare verified
        a document the reader will actually refuse.
        """

        self.assertIsNone(registro._indice_di_colonna(["EAN", "Prezzo", "Note"], "3"))
        self.assertIsNone(registro._indice_di_colonna(["EAN", "Prezzo", "Note"], "09"))
        self.assertIsNone(registro._indice_di_colonna(["EAN", "Prezzo", "Note"], "  9  "))
        # And when "3" really is a column's name, the name wins.
        self.assertEqual(registro._indice_di_colonna(["EAN", "3", "Note"], "3"), 2)

    # -- what stays unrecognized --------------------------------------------

    def test_un_fornitore_sconosciuto_resta_ambiguo(self) -> None:
        percorso = scrivi_foglio(self.radice / "sconosciuto.xlsx", [
            ["Codice", "Descrizione", "Conf.", "EAN", "Prezzo netto"],
            ["A1", "PRODOTTO", 6, "8000000000001", 1.25],
        ])

        esito = self.riconosci_file(percorso)

        self.assertEqual(esito["state"], "AMBIGUO")
        self.assertIsNone(esito["adapter_id"])
        self.assertEqual(esito["confidence"], 0.0)
        self.assertEqual(esito["evidence"], ["Nessuna firma nota sufficiente"])
        self.assertIsNone(esito["signature"])

    # -- rules for choosing among several candidates -------------------------

    def test_fra_due_candidati_vince_quello_che_dichiara_piu_obbligatorie(self) -> None:
        """At equal confidence the more specific schema wins: it describes
        the document better."""

        adapters = registro_di_prova(self.radice / "adapters.json", [
            {"id": "generico_v1", "supplier_id": "generico", "header_signature": {
                "kind": "headers", "sheet": "FIRST", "header_row": 1, "data_start_row": 2,
                "required": ["ean", "prezzo"], "known": ["ean", "prezzo"]}},
            {"id": "specifico_v1", "supplier_id": "specifico", "header_signature": {
                "kind": "headers", "sheet": "FIRST", "header_row": 1, "data_start_row": 2,
                "required": ["ean", "prezzo", "codice", "descrizione"],
                "known": ["ean", "prezzo", "codice", "descrizione"]}},
        ])
        percorso = scrivi_foglio(self.radice / "listino.xlsx", [
            ["EAN", "Prezzo", "Codice", "Descrizione"],
            ["8000000000001", 1.25, "A1", "PRODOTTO"],
        ])

        esito = self.riconosci_file(percorso, adapters)

        self.assertEqual(esito["adapter_id"], "specifico_v1")

    def test_la_soglia_dei_tipi_e_un_confronto_stretto(self) -> None:
        """`min_exclusive` is `>`, not `>=`: this was the behavior before the
        rule moved out of the code, and changing it silently would change
        recognition on an already-verified price list."""

        righe = [[None, "I", f"C{numero}", None, None, None, f"DESCRIZIONE {numero}",
                  None, None, None, None, None, None, None,
                  (2.50 if numero < 70 else "PREZZO DA CONCORDARE"),
                  0.05, None, f"800000{numero:07d}"]
                 for numero in range(100)]
        percorso = scrivi_foglio(self.radice / "forma.xlsx", righe, "Canvass")
        voce = {"id": "forma_v1", "supplier_id": "forma", "column_shape_signature": {
            "kind": "shape", "min_columns": 18, "required_columns": [2, 7, 15, 18],
            "min_score": 0.75, "max_confidence": 0.97,
            "checks": [
                {"column": 18, "min_nonempty": 100, "type_ratio": {"any_of": ["number", "text"], "min_exclusive": 0.95},
                 "weight": 0.4, "evidence": "colonna R popolata come EAN"},
                {"column": 7, "min_nonempty": 100, "type_ratio": {"any_of": ["text"], "min_exclusive": 0.7},
                 "weight": 0.3, "evidence": "descrizioni testuali in colonna G"},
                {"column": 15, "min_nonempty": 100, "type_ratio": {"any_of": ["number"], "min_exclusive": 0.7},
                 "weight": 0.3, "evidence": "prezzi numerici in colonna O"},
            ]}}

        # 70 out of 100: the ratio is exactly 0.7 and doesn't exceed it.
        stretto = registro_di_prova(self.radice / "stretto.json", [voce])
        esito_stretto = self.riconosci_file(percorso, stretto)

        larga = json.loads(json.dumps(voce))
        larga["column_shape_signature"]["checks"][2]["type_ratio"]["min_exclusive"] = 0.69
        esito_largo = self.riconosci_file(percorso, registro_di_prova(self.radice / "largo.json", [larga]))

        self.assertEqual(esito_stretto["state"], "AMBIGUO")
        self.assertEqual(esito_largo["adapter_id"], "forma_v1")
        self.assertEqual(esito_largo["confidence"], 0.97)

    def test_un_foglio_troppo_stretto_non_e_un_candidato_per_forma(self) -> None:
        """`min_columns` is the shape fingerprint's first defense: without it,
        four columns populated the right way would be enough for another
        supplier's price list to pass as Larice."""

        percorso = foglio_larice(self.radice / "stretto.xlsx")
        largo = registro_di_prova(self.radice / "largo.json", [
            {"id": "forma_v1", "supplier_id": "forma", "column_shape_signature": {
                "kind": "shape", "min_columns": 18, "required_columns": [2, 7, 15, 18],
                "min_score": 0.75, "max_confidence": 0.97,
                "checks": [{"column": 7, "min_nonempty": 100, "type_ratio": {"any_of": ["text"], "min_exclusive": 0.7},
                            "weight": 0.8, "evidence": "descrizioni testuali in colonna G"}]}},
        ])
        documento = json.loads(largo.read_text(encoding="utf-8"))
        documento["adapters"][0]["column_shape_signature"]["min_columns"] = 20
        stretto = registro_di_prova(self.radice / "stretto.json", documento["adapters"])

        self.assertEqual(self.riconosci_file(percorso, largo)["adapter_id"], "forma_v1")
        self.assertEqual(self.riconosci_file(percorso, stretto)["state"], "AMBIGUO")

    def test_una_colonna_dichiarata_per_nome_non_diventa_una_lettera(self) -> None:
        """"UM", "QT" and "P" are real headers and also plausible column
        letters: checking headers first avoids measuring the wrong column."""

        intestazioni = ["EAN", "DESCRIZIONE", "P"] + [None] * 12 + ["NOTE"]
        righe = [intestazioni] + [
            [f"800000000000{numero}", f"PRODOTTO {numero}", 1.25 + numero] + [None] * 12 + ["nota testuale"]
            for numero in range(5)
        ]
        percorso = scrivi_foglio(self.radice / "listino.xlsx", righe)
        adapters = registro_di_prova(self.radice / "adapters.json", [
            {"id": "lettere_v1", "supplier_id": "lettere",
             "header_signature": {"kind": "headers", "sheet": "FIRST", "header_row": 1,
                                  "data_start_row": 2, "required": ["ean", "descrizione", "p"],
                                  "known": ["ean", "descrizione", "p", "note"]},
             "field_mapping": {"sheet": "FIRST", "header_row": 1, "data_start_row": 2,
                               "columns": {"ean": "EAN", "description": "DESCRIZIONE",
                                           "unit_price_net": "P"}}},
        ])

        esito = self.riconosci_file(percorso, adapters)

        per_nome = {verifica["name"]: verifica for verifica in esito["checks"]}
        self.assertEqual(esito["state"], "SCHEMA_NOTO")
        self.assertTrue(per_nome["tipi_plausibili"]["ok"])

    def test_una_colonna_dichiarata_per_numero_si_verifica_sul_documento(self) -> None:
        """A column with no header is declared by number: the case for almost
        every order column, which arrives empty.

        Looking for "6" among the headers would never find it, leaving the
        supplier unrecognized forever — an AI call every week for a column
        the reader finds trivially."""

        intestazioni = ["CODICE", "DESCRIZIONE", "PZ", "PREZZO", "BARCODE", None]
        righe = [intestazioni] + [
            [f"A{numero}", f"PRODOTTO {numero}", 6, 1.25 + numero, f"800000000000{numero}", "SI"]
            for numero in range(5)
        ]
        percorso = scrivi_foglio(self.radice / "con_colonna_muta.xlsx", righe)
        mappatura = {"sheet": "FIRST", "header_row": 1, "data_start_row": 2, "order_column": "G",
                     "columns": {"supplier_code": "CODICE", "description": "DESCRIZIONE",
                                 "pieces_per_carton": "PZ", "unit_price_net": "PREZZO",
                                 "ean": "BARCODE", "availability": 6}}
        firma = {"kind": "headers", "sheet": "FIRST", "header_row": 1, "data_start_row": 2,
                 "required": ["codice", "descrizione", "prezzo"],
                 "known": ["barcode", "codice", "descrizione", "prezzo", "pz"]}
        dentro = registro_di_prova(self.radice / "dentro.json", [
            {"id": "muta_v1", "supplier_id": "muta", "header_signature": firma, "field_mapping": mappatura},
        ])
        fuori = json.loads(json.dumps(mappatura))
        fuori["columns"]["availability"] = 99
        oltre = registro_di_prova(self.radice / "oltre.json", [
            {"id": "muta_v1", "supplier_id": "muta", "header_signature": firma, "field_mapping": fuori},
        ])

        esito = self.riconosci_file(percorso, dentro)
        per_nome = {verifica["name"]: verifica for verifica in esito["checks"]}

        self.assertEqual(esito["state"], "SCHEMA_NOTO")
        self.assertIn("di cui 1 per numero", per_nome["colonne_attese"]["detail"])
        # A column declared past the document's width is still an error:
        # the difference between "has no name" and "doesn't exist".
        oltre_esito = self.riconosci_file(percorso, oltre)
        oltre_nome = {verifica["name"]: verifica for verifica in oltre_esito["checks"]}
        self.assertEqual(oltre_esito["state"], "SCHEMA_VARIATO")
        self.assertFalse(oltre_nome["colonne_attese"]["ok"])
        self.assertIn("99", oltre_nome["colonne_attese"]["detail"])

    def test_il_profilo_di_un_csv_distingue_i_numeri_dal_testo(self) -> None:
        """In a CSV everything is text, so the profiler must not report the
        price column as "text" too, or any mapped CSV adapter would be stuck
        at SCHEMA_VARIATO forever. Prices arrive Italian-formatted ("21,75")
        and must be recognized as numeric in that form too."""

        percorso = self.radice / "fornitore.csv"
        with percorso.open("w", encoding="utf-8", newline="") as flusso:
            scrittore = csv.writer(flusso)
            scrittore.writerow(["codice", "descrizione", "pezzi", "prezzo", "barcode"])
            for numero in range(5):
                scrittore.writerow([f"A{numero}", f"PRODOTTO {numero}", 6, f"21,7{numero}",
                                    f"800000000000{numero}"])
        adapters = registro_di_prova(self.radice / "adapters.json", [
            {"id": "csv_v1", "supplier_id": "csv",
             "header_signature": {"kind": "headers", "sheet": None, "header_row": 1, "data_start_row": 2,
                                  "required": ["codice", "descrizione", "prezzo"],
                                  "known": ["barcode", "codice", "descrizione", "pezzi", "prezzo"]},
             "field_mapping": {"sheet": None, "header_row": 1, "data_start_row": 2,
                               "columns": {"supplier_code": "codice", "description": "descrizione",
                                           "pieces_per_carton": "pezzi", "unit_price_net": "prezzo",
                                           "ean": "barcode"}}},
        ])

        esito = self.riconosci_file(percorso, adapters)
        per_nome = {verifica["name"]: verifica for verifica in esito["checks"]}

        self.assertEqual(esito["state"], "SCHEMA_NOTO")
        self.assertTrue(per_nome["tipi_plausibili"]["ok"])
        self.assertIn("unit_price_net (prezzo) 100%", per_nome["tipi_plausibili"]["detail"])

    def test_un_adattatore_per_forma_dice_quali_obbligatorie_mancano(self) -> None:
        """`missing_headers` can be non-empty only here: a shape-matched
        adapter that also declares a header fingerprint.

        Covers the day Larice starts titling its columns, and is the only
        message telling the user what to look for."""

        larice = next(voce for voce in registro.adattatori() if voce["id"] == "larice_v1")
        adapters = registro_di_prova(self.radice / "larice_intitolato.json", [{
            **larice,
            "header_signature": {"kind": "headers", "sheet": "FIRST", "header_row": 1,
                                 "data_start_row": 2,
                                 "required": ["codart", "descrizione", "ean"],
                                 "known": ["codart", "descrizione", "ean"]},
        }])

        esito = self.riconosci_file(foglio_larice(self.radice / "canvass_muto.xlsx"), adapters)

        self.assertEqual(esito["adapter_id"], "larice_v1")
        self.assertEqual(esito["missing_headers"], ["codart", "descrizione", "ean"])
        self.assertIn("Intestazioni dichiarate obbligatorie e non trovate: codart, descrizione, ean",
                      esito["evidence"])


class ScritturaVersionataTests(unittest.TestCase):
    """Writing to the registry without losing what was there before."""

    maxDiff = None

    def setUp(self) -> None:
        self.radice = Path(tempfile.mkdtemp(prefix="collaudo_scrittura_"))
        self.addCleanup(shutil.rmtree, self.radice, True)
        # Always on a copy: a test must never be able to touch the real registry.
        self.registro = self.radice / "adapters.json"
        shutil.copyfile(ADAPTERS, self.registro)
        # There are two registry files: the shipped one, under git, that no
        # write ever touches, and the learned one, outside git, where
        # everything the engine learns ends up. `scrivi_adattatore` writes
        # only the second; a read sees them merged.
        self.imparato = registro.percorso_imparato(self.registro)

    def documento(self) -> dict[str, Any]:
        """The file writes actually land in."""

        return json.loads(self.imparato.read_text(encoding="utf-8"))

    def voce(self, identificativo: str) -> dict[str, Any]:
        """The entry as the engine sees it: shipped and learned merged."""

        letta = registro.adattatore(identificativo, self.registro)
        if not letta:
            raise AssertionError(f"«{identificativo}» non è nel registro effettivo")
        return letta

    def test_un_adattatore_nuovo_entra_in_coda_alla_versione_uno(self) -> None:
        esito = registro.scrivi_adattatore(
            {"id": "acero_v1", "kind": "supplier", "supplier_id": "acero",
             "field_mapping": {"sheet": "Foglio1", "header_row": 6}},
            self.registro,
        )

        self.assertEqual(esito, {"id": "acero_v1", "schema_version": 1,
                                 "created": True, "previous_versions": 0})
        self.assertEqual(self.documento()["adapters"][-1]["id"], "acero_v1")
        self.assertNotIn("previous_versions", self.voce("acero_v1"))

    def test_dopo_due_scritture_la_prima_versione_e_ancora_leggibile(self) -> None:
        """The property that matters: past writes stay inspectable.

        If recognition regresses later, the only way to tell what changed is
        still having the starting version to compare against.
        """

        originale = self.voce("cipresso_v1")

        registro.scrivi_adattatore({**originale, "display_name": "CIPRESSO seconda"}, self.registro)
        seconda = self.voce("cipresso_v1")
        esito = registro.scrivi_adattatore({**seconda, "display_name": "CIPRESSO terza"}, self.registro)

        viva = self.voce("cipresso_v1")
        self.assertEqual(viva["display_name"], "CIPRESSO terza")
        self.assertEqual(viva["schema_version"], 3)
        self.assertEqual(esito["previous_versions"], 2)
        self.assertEqual(len(viva["previous_versions"]), 2)
        # The first version, the one the engine started with, is still there
        # in full — fingerprint included — and carries no history of its own.
        prima = viva["previous_versions"][0]
        self.assertEqual(prima["display_name"], "CIPRESSO")
        self.assertEqual(prima["schema_version"], 1)
        self.assertEqual(prima["header_signature"], originale["header_signature"])
        self.assertNotIn("previous_versions", prima)
        self.assertEqual(viva["previous_versions"][1]["display_name"], "CIPRESSO seconda")

    def test_la_voce_viva_resta_al_livello_superiore(self) -> None:
        """Reading `field_mapping` by id must still return the latest version,
        unaware of any earlier ones."""

        originale = self.voce("cipresso_v1")
        registro.scrivi_adattatore({**originale, "display_name": "CIPRESSO nuova"}, self.registro)

        percorso_originale = registro.REGISTRO
        registro.REGISTRO = self.registro
        try:
            letta = registro.adattatore("cipresso_v1")
        finally:
            registro.REGISTRO = percorso_originale

        self.assertEqual(letta["display_name"], "CIPRESSO nuova")
        self.assertEqual(letta["field_mapping"], originale["field_mapping"])

    def test_il_registro_resta_a_fine_riga_lf_con_due_spazi(self) -> None:
        """The two registry files get diffed side by side when something's
        off; CRLF line endings would make every line look different even
        where the content is the same."""

        registro.scrivi_adattatore({"id": "nuovo_v1", "supplier_id": "nuovo"}, self.registro)

        contenuto = self.imparato.read_bytes()
        self.assertNotIn(b"\r\n", contenuto)
        self.assertTrue(contenuto.endswith(b"\n"))
        self.assertIn(b'\n  "adapters": [\n', contenuto)

    def test_la_scrittura_non_lascia_file_temporanei(self) -> None:
        registro.scrivi_adattatore({"id": "nuovo_v1", "supplier_id": "nuovo"}, self.registro)

        self.assertEqual(
            sorted(percorso.name for percorso in self.radice.iterdir()),
            ["adapters.json", "adattatori_imparati.json"],
        )

    def test_il_registro_spedito_non_cambia_di_un_byte(self) -> None:
        """Why the registry is two files: the one under git gets reset on
        every launch, so anything learned that ended up there would be lost."""

        prima = self.registro.read_bytes()
        originale = self.voce("cipresso_v1")

        registro.scrivi_adattatore({"id": "nuovo_v1", "supplier_id": "nuovo"}, self.registro)
        registro.scrivi_adattatore({**originale, "display_name": "CIPRESSO imparata"}, self.registro)

        self.assertEqual(self.registro.read_bytes(), prima)
        self.assertEqual(
            [voce["id"] for voce in self.documento()["adapters"]],
            ["nuovo_v1", "cipresso_v1"],
        )

    def test_a_parita_di_id_vince_l_imparato(self) -> None:
        """Whoever has the document in front of them outranks last week's
        shipped entry. The cost: a badly learned adapter isn't fixed by
        shipping a new one — it's fixed by removing its learned entry."""

        originale = self.voce("cipresso_v1")
        self.assertEqual(originale["display_name"], "CIPRESSO")

        registro.scrivi_adattatore({**originale, "display_name": "CIPRESSO del negozio"}, self.registro)

        self.assertEqual(self.voce("cipresso_v1")["display_name"], "CIPRESSO del negozio")
        self.assertEqual(
            registro.nomi_dei_fornitori(self.registro)["cipresso"], "CIPRESSO del negozio",
        )
        # The shipped entry is untouched: the earlier version isn't lost, only shadowed.
        spedite = json.loads(self.registro.read_text(encoding="utf-8"))["adapters"]
        originale_spedita = next(voce for voce in spedite if voce["id"] == "cipresso_v1")
        self.assertEqual(originale_spedita["display_name"], "CIPRESSO")

    def test_un_imparato_che_non_c_e_non_e_un_errore(self) -> None:
        """A fresh install has learned nothing, and must still work."""

        self.assertFalse(self.imparato.exists())
        self.assertIsNone(registro.motivo_registro_illeggibile(self.registro))
        self.assertIn("cipresso_v1", [voce["id"] for voce in registro.adattatori(self.registro)])

    def test_un_imparato_rotto_si_dice_ma_non_ferma_il_riconoscimento(self) -> None:
        """Stopping entirely would be worse: the shipped adapters are enough
        to keep working, and a broken learned file needs fixing, not silent
        failure."""

        self.imparato.write_bytes(b"{questo non e' JSON")

        motivo = registro.motivo_registro_illeggibile(self.registro)

        self.assertIsNotNone(motivo)
        self.assertIn("imparati", motivo)
        self.assertIn("cipresso_v1", [voce["id"] for voce in registro.adattatori(self.registro)])

    def test_una_scrittura_interrotta_non_rovina_il_registro(self) -> None:
        """A write goes through a temp file and `os.replace` because a
        truncated registry means zero adapters: every supplier suddenly
        unrecognized, with no clue why."""

        registro.scrivi_adattatore({"id": "nuovo_v1", "supplier_id": "nuovo"}, self.registro)
        prima = self.registro.read_bytes()
        prima_imparato = self.imparato.read_bytes()
        identificativi = [voce["id"] for voce in registro.adattatori(self.registro)]

        def dump_che_si_interrompe(documento: Any, flusso: Any, **_argomenti: Any) -> None:
            flusso.write('{\n  "adapters": [\n    {"id": "a')
            raise OSError("disco pieno")

        originale = registro.json.dump
        registro.json.dump = dump_che_si_interrompe
        try:
            with self.assertRaises(OSError):
                registro.scrivi_adattatore({"id": "acero_v1", "supplier_id": "acero"}, self.registro)
        finally:
            registro.json.dump = originale

        self.assertEqual(self.registro.read_bytes(), prima, "il registro non deve cambiare di un byte")
        self.assertEqual(self.imparato.read_bytes(), prima_imparato, "nemmeno l'imparato")
        # The property is "nothing was lost", not a fixed count: the number
        # of adapters grows every time one is learned.
        dopo = [voce["id"] for voce in registro.adattatori(self.registro)]
        self.assertEqual(dopo, identificativi)
        # Checked against the list taken AFTER the attempt, not before it:
        # "acero_v1" wasn't in the earlier list by construction, so that
        # comparison alone wouldn't prove anything.
        self.assertNotIn("acero_v1", dopo, "la voce interrotta non deve essere entrata")
        self.assertEqual(
            sorted(percorso.name for percorso in self.radice.iterdir()),
            ["adapters.json", "adattatori_imparati.json"],
        )

    def test_un_adattatore_di_un_altro_fornitore_viene_rifiutato(self) -> None:
        """Would otherwise let one supplier overwrite another's adapter."""

        with self.assertRaises(ValueError) as errore:
            registro.scrivi_adattatore({"id": "cipresso_v1", "supplier_id": "acero"}, self.registro)

        self.assertIn("cipresso", str(errore.exception))
        self.assertEqual(self.voce("cipresso_v1")["supplier_id"], "cipresso")
        self.assertEqual(self.voce("cipresso_v1")["schema_version"], 1)

    def test_una_voce_senza_id_viene_rifiutata(self) -> None:
        with self.assertRaises(ValueError):
            registro.scrivi_adattatore({"supplier_id": "acero"}, self.registro)
        with self.assertRaises(ValueError):
            registro.scrivi_adattatore(["non", "un", "dizionario"], self.registro)  # type: ignore[arg-type]

    def test_un_imparato_illeggibile_non_viene_riscritto_da_zero(self) -> None:
        """Better to stop than to lose every other adapter."""

        self.imparato.write_bytes(b"{questo non e' JSON")

        with self.assertRaises(ValueError) as errore:
            registro.scrivi_adattatore({"id": "nuovo_v1", "supplier_id": "nuovo"}, self.registro)

        self.assertIn("non è leggibile", str(errore.exception))
        self.assertEqual(self.imparato.read_bytes(), b"{questo non e' JSON")

    def test_uno_spedito_illeggibile_ferma_la_scrittura(self) -> None:
        """Without the shipped file there's no base version to write on top
        of: writing anyway would silently reset a versioned adapter's history."""

        self.registro.write_bytes(b"{questo non e' JSON")

        with self.assertRaises(ValueError) as errore:
            registro.scrivi_adattatore({"id": "nuovo_v1", "supplier_id": "nuovo"}, self.registro)

        self.assertIn("non è leggibile", str(errore.exception))
        self.assertFalse(self.imparato.exists())

    def test_un_registro_che_non_c_e_viene_creato(self) -> None:
        vergine = self.radice / "nuova" / "adapters.json"

        esito = registro.scrivi_adattatore({"id": "nuovo_v1", "supplier_id": "nuovo"}, vergine)

        self.assertTrue(esito["created"])
        scritto = registro.percorso_imparato(vergine)
        self.assertEqual(json.loads(scritto.read_text(encoding="utf-8"))["adapters"][0]["id"], "nuovo_v1")

    def test_una_voce_scritta_si_riconosce_subito(self) -> None:
        """Writing and recognition must agree on the fingerprint format, or
        an adapter learned today would stay unrecognized tomorrow."""

        cartella = self.radice / "listini"
        cartella.mkdir()
        percorso = scrivi_foglio(cartella / "acero.xlsx", [
            ["Cod.Art.", "Cod.Ean", "Descrizione", "Pz x Ct", "Costo impon."],
            ["A1", "8000000000001", "PRODOTTO", 6, 1.25],
        ])
        profilo = inspect_sources.profile_file(percorso)["details"]
        intestazioni = profilo["sheets"][0]["header_candidates"][0]["values"]
        impronta = registro.impronta("FIRST", 1, 2, intestazioni)

        registro.scrivi_adattatore({
            "id": "acero_v1", "kind": "supplier", "supplier_id": "acero",
            "header_signature": {"kind": "headers", "sheet": "FIRST", "header_row": 1,
                                 "data_start_row": 2, "required": impronta["headers"],
                                 "known": impronta["headers"]},
        }, self.registro)

        esito = registro.riconosci(profilo, self.registro)

        self.assertEqual(esito["state"], "SCHEMA_NOTO")
        self.assertEqual(esito["adapter_id"], "acero_v1")


@unittest.skipUnless(
    LISTINI.is_dir(),
    f"Mancano i listini veri: {LISTINI}. "
    "Si può indicare un'altra cartella con la variabile d'ambiente LISTINI_STORICI.",
)
class ListiniVeriTests(unittest.TestCase):
    """Recognition against real price lists, read-only.

    These are the measurements that matter: an engine that only works on
    fixtures built by the tests is useless.
    """

    maxDiff = None

    def setUp(self) -> None:
        self.radice = Path(tempfile.mkdtemp(prefix="collaudo_forma_"))
        self.addCleanup(shutil.rmtree, self.radice, True)

    def riconosci_listino(self, nome: str) -> dict[str, Any]:
        percorso = LISTINI / nome
        if not percorso.is_file():
            self.skipTest(f"Manca il listino {percorso}")
        return registro.riconosci(profilo_del_file(percorso))

    def test_betulla_e_riconosciuto(self) -> None:
        esito = self.riconosci_listino("LISTINO BETULLA VALIDO FINO AL 28-07-26.xlsx")

        self.assertEqual(esito["state"], "SCHEMA_NOTO")
        self.assertEqual(esito["adapter_id"], "betulla_v1")
        self.assertEqual(esito["signature"]["sheet"], "Sheet1")
        self.assertEqual(esito["signature"]["header_row"], 1)
        # The real price list writes TOTALE: it's declared, so it's not unknown.
        self.assertEqual(esito["unknown_headers"], [])

    def test_il_betulla_risalvato_in_excel_si_riconosce_lo_stesso_per_dirlo(self) -> None:
        """A BETULLA file resaved in Excel with the ORDINE header cell cleared.

        The document stays AMBIGUO — a missing required column must not be
        read — but the message should say what's missing and where, instead
        of the generic "no known signature is sufficient".
        """

        percorso = LISTINI / "LISTINO BETULLA VALIDO FINO AL 01-09-26.xlsx"
        if not percorso.is_file():
            percorso = LISTINI.parent / "documenti" / "LISTINO BETULLA VALIDO FINO AL 01-09-26.xlsx"
        if not percorso.is_file():
            self.skipTest(f"Manca il listino BETULLA sotto {LISTINI}")

        # Copy first, modify the copy: the original is an input fixture.
        guasto = self.radice / "betulla risalvato.xlsx"
        shutil.copy(percorso, guasto)
        libro = load_workbook(guasto)
        foglio = libro[libro.sheetnames[0]]
        self.assertEqual(foglio.cell(row=1, column=3).value, "ORDINE")
        foglio.cell(row=1, column=3).value = None
        libro.save(guasto)
        libro.close()

        esito = registro.riconosci(profilo_del_file(guasto))

        self.assertEqual(esito["state"], "AMBIGUO")
        self.assertEqual(esito["quasi_adapter_id"], "betulla_v1")
        self.assertEqual(esito["quasi_missing"], [{"header": "ORDINE", "column": "C"}])
        self.assertEqual(esito["quasi_present"], 4)
        self.assertIn("BETULLA", esito["evidence"][0])
        self.assertIn("colonna C", esito["evidence"][0])

    def test_i_listini_di_nessuno_restano_di_nessuno(self) -> None:
        """Counter-check for "close but not quite": a document the registry
        truly doesn't know must not become "looks like...".

        ACERO, QUERCIA and GINEPRO have no adapter at all. If one of them
        started to resemble a known supplier, the "close match" message
        would be guessing instead of recognizing.
        """

        for nome in ("ACERO LISTINO SETTIMANA 26.xlsx",
                     "LISTINO QUERCIA AGGIORNATO DEL 06-08-2026.xlsx",
                     "ListinoGINEPRO 5 aggiornato al 25-02-2026.xlsx"):
            with self.subTest(listino=nome):
                esito = self.riconosci_listino(nome)
                self.assertEqual(esito["state"], "AMBIGUO")
                self.assertEqual(esito["evidence"], ["Nessuna firma nota sufficiente"])

    def test_cipresso_e_riconosciuto(self) -> None:
        esito = self.riconosci_listino("3listino_Cipresso.xlsx")

        self.assertEqual(esito["state"], "SCHEMA_NOTO")
        self.assertEqual(esito["adapter_id"], "cipresso_v1")
        self.assertTrue(all(verifica["ok"] for verifica in esito["checks"]))

    def test_noce_e_riconosciuto_con_l_intestazione_alla_riga_cinque(self) -> None:
        esito = self.riconosci_listino("formattato_104233.xls")

        self.assertEqual(esito["state"], "SCHEMA_NOTO")
        self.assertEqual(esito["adapter_id"], "noce_xls_v1")
        self.assertIn("riga 5", " ".join(esito["evidence"]))
        self.assertIn("riga 6", " ".join(esito["evidence"]))

    def test_le_offerte_sono_riconosciute_per_forma(self) -> None:
        """The offers sheet has no header row to read.

        Above the products there are blank rows and only the word ORDINE in
        column H: a header fingerprint has nothing to match, which is why
        this adapter, like Larice, is recognized by column shape instead.
        Where the products start isn't a fixed row number — a marker finds
        it fresh on every read, since the blank block on top varies in size.
        """

        esito = self.riconosci_listino("OFFERTE AGOSTO 4.xlsx")

        self.assertEqual(esito["state"], "SCHEMA_NOTO")
        self.assertEqual(esito["adapter_id"], "offerte_v1")
        self.assertEqual(esito["confidence"], 0.96)

    def test_nessun_altro_listino_vero_finisce_nelle_offerte(self) -> None:
        """Column shape is a generic fingerprint: it must not match elsewhere.

        With no header names anchoring it, the real risk isn't that a shape
        fingerprint fails to match its own document — it's that it claims
        someone else's. This runs against every real price list the project
        keeps, including the three no adapter has learned yet.
        """

        for nome in ("LISTINO BETULLA VALIDO FINO AL 28-07-26.xlsx",
                     "3listino_Cipresso.xlsx",
                     "Listino3_33.xlsx",
                     "28.1 06-10lug.xlsx",
                     "31.1 27-31lug (1).xlsx",
                     "Copia di 30.1 20-24lug.xlsx",
                     "formattato_104233.xls",
                     "ACERO LISTINO SETTIMANA 26.xlsx",
                     "LISTINO QUERCIA AGGIORNATO DEL 06-08-2026.xlsx",
                     "ListinoGINEPRO 5 aggiornato al 25-02-2026.xlsx"):
            with self.subTest(listino=nome):
                self.assertNotEqual(self.riconosci_listino(nome)["adapter_id"], "offerte_v1")

    def test_l_impronta_delle_offerte_non_si_prende_il_listino_di_un_altro(self) -> None:
        """The real risk of a shape fingerprint: nearly any price list has
        prices, units per package and barcodes in its first six columns.
        With only four checks out of five, a generic price list could score
        0.84 against a 0.75 threshold and match the "offers" adapter — read
        whole, compiled into an order, with no way to correct it from the
        page, since a SCHEMA_NOTO document never opens the guided mapping.
        """

        prodotti = [
            [f"0{28000 + indice}", f"ARTICOLO DI PROVA {indice}", "PZ", 6, 1.85 + indice / 100,
             f"800241003{7000 + indice}"]
            for indice in range(120)
        ]

        # Unique file names matter here: `profilo_del_file` caches profiles by
        # path, so two different sheets sharing a name would return the same
        # cached profile, making the test prove nothing.
        contatore = itertools.count(1)

        def foglio(testa: list[list[Any]], coda: list[Any] | None = None) -> dict[str, Any]:
            percorso = self.radice / f"forma_{next(contatore)}.xlsx"
            libro = Workbook()
            ws = libro.active
            ws.title = "Foglio1"
            for riga in testa:
                ws.append(riga)
            for riga in prodotti:
                ws.append(list(riga) + (list(coda) if coda else []))
            libro.save(percorso)
            libro.close()
            return registro.riconosci(profilo_del_file(percorso))

        with self.subTest(caso="listino normale con IVA in G, nessun ORDINE"):
            esito = foglio([["CODICE", "DESCRIZIONE", "U.M.", "PZ-CT", "PREZZO", "BARCODE", "IVA"]], ["22"])
            self.assertNotEqual(esito["adapter_id"], "offerte_v1")

        with self.subTest(caso="intestazione alla riga 3, ORDINE in H, IVA in G"):
            esito = foglio(
                [["LISTINO ROSSI"], [], ["CODICE", "DESCRIZIONE", "UM", "QT", "PREZZO", "EAN", "IVA", "ORDINE"]],
                ["22"],
            )
            self.assertNotEqual(esito["adapter_id"], "offerte_v1")

        with self.subTest(caso="il foglio delle offerte, senza la parola ORDINE"):
            esito = foglio([[], [], [], [], []])
            self.assertNotEqual(esito["adapter_id"], "offerte_v1")

        with self.subTest(caso="il foglio delle offerte, com'è"):
            esito = foglio([[], [], [None] * 7 + ["ORDINE "], [], []])
            self.assertEqual(esito["adapter_id"], "offerte_v1")
            self.assertEqual(esito["state"], "SCHEMA_NOTO")

    def test_le_offerte_si_riconoscono_anche_quando_sono_poche(self) -> None:
        """A regular promo has around thirty items; this covers a much larger one."""

        libro = Workbook()
        ws = libro.active
        ws.title = "Foglio1"
        for _ in range(2):
            ws.append([])
        ws.append([None] * 7 + ["ORDINE "])
        for _ in range(2):
            ws.append([])
        for indice in range(20):
            ws.append([f"0{28000 + indice}", f"ARTICOLO {indice}", "PZ", 6, 1.85, f"800241003{7000 + indice}"])
        percorso = self.radice / "offerte_corte.xlsx"
        libro.save(percorso)
        libro.close()

        self.assertEqual(registro.riconosci(profilo_del_file(percorso))["adapter_id"], "offerte_v1")

    def test_i_tre_larice_sono_riconosciuti_per_forma(self) -> None:
        for nome in ("28.1 06-10lug.xlsx", "31.1 27-31lug (1).xlsx", "Copia di 30.1 20-24lug.xlsx"):
            with self.subTest(listino=nome):
                esito = self.riconosci_listino(nome)
                self.assertEqual(esito["state"], "SCHEMA_NOTO")
                self.assertEqual(esito["adapter_id"], "larice_v1")
                self.assertEqual(esito["confidence"], 0.97)

    def test_nessun_adattatore_nativo_si_prende_un_listino_che_non_e_suo(self) -> None:
        """ACERO, QUERCIA, GINEPRO and the second CIPRESSO schema must never be
        claimed by one of the six native adapters.

        The property isn't "stays AMBIGUO" — once a user learns one of these,
        which is what the registry is for, that price list legitimately
        becomes known, and a test pinned to AMBIGUO would fail on correct
        behavior. What must never happen is a native adapter claiming it: a
        price list read with another supplier's commercial rules, silently.
        """

        for nome in ("ACERO LISTINO SETTIMANA 26.xlsx",
                     "LISTINO QUERCIA AGGIORNATO DEL 06-08-2026.xlsx",
                     "ListinoGINEPRO 5 aggiornato al 25-02-2026.xlsx",
                     "Listino3_33.xlsx"):
            with self.subTest(listino=nome):
                esito = self.riconosci_listino(nome)
                self.assertNotIn(esito["adapter_id"], ADATTATORI_NATIVI)
                if esito["adapter_id"] is None:
                    # Until someone learns it, it stays unrecognized, and says
                    # so instead of guessing.
                    self.assertEqual(esito["state"], "AMBIGUO")
                    self.assertEqual(esito["confidence"], 0.0)

    def test_profilare_un_listino_vero_non_lo_tocca(self) -> None:
        """Profiling has rewritten a real price list during a test before."""

        percorso = LISTINI / "LISTINO BETULLA VALIDO FINO AL 28-07-26.xlsx"
        if not percorso.is_file():
            self.skipTest(f"Manca il listino {percorso}")
        prima = percorso.stat()

        registro.riconosci(inspect_sources.profile_file(percorso)["details"])

        dopo = percorso.stat()
        self.assertEqual((prima.st_size, prima.st_mtime_ns), (dopo.st_size, dopo.st_mtime_ns))


class IListiniVeriControIlRegistroCheVaAlNegozio(unittest.TestCase):
    """The same price lists, read with the shipped registry.

    The tests above run against the frozen copy
    (`tests/fixtures/adapters_nativi.json`) precisely so they don't fail when
    the engine learns a schema, but that leaves a gap: the frozen copy and
    the shipped registry can diverge, and nothing else here would notice.

    This class reads the registry the store actually receives. The shipped
    registry file is never rewritten by the engine — learned adapters go into
    `app/data/adattatori_imparati.json` instead — so this doesn't fail when
    the engine does its job.
    """

    maxDiff = None

    def riconosci_consegnato(self, nome: str) -> dict[str, Any]:
        percorso = LISTINI / nome
        if not percorso.is_file():
            self.skipTest(f"Manca il listino {percorso}")
        return registro.riconosci(profilo_del_file(percorso), ADAPTERS_CONSEGNATO)

    def test_i_due_listini_di_cipresso_si_leggono_tutti_e_due(self) -> None:
        """CIPRESSO sends two genuinely different schemas, not just a sheet
        name that varies: the header row differs too, and only one of the
        two has an ORDINE column.
        """

        datato = self.riconosci_consegnato("3listino_Cipresso.xlsx")
        self.assertEqual(datato["state"], "SCHEMA_NOTO")
        self.assertEqual(datato["adapter_id"], "cipresso_con_ordine_v1")

        # E l'altro schema non se lo prende la voce nuova.
        senza_ordine = self.riconosci_consegnato("Listino3_33.xlsx")
        self.assertEqual(senza_ordine["state"], "SCHEMA_NOTO")
        self.assertEqual(senza_ordine["adapter_id"], "cipresso_v1")

    def test_gli_altri_listini_veri_restano_dove_erano(self) -> None:
        """The extra entry must not claim any other supplier's document."""

        atteso = {
            "28.1 06-10lug.xlsx": "larice_v1",
            "31.1 27-31lug (1).xlsx": "larice_v1",
            "Copia di 30.1 20-24lug.xlsx": "larice_v1",
            "LISTINO BETULLA VALIDO FINO AL 28-07-26.xlsx": "betulla_v1",
            "OFFERTE AGOSTO 4.xlsx": "offerte_v1",
            "New Larice N°37(v.0).xls": "larice_canvass_v1",
        }
        for nome, adattatore in atteso.items():
            with self.subTest(listino=nome):
                esito = self.riconosci_consegnato(nome)
                self.assertEqual(esito["state"], "SCHEMA_NOTO")
                self.assertEqual(esito["adapter_id"], adattatore)

    def test_i_due_canvass_di_larice_non_si_prendono_il_documento_dell_altro(self) -> None:
        """LARICE sends two canvass formats with different schemas, and they
        must stay two distinct adapters.

        "New Larice N°37(v.0)" has real headers at row 11, EAN in column B,
        net price in K, 16 columns; the older canvass has no headers, keeps
        the EAN in column R and has at least 18 columns. The two signatures
        deliberately exclude each other — one requires its own headers, the
        other a column count the new format doesn't reach — since the
        supplier can revert to the older format from one week to the next.
        """

        nuovo = self.riconosci_consegnato("New Larice N°37(v.0).xls")
        self.assertEqual(nuovo["state"], "SCHEMA_NOTO")
        self.assertEqual(nuovo["adapter_id"], "larice_canvass_v1")

        vecchio = self.riconosci_consegnato("28.1 06-10lug.xlsx")
        self.assertEqual(vecchio["state"], "SCHEMA_NOTO")
        self.assertEqual(vecchio["adapter_id"], "larice_v1")

    def test_i_due_canvass_di_larice_sono_dello_stesso_fornitore(self) -> None:
        """Two entries, one supplier: downstream, LARICE stays LARICE.

        With two different `supplier_id` values the comparison would see two
        suppliers where there's one, splitting order quantities across two
        separate order files.
        """

        voci = {voce["id"]: voce for voce in registro.adattatori(ADAPTERS_CONSEGNATO)}
        self.assertEqual(voci["larice_v1"]["supplier_id"],
                         voci["larice_canvass_v1"]["supplier_id"])
        self.assertEqual(voci["larice_canvass_v1"]["supplier_id"], "larice")

    def test_il_canvass_nuovo_dichiara_dove_tiene_le_sue_offerte(self) -> None:
        """Threshold-with-reward promotions in the new canvass don't parse
        themselves.

        The new canvass writes the threshold text and the reward row in the
        same column E, and the guided mapping page can't declare that — it
        expects four separate columns for the "blocks" layout. If this
        declaration ever disappeared from the registry, LARICE's promotions
        would silently stop being detected.
        """

        voci = {voce["id"]: voce for voce in registro.adattatori(ADAPTERS_CONSEGNATO)}
        condizioni = voci["larice_canvass_v1"]["commercial_conditions"]
        self.assertEqual(condizioni["layout"], "blocchi")
        self.assertEqual(condizioni["fields"]["text"], "description")
        self.assertEqual(condizioni["fields"]["reward"], "description")
        righe_premio = voci["larice_canvass_v1"]["field_mapping"]["row_markers"]["reward_rows"]
        self.assertIs(righe_premio["orderable"], False)
        self.assertEqual(righe_premio["row_type"], "OMAGGIO")

    def test_i_due_schemi_di_cipresso_sono_dello_stesso_fornitore(self) -> None:
        """Two entries, one supplier: downstream, CIPRESSO stays CIPRESSO.

        Different `supplier_id` values would split one supplier's quantities
        across two order files.
        """

        voci = {voce["id"]: voce for voce in registro.adattatori(ADAPTERS_CONSEGNATO)}
        self.assertEqual(voci["cipresso_v1"]["supplier_id"],
                         voci["cipresso_con_ordine_v1"]["supplier_id"])
        self.assertEqual(
            registro.nome_del_fornitore_fra("cipresso", voci.values()), "CIPRESSO",
        )

    def test_tutti_e_due_gli_schemi_sanno_scrivere_l_ordine(self) -> None:
        """A schema that's recognized but can't write its order file would
        leave CIPRESSO out of order compilation every other week, silently."""

        voci = {voce["id"]: voce for voce in registro.adattatori(ADAPTERS_CONSEGNATO)}
        for identificativo in ("cipresso_v1", "cipresso_con_ordine_v1"):
            with self.subTest(adattatore=identificativo):
                scrittura = registro.scrittura_ordine(voci[identificativo])
                self.assertTrue(scrittura, "questo schema non sa scrivere il proprio ordine")
                self.assertEqual(scrittura.get("order_column"), "G")


class IlNomeDelFornitoreInOgniFrase(unittest.TestCase):
    """One rule for turning a supplier id into a display name, defined once.

    The launcher and the guided mapping page must both read the name the
    registry declares, instead of each formatting `supplier_id` on its own.
    """

    ADATTATORI = [
        {"id": "sapori_v1", "kind": "supplier", "supplier_id": "sapori_e_co",
         "display_name": "Sapori & Co."},
        {"id": "noce_xls_v1", "kind": "supplier", "supplier_id": "noce",
         "display_name": "NOCE listino Excel 97-2003"},
        {"id": "noce_csv_v1", "kind": "supplier", "supplier_id": "noce",
         "display_name": "NOCE"},
    ]

    def test_il_nome_dichiarato_si_legge_da_adattatori_gia_in_mano(self) -> None:
        self.assertEqual(
            registro.nome_del_fornitore_fra("sapori_e_co", self.ADATTATORI), "Sapori & Co.",
        )

    def test_a_parita_di_fornitore_vince_il_nome_piu_corto(self) -> None:
        """The longer name describes the document, not the supplier."""

        self.assertEqual(registro.nome_del_fornitore_fra("noce", self.ADATTATORI), "NOCE")

    def test_chi_non_e_dichiarato_non_esce_con_gli_underscore(self) -> None:
        self.assertEqual(
            registro.nome_del_fornitore_fra("nuovo_fornitore_1", self.ADATTATORI),
            "NUOVO FORNITORE 1",
        )
        self.assertEqual(registro.nome_del_fornitore_fra("", self.ADATTATORI), "FORNITORE")

    def test_la_mappatura_guidata_usa_la_stessa_regola(self) -> None:
        import schema_mapping

        self.assertEqual(
            schema_mapping.nome_dichiarato("sapori_e_co", self.ADATTATORI), "Sapori & Co.",
        )

    def test_le_frasi_del_lanciatore_usano_la_stessa_regola(self) -> None:
        import launcher

        self.assertEqual(launcher.nome_leggibile("noce"), "NOCE")
        self.assertEqual(launcher.nome_leggibile("nuovo_fornitore_1"), "NUOVO FORNITORE 1")


class UnPrezzoCalcolatoEUnPrezzoTests(unittest.TestCase):
    """A column written as a formula must read like any other numeric column.

    GINEPRO writes its discounted price as `=SUM(E4*(1-5%))`: the reader
    opens the file with `data_only=True` and finds 1.52. The profiler must
    read the same cached value instead of the formula's text, or
    `tipi_plausibili` reports a formula column of 4132 prices as 0%
    numeric, downgrading the schema and forcing manual mapping every week.
    """

    maxDiff = None

    def setUp(self) -> None:
        self.temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporanea.cleanup)
        self.radice = Path(self.temporanea.name)

    def listino(self, nome: str, prezzo: Any, *, righe: int = 12) -> Path:
        """Build a minimal price list where the net price is whatever is declared.

        `prezzo` returns the cell value: a number, text, or a
        (formula, cached_value) pair — the only form that matches a document
        that has actually gone through Excel, which openpyxl alone can't write.
        """

        intestazioni = ["CODICE", "Conf.", "DESCRIZIONE", "PREZZO NETTO", "PREZZO sc,5%", "EAN"]
        contenuto: list[list[Any]] = [intestazioni]
        calcolati: dict[str, Any] = {}
        for numero in range(righe):
            cella = prezzo(numero)
            if isinstance(cella, tuple):
                formula, valore = cella
                calcolati[f"E{numero + 2}"] = valore
                cella = formula
            contenuto.append([
                f"1616{numero:02d}", 6, f"BORAFRESH B/D {numero}", "1,6",
                cella, f"800241004{numero:04d}",
            ])
        percorso = scrivi_foglio(self.radice / nome, contenuto)
        if calcolati:
            scrivi_valori_calcolati(percorso, calcolati)
        return percorso

    def adattatore(self) -> dict[str, Any]:
        return {
            "id": "ginepro_v1", "kind": "supplier", "supplier_id": "ginepro", "display_name": "GINEPRO",
            "header_signature": {"kind": "headers", "sheet": "Foglio1", "header_row": 1,
                                 "data_start_row": 2,
                                 "required": ["codice", "conf", "descrizione", "prezzosc5", "ean"]},
            "field_mapping": {
                "sheet": "Foglio1", "header_row": 1, "data_start_row": 2,
                "columns": {"supplier_code": "CODICE", "pieces_per_carton": "Conf.",
                            "description": "DESCRIZIONE", "unit_price_net": "PREZZO sc,5%",
                            "ean": "EAN"},
                "order_column": "G", "ean_unavailable": False,
            },
        }

    def riconoscimento(self, percorso: Path) -> dict[str, Any]:
        registro_scritto = registro_di_prova(self.radice / "adapters.json", [self.adattatore()])
        return registro.riconosci(inspect_sources.profile_file(percorso)["details"], registro_scritto)

    def test_una_colonna_di_formule_numeriche_e_una_colonna_numerica(self) -> None:
        percorso = self.listino(
            "calcolato.xlsx", lambda numero: (f"=SUM(D{numero + 2}*(1-5%))", 1.52))
        esito = self.riconoscimento(percorso)

        tipi = next(voce for voce in esito["checks"] if voce["name"] == "tipi_plausibili")
        self.assertTrue(tipi["ok"], tipi["detail"])
        self.assertIn("100%", tipi["detail"])
        self.assertEqual(esito["state"], "SCHEMA_NOTO")

    def test_una_colonna_di_testo_continua_a_essere_bocciata(self) -> None:
        """The check must stay strict: without it, a price turned to text
        would pass as SCHEMA_NOTO 0.99 with zero offers read."""

        percorso = self.listino("testuale.xlsx", lambda numero: f"1,{numero:02d}")
        esito = self.riconoscimento(percorso)

        tipi = next(voce for voce in esito["checks"] if voce["name"] == "tipi_plausibili")
        self.assertFalse(tipi["ok"], tipi["detail"])
        self.assertIn("0%", tipi["detail"])
        self.assertEqual(esito["state"], "SCHEMA_VARIATO")

    def test_una_formula_che_restituisce_testo_resta_testo(self) -> None:
        """The check looks at what the formula evaluates to, not that it's a formula."""

        percorso = self.listino(
            "formula-testo.xlsx", lambda numero: (f'=CONCATENATE("1,",{numero})', f"1,{numero}"))
        # The column is all formulas, and the profiler read them: it's their
        # result that's text, not the fact that they're formulas.
        colonna = next(voce for voce in inspect_sources.profile_file(percorso)["details"]["sheets"][0]["columns"]
                       if voce["index"] == 5)
        self.assertEqual(colonna["formula_values"], {"text": 12})

        esito = self.riconoscimento(percorso)

        tipi = next(voce for voce in esito["checks"] if voce["name"] == "tipi_plausibili")
        self.assertFalse(tipi["ok"], tipi["detail"])
        self.assertEqual(esito["state"], "SCHEMA_VARIATO")

    def test_una_formula_mai_calcolata_non_e_un_prezzo(self) -> None:
        """A file Excel has never opened carries no cached formula value.

        There the column is genuinely unreadable — the reader finds `None` —
        and declaring it numeric would be the worse of the two possible lies.
        """

        percorso = self.listino("mai-calcolato.xlsx", lambda numero: f"=D{numero + 2}*0.95")
        colonna = next(voce for voce in inspect_sources.profile_file(percorso)["details"]["sheets"][0]["columns"]
                       if voce["index"] == 5)
        self.assertEqual(colonna["formula_values"], {"blank": 12})

        esito = self.riconoscimento(percorso)

        tipi = next(voce for voce in esito["checks"] if voce["name"] == "tipi_plausibili")
        self.assertFalse(tipi["ok"], tipi["detail"])

    def test_il_profilo_censisce_il_valore_in_cache_solo_dove_ci_sono_formule(self) -> None:
        con_formule = self.listino(
            "con-formule.xlsx", lambda numero: (f"=SUM(D{numero + 2}*(1-5%))", 1.52))
        senza_formule = self.listino("senza-formule.xlsx", lambda numero: 1.52 + numero)

        primo = inspect_sources.profile_file(con_formule)["details"]["sheets"][0]
        secondo = inspect_sources.profile_file(senza_formule)["details"]["sheets"][0]

        self.assertTrue(primo["formula_values_read"])
        colonna = next(voce for voce in primo["columns"] if voce["index"] == 5)
        self.assertEqual(colonna["types"].get("number"), None)
        self.assertEqual(colonna["formula_values"], {"number": 12})
        # A document with no formulas skips the second read, and doesn't
        # claim to have done it.
        self.assertNotIn("formula_values_read", secondo)
        self.assertFalse(any("formula_values" in voce for voce in secondo["columns"]))

    def test_l_impronta_per_forma_continua_a_distinguere_le_formule(self) -> None:
        """"What document is this" and "what does this column read as" are
        different questions.

        If shape fingerprints also counted formulas by their result, the real
        ACERO price list (18,644 formulas) would score 0.76 against LARICE's
        fingerprint, which has no formulas at all — one real supplier
        mistaken for another.
        """

        colonna = {"index": 15, "nonempty": 100, "types": {"formula": 100},
                   "formula_values": {"number": 100}}
        self.assertEqual(registro._quota(colonna, "number"), 0.0)
        self.assertEqual(registro._quota(colonna, "formula"), 1.0)
        self.assertEqual(registro._quota_leggibile(colonna, "number"), 1.0)


class DoveCominciaIlListinoTests(unittest.TestCase):
    """The marker declaring where data starts, as seen by the profiler."""

    maxDiff = None

    def setUp(self) -> None:
        self.temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporanea.cleanup)
        self.radice = Path(self.temporanea.name)

    def listino_con_blocco(self, promozionali: int = 4) -> Path:
        righe: list[list[Any]] = [["Articolo", "EAN", "Descrizione", "Imballo", "Prezzo", "Ordine"]]
        for numero in range(promozionali):
            righe.append([f"OMA{numero}", f"80000000009{numero:02d}", f"OMAGGIO {numero}", 12, 1.05, None])
        righe.append(["LISTINO", None, None, None, None, None])
        for numero in range(30):
            righe.append([f"FAT{numero}", f"80014807193{numero:02d}", f"AXO {numero}", 18, 0.94, None])
        return scrivi_foglio(self.radice / "listino.xlsx", righe, "Sheet")

    def test_il_profilo_dichiara_il_separatore_e_da_quale_riga_riparte(self) -> None:
        foglio = inspect_sources.profile_file(self.listino_con_blocco())["details"]["sheets"][0]

        separatori = foglio["section_breaks"]
        self.assertEqual([voce["row"] for voce in separatori], [6])
        self.assertEqual(separatori[0]["text"], "LISTINO")
        self.assertEqual(separatori[0]["letter"], "A")
        self.assertEqual(separatori[0]["data_from"], 7)

    def test_il_profilo_porta_le_righe_attorno_al_taglio(self) -> None:
        """Without this, whoever maps the file sees the promo block, not the list."""

        percorso = self.listino_con_blocco(promozionali=25)
        foglio = inspect_sources.profile_file(percorso)["details"]["sheets"][0]

        numeri = {voce["row"] for voce in foglio["section_rows"]}
        # The marker sits at row 27, past the first twenty rows the profile
        # sends anyway: without these extra rows the preview wouldn't see it.
        self.assertEqual([voce["row"] for voce in foglio["section_breaks"]], [27])
        self.assertLessEqual({25, 26, 27, 28, 29, 30}, numeri)

    def test_un_etichetta_in_mezzo_ai_dati_non_gonfia_il_profilo(self) -> None:
        """A repeated label mid-document (a group heading) is not a data-start marker."""

        righe: list[list[Any]] = []
        for numero in range(400):
            if numero % 5 == 0 and numero > 120:
                righe.append([f"GRUPPO {numero}", "x", None, None, None, None])
                continue
            righe.append([f"FAT{numero}", f"80014807193{numero:02d}", f"AXO {numero}", 18, 0.94, 1])
        foglio = inspect_sources.profile_file(scrivi_foglio(self.radice / "gruppi.xlsx", righe))["details"]["sheets"][0]

        self.assertEqual(foglio["section_breaks"], [])
        self.assertEqual(foglio["section_rows"], [])

    def test_la_verifica_dichiara_dove_ha_trovato_il_separatore(self) -> None:
        percorso = self.listino_con_blocco()
        voce = {
            "id": "quercia_v1", "kind": "supplier", "supplier_id": "quercia", "display_name": "QUERCIA",
            "header_signature": {"kind": "headers", "sheet": "Sheet", "header_row": 1,
                                 "data_start_row": 7,
                                 "required": ["articolo", "ean", "descrizione", "imballo", "prezzo"]},
            "field_mapping": {
                "sheet": "Sheet", "header_row": 1, "data_start_row": 7,
                "data_start_marker": {"column": "A", "equals": "LISTINO", "offset": 1},
                "columns": {"supplier_code": "Articolo", "ean": "EAN", "description": "Descrizione",
                            "pieces_per_carton": "Imballo", "unit_price_net": "Prezzo"},
                "order_column": "F",
            },
        }
        scritto = registro_di_prova(self.radice / "adapters.json", [voce])

        esito = registro.riconosci(inspect_sources.profile_file(percorso)["details"], scritto)

        righe_dati = next(voce for voce in esito["checks"] if voce["name"] == "righe_dati")
        self.assertTrue(righe_dati["ok"], righe_dati["detail"])
        self.assertIn("riga 6", righe_dati["detail"])
        self.assertEqual(esito["state"], "SCHEMA_NOTO")

    def test_un_separatore_che_compare_due_volte_e_gia_ambiguo_nel_profilo(self) -> None:
        foglio = {"header_rows": [
            {"row": 2, "values": ["LISTINO", None]},
            {"row": 9, "values": ["LISTINO", None]},
        ]}

        esito = registro._verifica_marcatore_dei_dati(
            {"column": "A", "equals": "LISTINO", "offset": 1}, foglio)

        self.assertFalse(esito["ok"])
        self.assertIn("ambiguo", esito["detail"])
        self.assertIn("2, 9", esito["detail"])

    def test_un_separatore_fuori_dalle_righe_profilate_non_si_dichiara_verificato(self) -> None:
        """The profile doesn't carry the whole document: saying so beats pretending."""

        esito = registro._verifica_marcatore_dei_dati(
            {"column": "A", "equals": "LISTINO", "offset": 1},
            {"header_rows": [{"row": 1, "values": ["Articolo", "EAN"]}]})

        self.assertTrue(esito["ok"])
        self.assertIn("si cerca sul documento intero", esito["detail"])


class ITreListiniSenzaAdattatoreTests(unittest.TestCase):
    """Two measurements against real price lists with no native adapter yet.

    QUERCIA and GINEPRO are the two files these checks were measured against;
    an engine that only works on fixtures built by the tests proves nothing.
    """

    maxDiff = None

    def listino(self, nome: str) -> Path:
        percorso = LISTINI / nome
        if not percorso.is_file():
            self.skipTest(f"Manca il listino {percorso}")
        return percorso

    def test_il_prezzo_calcolato_di_ginepro_e_una_colonna_numerica(self) -> None:
        """4132 formula cells `=SUM(E4*(1-5%))` must read as numeric, not 0%."""

        percorso = self.listino("ListinoGINEPRO 5 aggiornato al 25-02-2026.xlsx")
        foglio = profilo_del_file(percorso)["sheets"][0]

        colonna = next(voce for voce in foglio["columns"] if voce["letter"] == "F")
        self.assertGreater(colonna["types"].get("formula", 0), 4000)
        self.assertEqual(set(colonna["formula_values"]), {"number"})
        self.assertGreater(registro._quota_leggibile(colonna, "number"), 0.99)
        # Column E, which the supplier writes as Italian-formatted text
        # ("1,6"), stays text: the profiler doesn't convert anything, so
        # declaring it as a price would still be caught as before.
        prezzo_testuale = next(voce for voce in foglio["columns"] if voce["letter"] == "E")
        self.assertNotIn("formula_values", prezzo_testuale)
        self.assertLess(registro._quota_leggibile(prezzo_testuale, "number", 1), 0.5)

    def test_il_separatore_di_quercia_e_nel_profilo_con_le_righe_attorno(self) -> None:
        """`A68 = 'LISTINO'`: the only data-start marker in the whole file."""

        percorso = self.listino("LISTINO QUERCIA AGGIORNATO DEL 06-08-2026.xlsx")
        foglio = profilo_del_file(percorso)["sheets"][0]

        separatori = {voce["row"]: voce for voce in foglio["section_breaks"]}
        self.assertIn(68, separatori)
        self.assertEqual(separatori[68]["text"], "LISTINO")
        self.assertEqual(separatori[68]["letter"], "A")
        self.assertEqual(separatori[68]["data_from"], 69)
        numeri = {voce["row"] for voce in foglio["section_rows"]}
        # Rows 66-74 must be in the profile: sending only 1-20, 2094-2098 and
        # 4180-4190 would cover everything except where the list actually starts.
        self.assertLessEqual({66, 67, 68, 69, 70, 71}, numeri)


class DueScrittureInsiemeNonSiCancellanoAVicenda(unittest.TestCase):
    """Two writes racing on the same registry must not erase each other.

    The file write itself is atomic (temp file plus `os.replace`), so no read
    ever sees a truncated registry. But two writes starting from the same
    document, without a lock around the read-modify-write cycle, would have
    the second overwrite erase the first write's entry.

    This isn't a lab-only scenario: the service is a `ThreadingHTTPServer`,
    `impara_adattatore` runs as its own process during the pipeline, and
    every comparator started from the same folder writes the same learned
    registry file.
    """

    def setUp(self) -> None:
        self.cartella = tempfile.TemporaryDirectory()
        self.radice = Path(self.cartella.name)
        self.addCleanup(self.cartella.cleanup)
        self.spedito = registro_di_prova(self.radice / "adapters.json", [])

    @staticmethod
    def voce(numero: int) -> dict[str, Any]:
        return {"id": f"forn{numero}_v1", "supplier_id": f"forn{numero}", "kind": "supplier",
                "header_signature": {"kind": "headers", "required": ["a", "b", "c"]}}

    def imparate(self) -> list[str]:
        documento = registro.percorso_imparato(self.spedito)
        if not documento.is_file():
            return []
        return [str(v.get("id") or "") for v in json.loads(
            documento.read_text(encoding="utf-8"))["adapters"]]

    def test_dieci_scritture_insieme_restano_dieci(self) -> None:
        quante = 10
        barriera = threading.Barrier(quante)
        guasti: list[str] = []

        def scrivi(numero: int) -> None:
            try:
                barriera.wait(timeout=10)
                registro.scrivi_adattatore(self.voce(numero), self.spedito)
            except Exception as errore:  # noqa: BLE001 - any exception must be reported
                guasti.append(f"{type(errore).__name__}: {errore}")

        fili = [threading.Thread(target=scrivi, args=(numero,)) for numero in range(quante)]
        for filo in fili:
            filo.start()
        for filo in fili:
            filo.join(timeout=30)

        self.assertEqual(guasti, [])
        self.assertEqual(sorted(self.imparate()), sorted(f"forn{n}_v1" for n in range(quante)))

    def test_un_turno_abbandonato_non_blocca_il_registro_per_sempre(self) -> None:
        """A stale lock must be reclaimed, or a killed process (antivirus,
        power loss) would leave the registry permanently locked with no
        visible symptom other than learning silently stopping.
        """

        documento = registro.percorso_imparato(self.spedito)
        documento.parent.mkdir(parents=True, exist_ok=True)
        lucchetto = documento.with_name(documento.name + ".lock")
        lucchetto.write_text("99999 0\n", encoding="utf-8")
        # Older than the timeout: whoever held it is gone.
        vecchio = time.time() - registro.TURNO_ABBANDONATO - 5
        os.utime(lucchetto, (vecchio, vecchio))

        registro.scrivi_adattatore(self.voce(1), self.spedito)

        self.assertEqual(self.imparate(), ["forn1_v1"])
        self.assertFalse(lucchetto.exists(), "il turno preso va restituito")

    def test_un_turno_di_un_altro_non_fa_perdere_la_voce(self) -> None:
        """If the lock isn't acquired within the timeout, the write proceeds
        anyway: the worst case reverts to the pre-lock behavior, not
        something worse. A just-confirmed adapter must not be lost because
        another process is slow."""

        documento = registro.percorso_imparato(self.spedito)
        documento.parent.mkdir(parents=True, exist_ok=True)
        lucchetto = documento.with_name(documento.name + ".lock")
        lucchetto.write_text("1 0\n", encoding="utf-8")  # fresh: not stale

        registro.scrivi_adattatore(self.voce(2), self.spedito)

        self.assertEqual(self.imparate(), ["forn2_v1"])
        # The other process's lock stays theirs: nothing takes it away.
        self.assertTrue(lucchetto.exists())

    def test_il_lucchetto_non_resta_in_giro_dopo_una_scrittura_riuscita(self) -> None:
        registro.scrivi_adattatore(self.voce(3), self.spedito)

        documento = registro.percorso_imparato(self.spedito)
        self.assertFalse(documento.with_name(documento.name + ".lock").exists())

    def test_e_nemmeno_dopo_una_scrittura_rifiutata(self) -> None:
        """An entry without an "id" is rejected, but the lock must still be
        released, or the first rejection would lock the registry for the
        full timeout."""

        registro.scrivi_adattatore(self.voce(4), self.spedito)
        with self.assertRaises(ValueError):
            registro.scrivi_adattatore(
                {"id": "forn4_v1", "supplier_id": "un_altro", "kind": "supplier"}, self.spedito,
            )

        documento = registro.percorso_imparato(self.spedito)
        self.assertFalse(documento.with_name(documento.name + ".lock").exists())


class IlCandidatoMancatoPerUnPelo(unittest.TestCase):
    """"Unrecognized" versus "this supplier's, missing just this one thing".

    A document missing one required header, with the rest present and in
    place, should be reported as a near-miss on a specific adapter instead of
    the generic "no known signature is sufficient" — without ever reading it
    as that supplier's file. The state stays AMBIGUO in every case here;
    only the message changes.
    """

    maxDiff = None

    def setUp(self) -> None:
        self.cartella = tempfile.TemporaryDirectory()
        self.radice = Path(self.cartella.name)
        self.addCleanup(self.cartella.cleanup)

    INTESTAZIONI = ["EAN", "CODART", "ORDINE", "DESCRIZIONE", "PREZZO"]

    def registro_con_cinque_obbligatorie(self) -> Path:
        return registro_di_prova(self.radice / "adapters.json", [
            {"id": "tizio_v1", "supplier_id": "tizio", "kind": "supplier",
             "display_name": "TIZIO",
             "header_signature": {
                 "kind": "headers", "sheet": "FIRST", "header_row": 1, "data_start_row": 2,
                 "columns": {"ean": 1, "codart": 2, "ordine": 3, "descrizione": 4, "prezzo": 5},
                 "required": ["codart", "descrizione", "ean", "ordine", "prezzo"],
                 "known": ["codart", "descrizione", "ean", "ordine", "prezzo"],
             },
             "order_write": {"sheet": "FIRST", "header_row": 1, "data_start_row": 2,
                             "order_column": "C", "expected_header": "ORDINE"}},
        ])

    def listino(self, intestazioni: list[Any]) -> Path:
        return scrivi_foglio(self.radice / "listino.xlsx", [
            list(intestazioni),
            [8000000000001, "A1", None, "SAPONE", 1.5],
        ], "Foglio1")

    def test_una_intestazione_svuotata_non_e_un_documento_sconosciuto(self) -> None:
        intestazioni = list(self.INTESTAZIONI)
        intestazioni[2] = None                      # cell C1, "ORDINE"
        esito = registro.riconosci(
            profilo_del_file(self.listino(intestazioni)), self.registro_con_cinque_obbligatorie(),
        )

        # The state doesn't change: a document missing a column isn't read.
        self.assertEqual(esito["state"], "AMBIGUO")
        self.assertIsNone(esito["adapter_id"])
        # What changes is what the message reports.
        self.assertEqual(esito["quasi_adapter_id"], "tizio_v1")
        self.assertEqual(esito["quasi_missing"], [{"header": "ORDINE", "column": "C"}])
        self.assertEqual(esito["quasi_present"], 4)
        frase = esito["evidence"][0]
        self.assertIn("TIZIO", frase)
        self.assertIn("«ORDINE»", frase)
        self.assertIn("colonna C", frase)
        self.assertIn("Excel", frase)

    def test_tre_intestazioni_mancanti_non_sono_un_pelo(self) -> None:
        """Claiming "this is TIZIO's" from two matching headers out of five
        would be a confident lie, worse than "unrecognized"."""

        intestazioni = [None, None, None, "DESCRIZIONE", "PREZZO"]
        esito = registro.riconosci(
            profilo_del_file(self.listino(intestazioni)), self.registro_con_cinque_obbligatorie(),
        )

        self.assertEqual(esito["state"], "AMBIGUO")
        self.assertEqual(esito["evidence"], ["Nessuna firma nota sufficiente"])
        self.assertNotIn("quasi_adapter_id", esito)

    def test_le_intestazioni_che_restano_devono_stare_dove_il_registro_dice(self) -> None:
        """The defense against false positives.

        Another supplier's document that happens to share four column names
        almost never has them in the same positions too: without this check,
        "looks like TIZIO's" would match nearly anyone.
        """

        # Same headers minus one, but all shifted by one column.
        intestazioni = [None, "EAN", "CODART", "DESCRIZIONE", "PREZZO"]
        esito = registro.riconosci(
            profilo_del_file(self.listino(intestazioni)), self.registro_con_cinque_obbligatorie(),
        )

        self.assertEqual(esito["evidence"], ["Nessuna firma nota sufficiente"])

    def test_il_listino_che_si_legge_non_passa_di_qui(self) -> None:
        """A complete document stays SCHEMA_NOTO: the near-miss message must
        not appear where nothing is missing."""

        esito = registro.riconosci(
            profilo_del_file(self.listino(self.INTESTAZIONI)), self.registro_con_cinque_obbligatorie(),
        )

        self.assertEqual(esito["state"], "SCHEMA_NOTO")
        self.assertEqual(esito["adapter_id"], "tizio_v1")


class QuelloCheSImparaNonCancellaQuelloCheSiSpedisce(unittest.TestCase):
    """`__locale` adapters, and who wins when there are two candidates.

    A mapping confirmed for a shipped supplier must be written under a
    distinct id, not the shipped one, or it would silently shadow everything
    the shipped entry declares that the guided mapping page doesn't ask for.
    """

    maxDiff = None

    def setUp(self) -> None:
        self.cartella = tempfile.TemporaryDirectory()
        self.radice = Path(self.cartella.name)
        self.addCleanup(self.cartella.cleanup)

    def test_l_id_di_chi_impara_sopra_uno_spedito_porta_dentro_quello_spedito(self) -> None:
        self.assertEqual(registro.id_locale("betulla_v1"), "betulla_v1__locale")
        self.assertEqual(registro.adattatore_base("betulla_v1__locale"), "betulla_v1")

    def test_un_fornitore_imparato_da_zero_resta_se_stesso(self) -> None:
        """`adattatore_base` must not invent a derivation where there is none."""

        self.assertEqual(registro.adattatore_base("quercia_v1"), "quercia_v1")
        self.assertEqual(registro.adattatore_base(""), "")
        self.assertEqual(registro.adattatore_base(None), "")

    def test_gli_id_spediti_si_leggono_senza_l_imparato(self) -> None:
        """Needed to know whether a write would shadow a shipped entry."""

        spedito = registro_di_prova(self.radice / "adapters.json", [
            {"id": "tizio_v1", "supplier_id": "tizio"},
        ])
        registro_di_prova(registro.percorso_imparato(spedito), [
            {"id": "caio_v1", "supplier_id": "caio"},
        ])

        self.assertEqual(registro.identificativi_spediti(spedito), {"tizio_v1"})
        # The effective registry has both: two different questions, and
        # conflating them would reintroduce the shadowing bug.
        voci, motivo = registro.adattatori_effettivi(spedito)
        self.assertIsNone(motivo)
        self.assertEqual({voce["id"] for voce in voci}, {"tizio_v1", "caio_v1"})

    def firma(self, foglio: str, riga: int) -> dict[str, Any]:
        return {
            "kind": "headers", "sheet": foglio, "header_row": riga, "data_start_row": riga + 1,
            "required": ["codart", "descrizione", "prezzo"],
            "known": ["codart", "descrizione", "prezzo"],
        }

    def mappatura(self, foglio: str, riga: int) -> dict[str, Any]:
        return {
            "sheet": foglio, "header_row": riga, "data_start_row": riga + 1,
            "columns": {"supplier_code": "COD.ART.", "description": "DESCRIZIONE",
                        "unit_price_net": "PREZZO"},
        }

    def test_fra_due_candidati_prende_il_documento_quello_che_lo_legge_davvero(self) -> None:
        """The rule that makes shipped and learned adapters coexist safely.

        The same document resembles two adapters: the shipped one, declaring
        a sheet name that this document doesn't have, and the learned one on
        top of it, declaring the right one. The winner must be whichever one
        actually passes the checks, not whichever comes first in the list.
        """

        listino = scrivi_foglio(self.radice / "listino.xlsx", [
            ["COD.ART.", "DESCRIZIONE", "PREZZO"],
            ["A1", "SAPONE", 1.5],
        ], "Listino al 21-08-2026")
        spedita = {"id": "tizio_v1", "supplier_id": "tizio", "kind": "supplier",
                   "header_signature": self.firma("Listino", 1),
                   "field_mapping": self.mappatura("Listino", 1)}
        spedito = registro_di_prova(self.radice / "adapters.json", [spedita])
        registro_di_prova(registro.percorso_imparato(spedito), [
            {"id": "tizio_v1__locale", "supplier_id": "tizio", "kind": "supplier",
             "derivato_da": "tizio_v1", "sopra_spedito": registro.sopra_spedito_di(spedita),
             "header_signature": self.firma("Listino al 21-08-2026", 1),
             "field_mapping": self.mappatura("Listino al 21-08-2026", 1)},
        ])

        esito = registro.riconosci(profilo_del_file(listino), spedito)

        self.assertEqual(esito["adapter_id"], "tizio_v1__locale")
        self.assertEqual(esito["state"], "SCHEMA_NOTO")

    def test_a_parita_piena_vince_quello_imparato_qui(self) -> None:
        """When both candidates read the document cleanly, the learned one
        wins: it's the most recent answer, given by someone who had the
        document in front of them.

        This matters specifically for a learned entry that changes only
        where the order column is written: it reads the document exactly
        like the shipped one, so the two tie on every other check, and the
        tie-breaker must still favor the learned entry.
        """

        listino = scrivi_foglio(self.radice / "listino.xlsx", [
            ["COD.ART.", "DESCRIZIONE", "PREZZO"],
            ["A1", "SAPONE", 1.5],
        ], "Listino")
        spedita = {"id": "tizio_v1", "supplier_id": "tizio", "kind": "supplier",
                   "header_signature": self.firma("Listino", 1),
                   "field_mapping": self.mappatura("Listino", 1)}
        spedito = registro_di_prova(self.radice / "adapters.json", [spedita])
        registro_di_prova(registro.percorso_imparato(spedito), [
            {"id": "tizio_v1__locale", "supplier_id": "tizio", "kind": "supplier",
             "derivato_da": "tizio_v1", "sopra_spedito": registro.sopra_spedito_di(spedita),
             "header_signature": self.firma("Listino", 1),
             "field_mapping": self.mappatura("Listino", 1)},
        ])

        esito = registro.riconosci(profilo_del_file(listino), spedito)

        self.assertEqual(esito["adapter_id"], "tizio_v1__locale")
        self.assertEqual(esito["state"], "SCHEMA_NOTO")

    def test_fra_famiglie_diverse_a_parita_resta_l_ordine_del_registro(self) -> None:
        """The "learned wins ties" rule applies only between two versions of
        the SAME adapter.

        Between a shipped adapter and another supplier's learned one there's
        no older-or-newer relationship — only the registry's own order, which
        doesn't change. Extending the rule here would let a `caio_v1__locale`
        outrank a `tizio_v1` shipped entry that reads the document just as
        well, purely for having been learned more recently.
        """

        listino = scrivi_foglio(self.radice / "listino.xlsx", [
            ["COD.ART.", "DESCRIZIONE", "PREZZO"],
            ["A1", "SAPONE", 1.5],
        ], "Listino")
        spedito = registro_di_prova(self.radice / "adapters.json", [
            {"id": "tizio_v1", "supplier_id": "tizio", "kind": "supplier",
             "header_signature": self.firma("Listino", 1),
             "field_mapping": self.mappatura("Listino", 1)},
            {"id": "caio_v1", "supplier_id": "caio", "kind": "supplier",
             "header_signature": self.firma("Listino", 1),
             "field_mapping": self.mappatura("Listino", 1)},
        ])
        registro_di_prova(registro.percorso_imparato(spedito), [
            {"id": "caio_v1__locale", "supplier_id": "caio", "kind": "supplier",
             "derivato_da": "caio_v1",
             "header_signature": self.firma("Listino", 1),
             "field_mapping": self.mappatura("Listino", 1)},
        ])

        esito = registro.riconosci(profilo_del_file(listino), spedito)

        self.assertEqual(esito["adapter_id"], "tizio_v1")

    def test_la_voce_locale_e_la_seconda_versione_della_spedita(self) -> None:
        """Rereading a month later must still find the starting version."""

        spedito = registro_di_prova(self.radice / "adapters.json", [
            {"id": "tizio_v1", "supplier_id": "tizio", "kind": "supplier",
             "commercial_rules": {"price_basis": "net_unit"},
             "header_signature": self.firma("Listino", 1)},
        ])

        esito = registro.scrivi_adattatore(
            {"id": "tizio_v1__locale", "supplier_id": "tizio", "kind": "supplier",
             "derivato_da": "tizio_v1",
             "header_signature": self.firma("Listino al 21-08-2026", 1)},
            spedito,
        )

        self.assertEqual(esito["schema_version"], 2)
        imparati = json.loads(registro.percorso_imparato(spedito).read_text(encoding="utf-8"))
        voce = imparati["adapters"][0]
        self.assertEqual(len(voce["previous_versions"]), 1)
        self.assertEqual(voce["previous_versions"][0]["id"], "tizio_v1")
        # The shipped entry stays untouched.
        spedite = json.loads(spedito.read_text(encoding="utf-8"))["adapters"]
        self.assertEqual([voce["id"] for voce in spedite], ["tizio_v1"])


class IntestazioniDeiLettoriDedicati(unittest.TestCase):
    """Dedicated readers must expect what the REGISTRY declares, not a
    separate hardcoded header list.

    `normalizza` ignores case and punctuation, so the registry recognizes a
    header like "Ordine" written instead of "ORDINE". A dedicated reader
    comparing hardcoded names letter-for-letter would reject the same file
    the registry accepted — two definitions of the same thing, and these
    tests keep it to one.
    """

    def setUp(self) -> None:
        self.radice = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.radice, True)

    def test_betulla_letto_con_la_cassa_cambiata(self) -> None:
        righe = [["8000000000001", "C-001", None, "Prodotto Alfa", 6, 1.25, 60, 22, 0]]
        com_e = scrivi_foglio(self.radice / "betulla.xlsx", [INTESTAZIONI_BETULLA, *righe], "Listino")
        cambiato = scrivi_foglio(self.radice / "betulla_cassa.xlsx", [
            ["EAN", "CodArt", "Ordine", "Descr.Commerciale", "PzCt",
             "Cessione", "Pedana", "Iva", "Totali"],
            *righe,
        ], "Listino")

        self.assertEqual(
            prepare_sources.read_betulla(cambiato), prepare_sources.read_betulla(com_e)
        )

    def test_betulla_letto_con_la_punteggiatura_cambiata(self) -> None:
        """"Cod.Art.", "COD ART", "Cod. Art.": `normalizza`'s contract applies
        to the dedicated reader too."""

        percorso = scrivi_foglio(self.radice / "betulla_punti.xlsx", [
            ["EAN", "Cod. Art.", "ORDINE", "Descr.Commerciale", "PzCt",
             "Cessione", "Pedana", "Iva", "TOTALI"],
            ["8000000000001", "C-001", None, "Prodotto Alfa", 6, 1.25, 60, 22, 0],
        ], "Listino")

        self.assertEqual(len(prepare_sources.read_betulla(percorso)), 1)

    def test_a_betulla_manca_una_colonna_e_il_motivo_si_legge(self) -> None:
        """The rejection stays, but now names which column is missing.

        "Schema BETULLA non riconosciuto" alone never told anyone the actual
        problem was a lowercase header.
        """

        percorso = scrivi_foglio(self.radice / "betulla_monco.xlsx", [
            ["EAN", "CodArt", "Descr.Commerciale", "PzCt", "Cessione"],
            ["8000000000001", "C-001", "Prodotto Alfa", 6, 1.25],
        ], "Listino")

        with self.assertRaises(ValueError) as errore:
            prepare_sources.read_betulla(percorso)

        self.assertIn("Schema BETULLA non riconosciuto", str(errore.exception))
        self.assertIn("ordine", str(errore.exception))
        self.assertIn("CodArt", str(errore.exception))

    def test_le_obbligatorie_di_betulla_le_dichiara_il_registro(self) -> None:
        """No header list hardcoded in the reader's own code."""

        self.assertEqual(
            sorted(prepare_sources.intestazioni_obbligatorie("betulla_v1")),
            ["cessione", "codart", "ean", "ordine", "pzct"],
        )
        self.assertNotIn('"CodArt"', PERCORSO_LETTORI.read_text(encoding="utf-8"))

    def test_noce_csv_letto_con_la_cassa_cambiata(self) -> None:
        """Data rows must actually be read, not just the header row checked.

        The CSV reads columns by name; if only the header check tolerated
        case changes while the row reader didn't, every field would come
        back empty — a supplier read with zero usable rows and no warning.
        """

        corpo = "12,8000000000002,Prodotto Beta,Cartone,Disponibile,\"1,50\",x 6,\n"
        com_e = self.radice / "noce.csv"
        com_e.write_text(
            "catalog_page,ean,product,packaging,availability,price,unit,variation\n" + corpo,
            encoding="utf-8",
        )
        cambiato = self.radice / "noce_cassa.csv"
        cambiato.write_text(
            "Catalog_Page,EAN,Product,Packaging,Availability,Price,Unit,Variation\n" + corpo,
            encoding="utf-8",
        )

        letto = prepare_sources.read_noce(cambiato)

        self.assertEqual(letto, prepare_sources.read_noce(com_e))
        self.assertEqual(letto[0]["ean"], "8000000000002")
        self.assertTrue(letto[0]["usable"])

    def test_al_csv_noce_manca_una_colonna_e_il_motivo_si_legge(self) -> None:
        percorso = self.radice / "noce_monco.csv"
        percorso.write_text("ean,product,price\n8000000000002,Beta,\"1,50\"\n", encoding="utf-8")

        with self.assertRaises(ValueError) as errore:
            prepare_sources.read_noce(percorso)

        self.assertIn("Schema CSV Noce non riconosciuto", str(errore.exception))
        self.assertIn("packaging", str(errore.exception))


if __name__ == "__main__":
    unittest.main()


class LoSpeditoPiuRecenteVince(unittest.TestCase):
    """A learned entry on top of a shipped one stays valid only as long as
    the shipped entry it was learned against hasn't changed since.

    Without this, a manually confirmed mapping would keep shadowing an
    improved shipped adapter until someone finds and deletes the learned
    entry by hand. The engine must retire it automatically instead.
    """

    maxDiff = None

    def setUp(self) -> None:
        self.cartella = tempfile.TemporaryDirectory()
        self.radice = Path(self.cartella.name)
        self.addCleanup(self.cartella.cleanup)
        self.spedita = {"id": "tizio_v1", "supplier_id": "tizio", "kind": "supplier",
                        "display_name": "TIZIO",
                        "header_signature": {"kind": "headers", "required": ["a", "b", "c"]},
                        "order_write": {"sheet": "FIRST", "data_start_row": 2, "order_column": "C"}}
        self.spedito = registro_di_prova(self.radice / "adapters.json", [self.spedita])

    def ids_effettivi(self) -> list[str]:
        voci, motivo = registro.adattatori_effettivi(self.spedito)
        self.assertIsNone(motivo)
        return [voce["id"] for voce in voci]

    def imparato(self) -> dict[str, Any]:
        return json.loads(registro.percorso_imparato(self.spedito).read_text(encoding="utf-8"))

    def test_chi_scrive_sopra_una_spedita_porta_il_timbro_di_quella(self) -> None:
        registro.scrivi_adattatore(
            {"id": "tizio_v1__locale", "supplier_id": "tizio", "kind": "supplier",
             "order_write": {"sheet": "FIRST", "data_start_row": 2, "order_column": "H"}},
            self.spedito,
        )

        voce = self.imparato()["adapters"][0]
        self.assertEqual(voce["sopra_spedito"], registro.sopra_spedito_di(self.spedita))
        self.assertEqual(self.ids_effettivi(), ["tizio_v1", "tizio_v1__locale"])
        self.assertEqual(registro.adattatori_superati(self.spedito), [])

    def test_chi_impara_da_zero_non_porta_nessun_timbro(self) -> None:
        registro.scrivi_adattatore({"id": "caio_v1", "supplier_id": "caio"}, self.spedito)

        self.assertNotIn("sopra_spedito", self.imparato()["adapters"][0])
        self.assertEqual(self.ids_effettivi(), ["tizio_v1", "caio_v1"])

    def test_se_la_spedita_cambia_dopo_vince_la_spedita(self) -> None:
        """The fix finally lands: the shipped entry changes, and the learned
        one on top of the old version stops shadowing it."""

        registro.scrivi_adattatore(
            {"id": "tizio_v1__locale", "supplier_id": "tizio", "kind": "supplier"}, self.spedito,
        )
        self.assertEqual(self.ids_effettivi(), ["tizio_v1", "tizio_v1__locale"])

        corretta = {**self.spedita, "order_write": {**self.spedita["order_write"], "order_column": "D"}}
        registro_di_prova(self.spedito, [corretta])

        self.assertEqual(self.ids_effettivi(), ["tizio_v1"])
        superate = registro.adattatori_superati(self.spedito)
        self.assertEqual([voce["id"] for voce in superate], ["tizio_v1__locale"])
        self.assertEqual(superate[0]["base"], "tizio_v1")
        self.assertIn("è cambiato dopo", superate[0]["motivo"])

    def test_una_voce_senza_timbro_sopra_una_spedita_e_superata(self) -> None:
        """Covers learned entries with no stamp linking them to a shipped
        version — including ones that still carry the shipped adapter's own
        id, which otherwise would need deleting by hand."""

        registro_di_prova(registro.percorso_imparato(self.spedito), [
            {"id": "tizio_v1__locale", "supplier_id": "tizio", "derivato_da": "tizio_v1"},
            {"id": "tizio_v1", "supplier_id": "tizio", "header_signature": {"kind": "headers"}},
            {"id": "caio_v1", "supplier_id": "caio"},
        ])

        self.assertEqual(self.ids_effettivi(), ["tizio_v1", "caio_v1"])
        # And the shipped entry served is the actual shipped one, not the learned copy.
        self.assertEqual(registro.adattatore("tizio_v1", self.spedito)["order_write"]["order_column"], "C")
        self.assertEqual(
            sorted(voce["id"] for voce in registro.adattatori_superati(self.spedito)),
            ["tizio_v1", "tizio_v1__locale"],
        )

    def test_metterle_da_parte_le_toglie_dal_file_senza_cancellarle(self) -> None:
        registro_di_prova(registro.percorso_imparato(self.spedito), [
            {"id": "tizio_v1__locale", "supplier_id": "tizio", "display_name": "TIZIO"},
            {"id": "caio_v1", "supplier_id": "caio"},
        ])

        messe = registro.metti_da_parte_le_superate(self.spedito, quando="2026-09-05T10:00:00+00:00")

        self.assertEqual([voce["id"] for voce in messe], ["tizio_v1__locale"])
        self.assertEqual(messe[0]["display_name"], "TIZIO")
        documento = self.imparato()
        self.assertEqual([voce["id"] for voce in documento["adapters"]], ["caio_v1"])
        da_parte = documento["adapters_messi_da_parte"]
        self.assertEqual([voce["id"] for voce in da_parte], ["tizio_v1__locale"])
        self.assertEqual(da_parte[0]["messo_da_parte_il"], "2026-09-05T10:00:00+00:00")
        self.assertTrue(da_parte[0]["messo_da_parte_perche"])
        # The second time there's nothing left to move, and the file is untouched.
        prima = registro.percorso_imparato(self.spedito).read_bytes()
        self.assertEqual(registro.metti_da_parte_le_superate(self.spedito), [])
        self.assertEqual(registro.percorso_imparato(self.spedito).read_bytes(), prima)

    def test_riconfermare_dalla_mappatura_rimette_in_gioco_la_voce(self) -> None:
        """The full cycle: shipped entry changes, the learned one is
        retired, the user confirms again, and the new learned entry carries
        the current shipped version's stamp."""

        registro.scrivi_adattatore({"id": "tizio_v1__locale", "supplier_id": "tizio"}, self.spedito)
        corretta = {**self.spedita, "display_name": "TIZIO S.R.L."}
        registro_di_prova(self.spedito, [corretta])
        registro.metti_da_parte_le_superate(self.spedito)
        self.assertEqual(self.ids_effettivi(), ["tizio_v1"])

        registro.scrivi_adattatore({"id": "tizio_v1__locale", "supplier_id": "tizio"}, self.spedito)

        self.assertEqual(self.ids_effettivi(), ["tizio_v1", "tizio_v1__locale"])
        self.assertEqual(self.imparato()["adapters"][0]["sopra_spedito"], registro.sopra_spedito_di(corretta))
        self.assertEqual(registro.adattatori_superati(self.spedito), [])

    def test_senza_l_imparato_o_con_l_imparato_rotto_non_succede_niente(self) -> None:
        self.assertEqual(registro.adattatori_superati(self.spedito), [])
        self.assertEqual(registro.metti_da_parte_le_superate(self.spedito), [])
        registro.percorso_imparato(self.spedito).write_text("{non json", encoding="utf-8")
        self.assertEqual(registro.adattatori_superati(self.spedito), [])
        self.assertEqual(registro.metti_da_parte_le_superate(self.spedito), [])

