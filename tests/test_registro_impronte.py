"""La memoria degli schemi: impronte, riconoscimento, scrittura versionata.

Il programma finito gira da solo. Quando un fornitore cambia il listino non ci
sarà nessuno a rimettere le mani nel codice: la regola deve stare nel registro
e il codice deve limitarsi ad applicarla. Questi collaudi provano proprio
questo — che il riconoscimento viene dal registro e non da condizioni scritte
dentro una funzione — e che quello che il programma non riconosce lo dice,
invece di far finta di niente.

Le prove sui listini veri sono di **sola lettura**: profilare non riscrive
niente. Le prove che scrivono lavorano su una copia del registro in una
cartella temporanea.
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
# ⚠ Il registro di queste prove e' una **copia congelata**, non
# `references/adapters.json`.  Quel file il programma se lo riscrive da solo:
# il 14 agosto 2026 la prima run vera ha imparato lo schema CIPRESSO della
# settimana e ha riscritto `cipresso_v1` — comportamento voluto — e nove prove
# appuntate ai suoi valori sono diventate rosse senza che niente fosse rotto.
# Una suite che diventa rossa quando il programma fa il suo mestiere insegna a
# non guardarla.  Qui si prova che gli adattatori NATIVI riconoscono i listini
# storici: e' una proprieta' del registro consegnato, e va provata su quello.
ADAPTERS = SKILL_ROOT / "tests" / "fixtures" / "adapters_nativi.json"
ADAPTERS_CONSEGNATO = SKILL_ROOT / "references" / "adapters.json"
for cartella in (SCRIPTS, APP):
    if str(cartella) not in sys.path:
        sys.path.insert(0, str(cartella))

import inspect_sources  # noqa: E402
import registro  # noqa: E402
import prepare_sources  # noqa: E402


_REGISTRO_VERO = registro.REGISTRO

# Il file dei lettori dedicati, per provare che le intestazioni di un fornitore
# non ci sono scritte dentro: la regola sta nel registro (regola 4).
PERCORSO_LETTORI = SCRIPTS / "prepare_sources.py"


def setUpModule() -> None:
    """Anche le chiamate senza percorso guardano la copia congelata.

    `registro.riconosci(profilo)` senza percorso legge il registro predefinito,
    ed e' cosi' che tre prove si riprendevano `references/adapters.json` dopo
    che le altre erano gia' state spostate sulla copia.
    """

    registro.REGISTRO = ADAPTERS


def tearDownModule() -> None:
    registro.REGISTRO = _REGISTRO_VERO


# I listini veri stanno fuori dal progetto: si spostano con questa variabile
# d'ambiente senza toccare il codice del collaudo.
LISTINI = Path(os.environ.get("LISTINI_STORICI", str(SKILL_ROOT / "listini-storici")))

# Le intestazioni del listino Noce, nell'ordine in cui stanno nel file
# vero: la colonna A e' vuota, l'EAN comincia dalla B.
INTESTAZIONI_NOCE = [
    None, "codice_a_barre", "codice", "descrizione_articolo", "pezzi_x_cartone",
    "cartoni_x_stra", "strati_x_pal", "prezzo", "quantita", "offerta",
    "Importo", "descrizione_reparto", "cat", "ragione_sociale", "variato",
    "descrizione_offerta", "Iva",
]

INTESTAZIONI_BETULLA = ["EAN", "CodArt", "ORDINE", "Descr.Commerciale", "PzCt",
                      "Cessione", "Pedana", "Iva", "TOTALI"]

# Gli adattatori scritti a mano nel progetto.  Non e' l'elenco del registro:
# e' l'elenco di quelli che ci devono **essere**, e il registro puo' averne di
# piu' — anzi deve poterne avere di piu', perche' impararne uno nuovo e' la
# funzione per cui il registro esiste.  Un collaudo che fissa il contenuto del
# registro diventa rosso il giorno in cui il programma fa il suo mestiere.
ADATTATORI_NATIVI = ("gestionale_v1", "betulla_v1", "cipresso_v1", "larice_v1",
                     "noce_xls_v1", "noce_csv_v1", "offerte_v1")

_PROFILI: dict[Path, dict[str, Any]] = {}


def profilo_del_file(percorso: Path) -> dict[str, Any]:
    """Il `details` che l'inspector produce oggi, calcolato una volta sola.

    E' quello che `riconosci` riceve in produzione: costruirne uno a mano
    renderebbe il collaudo indipendente dal profilo vero, cioe' inutile.
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
    """Un foglio con la forma del listino Noce: nota in alto, dati in fondo."""

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
    """Un foglio con la forma del listino Larice: nessuna intestazione, 18 colonne."""

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
    """Aggiunge a un .xlsx il risultato gia' calcolato delle sue formule.

    Un documento salvato da openpyxl porta la formula e basta: aprendolo con
    `data_only=True` si trova `None`, che e' com'e' fatto un file mai aperto da
    Excel.  Un listino vero invece arriva **calcolato**, e la cella porta anche
    il suo valore (`<f>` e `<v>` insieme): e' la forma su cui il lettore del
    programma lavora, ed e' quella che questi collaudi devono descrivere.
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
            # `t="str"` e' come Excel dichiara il risultato testuale di una
            # formula: senza, il valore verrebbe letto come un numero.
            cella = cella.replace(">", ' t="str">', 1) if 't="' not in cella else cella
            return f"{cella}<f>{formula}</f><v>{valore}</v></c>"
        return f"{cella}<f>{formula}</f><v>{valore}</v></c>"

    # openpyxl scrive la cella del valore vuota, perche' nessuno ha ancora
    # fatto il conto — ma la scrive in DUE forme, e quale delle due dipende da
    # una libreria che nessuno ha dichiarato:
    #
    #     con lxml installato   <c r="E2"><f>…</f><v></v></c>
    #     senza lxml            <c r="E2"><f>…</f><v /></c>
    #
    # openpyxl serializza con lxml quando c'e' (`openpyxl.xml.LXML`) e con
    # `xml.etree` quando non c'e', e le due scrivono l'elemento vuoto in modo
    # diverso. Non e' la versione di openpyxl: la 3.1.5 fa tutte e due le cose.
    # Riconoscere solo la prima forma faceva fallire tre collaudi su qualunque
    # Python con il solo openpyxl installato — cioe' proprio quello del PC del
    # negozio, che e' il computer che conta.
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
    """L'impronta di uno schema: che cosa la compone e che cosa non la cambia."""

    maxDiff = None

    def test_normalizza_da_lo_stesso_risultato_dell_inspector(self) -> None:
        """`inspect_sources.normalized` deve poter sparire a favore di questa.

        Se le due divergessero, un listino riconosciuto dall'inspector non
        verrebbe piu' ritrovato nel registro, e nessuno saprebbe perche'.
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
        """Una cella vuota nella riga di intestazione non e' un'intestazione.

        Il listino Noce ha la colonna A vuota proprio nella riga 5: se il
        vuoto entrasse nell'impronta, l'insieme osservato non combacerebbe mai
        con quello dichiarato.
        """

        token = registro.impronta_intestazioni([None, "EAN", "  ", "ean", "CodArt", "", "Cod. Art."])

        self.assertEqual(token, ["codart", "ean"])

    def test_l_ordine_delle_colonne_non_cambia_l_impronta(self) -> None:
        """Un fornitore che sposta una colonna manda lo stesso listino."""

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
        """Per questo l'identita' non è l'hash: sarebbe un falso allarme a settimana."""

        prima = registro.impronta("Foglio1", 1, 2, ["EAN", "CodArt"])
        dopo = registro.impronta("Foglio1", 1, 2, ["EAN", "CodArt", "NOTE"])

        self.assertNotEqual(prima["hash"], dopo["hash"])
        self.assertTrue(set(prima["headers"]) <= set(dopo["headers"]))


class RegistroLetturaTests(unittest.TestCase):
    """Leggere il registro, e dire quando non si è potuto."""

    maxDiff = None

    def setUp(self) -> None:
        self.radice = Path(tempfile.mkdtemp(prefix="collaudo_impronte_"))
        self.addCleanup(shutil.rmtree, self.radice, True)

    def test_gli_adattatori_dichiarano_tutti_un_impronta(self) -> None:
        """Un adattatore senza impronta non verrebbe mai riconosciuto.

        ⚠ Qui si controllano le **proprieta'** del registro, non il suo
        contenuto.  Prima questo collaudo pretendeva sei voci esatte: bastava
        imparare un adattatore — cioe' usare la funzione per cui il registro
        esiste — perche' diventasse rosso, e la promessa «suite verde» valeva
        solo finche' nessuno faceva lavorare il programma.
        """

        # ⚠ Questo, a differenza delle prove sui listini storici, guarda il
        # registro **vivo**: sono le proprieta' che devono valere anche dopo
        # che il programma ha imparato uno schema, ed e' l'unico posto che se
        # ne accorgerebbe.
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
        """Imparare uno schema non deve togliere `order_write` a un fornitore.

        E' la proprieta' che il 14 agosto 2026 e' saltata senza che niente lo
        dicesse: imparando lo schema CIPRESSO della settimana il registro ha
        riscritto `cipresso_v1`, e da li' in avanti la compilazione non ha piu'
        prodotto **nessuna copia, per nessun fornitore**.  Il confronto e' con
        la copia congelata, cioe' con quello che il programma sapeva fare
        quando e' stato consegnato: un fornitore compilabile non torna
        indietro.
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
        """Una firma posizionale scritta in un'altra forma non difende niente.

        `_verifica_posizioni` cerca le intestazioni per token normalizzato: una
        chiave scritta «Cod.Art.» invece di «codart» non verrebbe confrontata
        con niente e la verifica passerebbe sempre — cioe' la difesa che ha
        fermato la trappola BETULLA esisterebbe solo sulla carta.
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
        """Chi legge deve capire che il difetto è nell'installazione, non nel listino."""

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
    """Il riconoscimento su documenti costruiti apposta."""

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
        """Noce manda «formattato_104233.xls»: il nome non e' mai una prova.

        Al contrario, un nome che sembra dire tutto non deve poter dirottare
        il riconoscimento: qui il documento si chiama come un listino BETULLA ed
        e' un listino Larice.
        """

        esito = self.riconosci_file(foglio_larice(self.radice / "LISTINO BETULLA VALIDO FINO AL 28-07-26.xlsx"))

        self.assertEqual(esito["adapter_id"], "larice_v1")
        self.assertEqual(esito["state"], "SCHEMA_NOTO")
        self.assertEqual(esito["confidence"], 0.97)

    def test_l_impronta_per_forma_riconosce_un_listino_senza_intestazioni(self) -> None:
        """Larice non ha nessuna riga di intestazione: senza questa strada la
        sua regola resterebbe scritta nel codice."""

        esito = self.riconosci_file(foglio_larice(self.radice / "canvass.xlsx"))

        self.assertEqual(esito["adapter_id"], "larice_v1")
        self.assertEqual(esito["signature"]["header_row"], None)
        self.assertIn("colonna R popolata come EAN", esito["evidence"])
        self.assertIn("indicatore I in colonna B", esito["evidence"])

    def test_il_csv_passa_dallo_stesso_motore(self) -> None:
        """Il profilo di un CSV è piatto, senza «sheets»: deve reggere lo stesso."""

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
        """Di un .xls il lettore del progetto non sa quante formule ci siano:
        `formula_count` e `merged_ranges_count` valgono None, non zero."""

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

    # -- quello che non deve declassare -------------------------------------

    def test_una_colonna_in_piu_non_declassa_lo_schema(self) -> None:
        """Un fornitore che aggiunge una colonna decorativa non deve costare
        una chiamata AI ogni settimana: si dice e si va avanti."""

        intestazioni = list(INTESTAZIONI_NOCE) + ["NOTE PROMOZIONALI"]
        esito = self.riconosci_file(foglio_noce(self.radice / "noce.xlsx", intestazioni=intestazioni))

        self.assertEqual(esito["state"], "SCHEMA_NOTO")
        self.assertEqual(esito["unknown_headers"], ["notepromozionali"])
        self.assertIn("Intestazioni non dichiarate, che non declassano lo schema: notepromozionali",
                      esito["evidence"])

    def test_il_totale_instabile_di_betulla_non_impedisce_il_riconoscimento(self) -> None:
        """Il listino vero scrive TOTALE, il collaudo TOTALI: e' lo stesso schema.

        E' la prova che l'identita' non puo' essere l'uguaglianza dell'insieme
        completo delle intestazioni.
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
        """BETULLA, Larice e il gestionale hanno lettori dedicati: pretendere le
        verifiche di una mappatura che non c'e' li declasserebbe sempre.

        Le verifiche che invece li riguardano girano eccome, e sono quelle che
        contano: dove stanno i loro campi numerici e dove stanno le loro
        colonne il registro lo dichiara lo stesso, con `header_aliases` e con
        `header_signature.columns`."""

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
        # Un listino vuoto invece riguarda anche loro.
        self.assertTrue(per_nome["righe_dati"]["ok"])
        # E cosi' i tipi e le posizioni, che per un lettore posizionale sono
        # l'unica difesa che ha.
        self.assertTrue(per_nome["tipi_plausibili"]["ok"])
        self.assertIn("unit_price_net (Cessione)", per_nome["tipi_plausibili"]["detail"])
        self.assertTrue(per_nome["posizioni_intestazioni"]["ok"])
        self.assertNotEqual(per_nome["posizioni_intestazioni"]["detail"], registro.NON_APPLICABILE)

    # -- chi legge per posizione ---------------------------------------------
    #
    # BETULLA, il gestionale e Larice non hanno una mappatura: hanno un lettore
    # dedicato che prende il prezzo da `row[5]`, non dalla colonna intitolata
    # «Cessione».  Per loro l'insieme delle intestazioni non e' una difesa, e i
    # due collaudi qui sotto sono la misura di quanto costava non averne una:
    # sul listino vero il percorso veloce dichiarava SCHEMA_NOTO 0.99 e il
    # lettore restituiva 12,00 euro al posto di 3,98.

    def test_una_colonna_in_piu_declassa_chi_legge_per_posizione(self) -> None:
        """Una colonna in testa sposta tutte le altre di uno: per un lettore
        posizionale non e' mai decorativa."""

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
        """La colonna d'ordine di Noce (I) si compila IN POSIZIONE dentro
        il loro `.xls`: una colonna in piu' in testa sposterebbe le quantita'
        di una colonna senza cambiare nessun nome.  Le posizioni sono state
        misurate su `formattato_104233.xls` il 13 agosto 2026 (cantiere R4).
        """

        percorso = foglio_noce(self.radice / "noce_colonna_in_piu.xlsx",
                                   intestazioni=["NOTE", *INTESTAZIONI_NOCE])

        esito = self.riconosci_file(percorso)
        per_nome = {verifica["name"]: verifica for verifica in esito["checks"]}

        self.assertEqual(esito["adapter_id"], "noce_xls_v1")
        self.assertEqual(esito["state"], "SCHEMA_VARIATO")
        self.assertFalse(per_nome["posizioni_intestazioni"]["ok"])

    def test_la_colonna_in_piu_in_testa_declassa_anche_cipresso(self) -> None:
        """Era il minore dichiarato dalla verifica del 12 agosto: «Cipresso con
        colonna in piu' resta SCHEMA_NOTO e lo ferma solo lo scrittore».  La
        colonna d'ordine (G) e' posizionale: ora la firma dichiara le
        posizioni (misurate su `3listino_Cipresso.xlsx`) e il documento
        slittato si vede al riconoscimento, non alla compilazione.
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
        """E' il caso che nessun controllo sui nomi puo' vedere: stesse
        intestazioni, stesso numero di colonne, nessuna intestazione nuova.

        Misurato: `read_betulla` restituiva 6,00 euro al posto di 1,25 e 1,25
        pezzi per cartone al posto di 6, senza un avviso."""

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
        # I due documenti hanno esattamente le stesse intestazioni: e' il punto.
        self.assertEqual(esito["signature"]["headers"], intatto["signature"]["headers"])
        self.assertEqual(esito["unknown_headers"], intatto["unknown_headers"])
        self.assertEqual(intatto["state"], "SCHEMA_NOTO")
        self.assertEqual(esito["state"], "SCHEMA_VARIATO")
        self.assertFalse(per_nome["posizioni_intestazioni"]["ok"])
        self.assertIn("«pzct» attesa in colonna 5, trovata nella 6",
                      per_nome["posizioni_intestazioni"]["detail"])

    def test_il_prezzo_diventato_testo_declassa_anche_senza_mappatura(self) -> None:
        """Dove sta il prezzo di BETULLA lo dice `header_aliases`, non una
        mappatura: saltare la verifica dei tipi per chi ha un lettore dedicato
        la toglieva ai tre fornitori piu' grossi del confronto."""

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
        """La cella dell'intestazione e' testo e sta nella colonna dei prezzi:
        contarla fra i dati faceva scendere i numeri al 50% su un listino di
        una riga, e un listino corto sarebbe diventato uno schema variato."""

        percorso = scrivi_foglio(self.radice / "betulla_una_riga.xlsx", [
            INTESTAZIONI_BETULLA,
            ["8000000000001", "C-001", None, "Prodotto Alfa", 6, 1.25, 60, 22, 0],
        ], "Listino")

        esito = self.riconosci_file(percorso)
        per_nome = {verifica["name"]: verifica for verifica in esito["checks"]}

        self.assertEqual(esito["state"], "SCHEMA_NOTO")
        self.assertTrue(per_nome["tipi_plausibili"]["ok"])
        self.assertIn("unit_price_net (Cessione) 100%", per_nome["tipi_plausibili"]["detail"])

    # -- quello che deve declassare ----------------------------------------

    def test_un_foglio_di_copertina_davanti_declassa_lo_schema(self) -> None:
        """`sheet: "FIRST"` non vuol dire «il primo che combacia»: chi legge
        davvero il documento prende il foglio numero uno e basta.

        Senza questa verifica il percorso veloce prometteva 0.99 su un
        documento che il lettore non riesce nemmeno ad aprire."""

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
        """BETULLA ha un lettore dedicato e nessuna `field_mapping`: senza il
        ripiego su `header_signature.data_start_row` un listino con la sola
        riga di intestazione sarebbe SCHEMA_NOTO, e il fornitore sparirebbe
        dal confronto senza che niente sembri andato storto."""

        percorso = scrivi_foglio(self.radice / "betulla_vuoto.xlsx", [INTESTAZIONI_BETULLA], "Listino")

        esito = self.riconosci_file(percorso)
        per_nome = {verifica["name"]: verifica for verifica in esito["checks"]}

        self.assertEqual(esito["adapter_id"], "betulla_v1")
        self.assertEqual(esito["state"], "SCHEMA_VARIATO")
        self.assertFalse(per_nome["righe_dati"]["ok"])
        self.assertIn("riga 2", per_nome["righe_dati"]["detail"])

    def test_una_colonna_mappata_che_sparisce_diventa_schema_variato(self) -> None:
        """«Iva» non e' fra le obbligatorie ma la mappatura la usa: senza
        questa verifica il lettore cercherebbe una colonna che non c'e'."""

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
        """E' la variazione che fa piu' danno: il confronto fra fornitori si
        svuoterebbe in silenzio."""

        percorso = foglio_noce(self.radice / "noce.xlsx", prezzi_testuali=True)

        esito = self.riconosci_file(percorso)

        per_nome = {verifica["name"]: verifica for verifica in esito["checks"]}
        self.assertEqual(esito["state"], "SCHEMA_VARIATO")
        self.assertFalse(per_nome["tipi_plausibili"]["ok"])
        self.assertIn("unit_price_net", per_nome["tipi_plausibili"]["detail"])

    def test_l_intestazione_spostata_di_riga_diventa_schema_variato(self) -> None:
        """Il lettore parte dalla riga dichiarata: se sbaglia riga legge i
        titoli come se fossero prodotti."""

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

    # -- le colonne dichiarate per numero -----------------------------------

    def _listino_con_due_colonne_uguali(self, prezzi: list[Any]) -> Path:
        """Il caso ACERO: «COSTO IMPON.» in colonna 3 e in colonna 5.

        Con un'intestazione ripetuta il prezzo **non si puo'** dichiarare per
        nome: chi legge si fermerebbe con «Intestazione duplicata». Si dichiara
        per numero, ed e' l'unica strada.
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
        """Con i prezzi al loro posto la verifica passa, e dice quale colonna ha guardato."""

        percorso = self._listino_con_due_colonne_uguali([1.25, 2.50, 3.75])

        esito = self.riconosci_file(percorso, self._registro_per_numero())

        per_nome = {verifica["name"]: verifica for verifica in esito["checks"]}
        self.assertEqual(esito["state"], "SCHEMA_NOTO")
        self.assertTrue(per_nome["tipi_plausibili"]["ok"], per_nome["tipi_plausibili"])
        self.assertIn("colonna 3", per_nome["tipi_plausibili"]["detail"])

    def test_il_prezzo_diventato_testo_declassa_anche_dichiarato_per_numero(self) -> None:
        """Misurato su ACERO il 12 agosto 2026: SCHEMA_NOTO 0.99, 0 offerte,
        fornitore sparito dal confronto, zero avvisi.

        `_indice_di_colonna` tornava `None` su una colonna dichiarata per
        numero, `tipi_plausibili` si dichiarava «non applicabile» e la sola
        verifica che si accorge di un prezzo diventato testo restava spenta —
        proprio dove l'unico modo di dichiarare il prezzo e' il numero.
        """

        percorso = self._listino_con_due_colonne_uguali(["1,25", "2,50", "3,75"])

        esito = self.riconosci_file(percorso, self._registro_per_numero())

        per_nome = {verifica["name"]: verifica for verifica in esito["checks"]}
        self.assertEqual(esito["state"], "SCHEMA_VARIATO")
        self.assertFalse(per_nome["tipi_plausibili"]["ok"])
        self.assertIn("unit_price_net (colonna 3)", per_nome["tipi_plausibili"]["detail"])

    def test_una_posizione_booleana_nella_firma_non_passa_per_buona(self) -> None:
        """In Python `True == 1`: una firma scritta a mano con una posizione
        booleana passava la verifica come «tutto al suo posto», e una
        posizione non intera veniva saltata come se non fosse dichiarata
        (revisione avversariale del 13 agosto 2026).
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
        """«UM» è un'intestazione vera di CIPRESSO e insieme la colonna 559."""

        self.assertEqual(registro._indice_di_colonna(["EAN", "Prezzo", "Note"], 3), 3)
        self.assertEqual(registro._indice_di_colonna(["EAN", "Prezzo", "Note"], "Prezzo"), 2)
        self.assertEqual(registro._indice_di_colonna(["EAN", "Prezzo", "Note"], "C"), 3)
        self.assertIsNone(registro._indice_di_colonna(["EAN"], 0))
        self.assertIsNone(registro._indice_di_colonna(["EAN"], True))

    def test_una_stringa_di_cifre_e_un_nome_e_non_un_numero(self) -> None:
        """Chi legge davvero (`column_number`) tratta «9» come un'intestazione.

        Rispondere «colonna 9» a una domanda che il lettore rifiutera' vuol
        dire dichiarare verificato un documento che non verra' mai letto: la
        divergenza era stata introdotta proprio dal ramo che doveva allineare
        le due parti (revisione avversariale del 13 agosto 2026).
        """

        self.assertIsNone(registro._indice_di_colonna(["EAN", "Prezzo", "Note"], "3"))
        self.assertIsNone(registro._indice_di_colonna(["EAN", "Prezzo", "Note"], "09"))
        self.assertIsNone(registro._indice_di_colonna(["EAN", "Prezzo", "Note"], "  9  "))
        # E se «3» e' davvero il nome di una colonna, vince come nome.
        self.assertEqual(registro._indice_di_colonna(["EAN", "3", "Note"], "3"), 2)

    # -- quello che non si riconosce ---------------------------------------

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

    # -- le regole di scelta fra piu' candidati ----------------------------

    def test_fra_due_candidati_vince_quello_che_dichiara_piu_obbligatorie(self) -> None:
        """A parita' di confidenza vince lo schema piu' specifico: e' quello
        che descrive meglio il documento."""

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
        """`min_exclusive` e' `>` e non `>=`: era cosi' prima che la regola
        uscisse dal codice, e spostarla di nascosto cambierebbe il
        riconoscimento di un listino gia' collaudato."""

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

        # 70 numeri su 100: il rapporto vale esattamente 0.7 e non lo supera.
        stretto = registro_di_prova(self.radice / "stretto.json", [voce])
        esito_stretto = self.riconosci_file(percorso, stretto)

        larga = json.loads(json.dumps(voce))
        larga["column_shape_signature"]["checks"][2]["type_ratio"]["min_exclusive"] = 0.69
        esito_largo = self.riconosci_file(percorso, registro_di_prova(self.radice / "largo.json", [larga]))

        self.assertEqual(esito_stretto["state"], "AMBIGUO")
        self.assertEqual(esito_largo["adapter_id"], "forma_v1")
        self.assertEqual(esito_largo["confidence"], 0.97)

    def test_un_foglio_troppo_stretto_non_e_un_candidato_per_forma(self) -> None:
        """`min_columns` e' la prima difesa dell'impronta per forma: senza,
        basterebbero quattro colonne popolate nel modo giusto perche' un
        listino di un altro fornitore passasse per Larice."""

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
        """«UM», «QT» e «P» sono intestazioni vere e insieme lettere di colonna
        plausibili: cercare prima fra le intestazioni evita di misurare la
        colonna sbagliata."""

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
        """Una colonna senza intestazione si dichiara per numero: e' il caso di
        quasi tutte le colonne d'ordine, che arrivano vuote.

        Cercare «6» fra le intestazioni non la troverebbe mai, e il fornitore
        resterebbe da interpretare per sempre: una chiamata AI ogni settimana
        per una colonna che il lettore trova senza fatica."""

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
        # Una colonna dichiarata oltre la larghezza del documento resta un
        # errore: e' la differenza fra «non ha un nome» e «non c'e'».
        oltre_esito = self.riconosci_file(percorso, oltre)
        oltre_nome = {verifica["name"]: verifica for verifica in oltre_esito["checks"]}
        self.assertEqual(oltre_esito["state"], "SCHEMA_VARIATO")
        self.assertFalse(oltre_nome["colonne_attese"]["ok"])
        self.assertIn("99", oltre_nome["colonne_attese"]["detail"])

    def test_il_profilo_di_un_csv_distingue_i_numeri_dal_testo(self) -> None:
        """In un CSV tutto e' testo, e il profilo dichiarava «testo» anche la
        colonna dei prezzi: qualunque adattatore CSV con una mappatura sarebbe
        stato condannato a SCHEMA_VARIATO per sempre.

        I prezzi arrivano scritti all'italiana — «21,75» — e vanno riconosciuti
        anche cosi'."""

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
        """`missing_headers` puo' essere non vuoto solo qui: un adattatore
        agganciato per forma che dichiari anche un'impronta per intestazioni.

        E' il giorno in cui Larice comincia a intitolare le colonne, ed e'
        l'unica frase che dice all'utente che cosa cercare."""

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
    """Scrivere nel registro senza perdere quello che c'era prima."""

    maxDiff = None

    def setUp(self) -> None:
        self.radice = Path(tempfile.mkdtemp(prefix="collaudo_scrittura_"))
        self.addCleanup(shutil.rmtree, self.radice, True)
        # Sempre su una copia: il registro vero lo legge una persona prima del
        # commit, e un collaudo non deve poterlo toccare.
        self.registro = self.radice / "adapters.json"
        shutil.copyfile(ADAPTERS, self.registro)
        # ⚠ Dal 19 agosto 2026 i registri sono due: quello **spedito**, sotto
        # git, che nessuna scrittura del programma tocca piu', e quello
        # **imparato**, fuori da git, dove finisce tutto quello che il
        # programma impara in negozio. `scrivi_adattatore` scrive solo nel
        # secondo; chi legge li vede fusi.
        self.imparato = registro.percorso_imparato(self.registro)

    def documento(self) -> dict[str, Any]:
        """Il file dove le scritture finiscono davvero."""

        return json.loads(self.imparato.read_text(encoding="utf-8"))

    def voce(self, identificativo: str) -> dict[str, Any]:
        """La voce come la vede il programma: spedito e imparato insieme."""

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
        """La prova che conta.

        Se la settimana prossima il riconoscimento peggiora, l'unico modo per
        capire che cosa e' cambiato e' avere ancora sotto gli occhi la versione
        di partenza.
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
        # La prima versione, quella con cui il programma e' partito, e' ancora
        # tutta li' — impronta compresa — e non porta a sua volta una storia.
        prima = viva["previous_versions"][0]
        self.assertEqual(prima["display_name"], "CIPRESSO")
        self.assertEqual(prima["schema_version"], 1)
        self.assertEqual(prima["header_signature"], originale["header_signature"])
        self.assertNotIn("previous_versions", prima)
        self.assertEqual(viva["previous_versions"][1]["display_name"], "CIPRESSO seconda")

    def test_la_voce_viva_resta_al_livello_superiore(self) -> None:
        """Chi legge `field_mapping` per id continua a trovare l'ultima
        versione senza sapere niente delle precedenti."""

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
        """I due registri si leggono affiancati quando qualcosa non torna, e uno
        scritto in CRLF li farebbe sembrare diversi riga per riga anche dove
        dicono la stessa cosa."""

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
        """La ragione per cui i file sono due: quello sotto git l'avvio del PC
        del negozio lo riporta indietro a ogni doppio clic, quindi tutto quello
        che il programma impara e che finisse li' dentro sparirebbe."""

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
        """Chi ha il documento davanti ha ragione su chi l'ha spedito la
        settimana scorsa. Il prezzo: un adattatore imparato male non si corregge
        spedendone uno nuovo — si corregge togliendo la sua voce
        dall'imparato."""

        originale = self.voce("cipresso_v1")
        self.assertEqual(originale["display_name"], "CIPRESSO")

        registro.scrivi_adattatore({**originale, "display_name": "CIPRESSO del negozio"}, self.registro)

        self.assertEqual(self.voce("cipresso_v1")["display_name"], "CIPRESSO del negozio")
        self.assertEqual(
            registro.nomi_dei_fornitori(self.registro)["cipresso"], "CIPRESSO del negozio",
        )
        # E lo spedito e' ancora quello che era: la voce di prima non e' persa,
        # e' coperta.
        spedite = json.loads(self.registro.read_text(encoding="utf-8"))["adapters"]
        originale_spedita = next(voce for voce in spedite if voce["id"] == "cipresso_v1")
        self.assertEqual(originale_spedita["display_name"], "CIPRESSO")

    def test_un_imparato_che_non_c_e_non_e_un_errore(self) -> None:
        """Un'installazione nuova non ha imparato niente, e deve funzionare."""

        self.assertFalse(self.imparato.exists())
        self.assertIsNone(registro.motivo_registro_illeggibile(self.registro))
        self.assertIn("cipresso_v1", [voce["id"] for voce in registro.adattatori(self.registro)])

    def test_un_imparato_rotto_si_dice_ma_non_ferma_il_riconoscimento(self) -> None:
        """Ferma tutto sarebbe peggio: gli adattatori spediti bastano a
        lavorare, e l'imparato rotto e' una cosa da aggiustare, non da subire
        in silenzio."""

        self.imparato.write_bytes(b"{questo non e' JSON")

        motivo = registro.motivo_registro_illeggibile(self.registro)

        self.assertIsNotNone(motivo)
        self.assertIn("imparati", motivo)
        self.assertIn("cipresso_v1", [voce["id"] for voce in registro.adattatori(self.registro)])

    def test_una_scrittura_interrotta_non_rovina_il_registro(self) -> None:
        """La scrittura passa da un file temporaneo e da `os.replace` perche' un
        registro monco vuol dire zero adattatori: BETULLA, Larice, CIPRESSO e
        Noce diventati «da interpretare» tutti insieme, e nessuno che sappia
        perche'.

        Il programma finito gira da solo: chi lo usa vedrebbe solo listini che
        non si riconoscono piu', e cercherebbe il guasto nei listini."""

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
        # La proprieta' e' «non si e' perso niente», non «erano sei»: il
        # numero di adattatori cresce ogni volta che se ne impara uno.
        dopo = [voce["id"] for voce in registro.adattatori(self.registro)]
        self.assertEqual(dopo, identificativi)
        # Sulla lista di DOPO, non su quella catturata prima del tentativo:
        # li' «acero_v1» non c'era per costruzione, e l'asserzione reggeva
        # solo per transitivita' (revisione avversariale del 13 agosto 2026).
        self.assertNotIn("acero_v1", dopo, "la voce interrotta non deve essere entrata")
        self.assertEqual(
            sorted(percorso.name for percorso in self.radice.iterdir()),
            ["adapters.json", "adattatori_imparati.json"],
        )

    def test_un_adattatore_di_un_altro_fornitore_viene_rifiutato(self) -> None:
        """Sarebbe un fornitore che si mangia l'adattatore di un altro."""

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
        """Meglio fermarsi che perdere tutti gli altri adattatori."""

        self.imparato.write_bytes(b"{questo non e' JSON")

        with self.assertRaises(ValueError) as errore:
            registro.scrivi_adattatore({"id": "nuovo_v1", "supplier_id": "nuovo"}, self.registro)

        self.assertIn("non è leggibile", str(errore.exception))
        self.assertEqual(self.imparato.read_bytes(), b"{questo non e' JSON")

    def test_uno_spedito_illeggibile_ferma_la_scrittura(self) -> None:
        """Senza lo spedito non si sa da che versione si riparte: scrivere lo
        stesso vorrebbe dire far ricominciare da uno un adattatore che ha una
        storia, e buttarla via senza dirlo."""

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
        """La scrittura e il riconoscimento devono parlare la stessa lingua:
        altrimenti un adattatore imparato oggi resterebbe sconosciuto domani."""

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
    """Il riconoscimento sui listini veri, in sola lettura.

    Sono le misure che contano: un motore che funziona solo sui documenti
    costruiti dal collaudo non serve a niente.
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
        # Il listino vero scrive TOTALE: e' dichiarato, quindi non e' una novita'.
        self.assertEqual(esito["unknown_headers"], [])

    def test_il_betulla_risalvato_in_excel_si_riconosce_lo_stesso_per_dirlo(self) -> None:
        """Il guasto vero del 22 agosto 2026, riprodotto sul listino vero.

        Qualcuno ha aperto il listino BETULLA in Excel e l'ha risalvato con la
        cella C1 svuotata: la parola ORDINE non c'era piu'. Il programma ha
        detto «Nessuna firma nota sufficiente» — e chi l'ha letto ha
        configurato BETULLA come se fosse un fornitore nuovo, scrivendo un
        adattatore imparato sopra quello spedito.

        Lo stato resta AMBIGUO: il listino non si legge, e non deve. Quello che
        cambia e' la frase, che adesso dice dove guardare.
        """

        percorso = LISTINI / "LISTINO BETULLA VALIDO FINO AL 01-09-26.xlsx"
        if not percorso.is_file():
            percorso = LISTINI.parent / "documenti" / "LISTINO BETULLA VALIDO FINO AL 01-09-26.xlsx"
        if not percorso.is_file():
            self.skipTest(f"Manca il listino BETULLA sotto {LISTINI}")

        # ⚠ Si copia e si modifica la copia: l'originale e' un file d'ingresso.
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
        """La controprova del «per un pelo»: i documenti che il registro
        davvero non conosce non devono diventare «sembra il listino di…».

        Sono i tre di sempre — ACERO, QUERCIA e GINEPRO — e nessuno dei tre ha un
        adattatore. Se uno di loro cominciasse a somigliare a qualcuno, la
        frase nuova starebbe indovinando invece di riconoscere.
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
        """Il foglio delle offerte non ha nessuna intestazione da leggere.

        Sopra i prodotti ci sono righe vuote e la sola parola ORDINE in
        colonna H: un'impronta per intestazioni non ha niente da ritrovare, ed
        e' il motivo per cui questo adattatore si riconosce dalla forma delle
        colonne come Larice. La riga da cui partono i prodotti non e' un
        numero congelato: la trova il marcatore, che si ricalcola a ogni
        lettura, perche' il blocco vuoto in testa cambia di mese in mese.
        """

        esito = self.riconosci_listino("OFFERTE AGOSTO 4.xlsx")

        self.assertEqual(esito["state"], "SCHEMA_NOTO")
        self.assertEqual(esito["adapter_id"], "offerte_v1")
        self.assertEqual(esito["confidence"], 0.96)

    def test_nessun_altro_listino_vero_finisce_nelle_offerte(self) -> None:
        """La forma delle colonne e' generica: va provato che non peschi altrove.

        Un'impronta per forma non ha nomi che la ancorino, quindi il rischio
        vero non e' che non riconosca il suo documento: e' che si prenda
        quello di un altro. Qui ci sono tutti i listini veri che il progetto
        conserva, compresi i tre che nessuno ha ancora imparato.
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
        """Il rischio vero di un'impronta per forma, misurato il 21 agosto 2026.

        «Ci sono prezzi, pezzi per collo e codici a barre nelle prime sei
        colonne» lo dicono quasi tutti i listini del mondo. Con quattro
        controlli su cinque bastavano 0,84 su una soglia di 0,75, e due fogli
        costruiti apposta — un listino qualunque con l'IVA in G, e lo stesso con
        l'intestazione alla riga 3 e ORDINE in H — diventavano «OFFERTE»: letti
        per intero, compilabili, e senza nessuna strada per correggerli dalla
        pagina, perche' uno SCHEMA_NOTO la mappatura guidata non la apre.
        """

        prodotti = [
            [f"0{28000 + indice}", f"ARTICOLO DI PROVA {indice}", "PZ", 6, 1.85 + indice / 100,
             f"800241003{7000 + indice}"]
            for indice in range(120)
        ]

        # ⚠ Un nome per caso: `profilo_del_file` tiene i profili in cache per
        # percorso, e due fogli diversi con lo stesso nome sarebbero lo stesso
        # profilo — cioe' una prova che si dimostra da sola.
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
        """Una promo normale ha trenta articoli; quella di agosto, con 621, era il caso grosso."""

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
        """ACERO, QUERCIA, GINEPRO e il secondo schema CIPRESSO non sono nessuno dei sei.

        ⚠ La proprieta' non e' «restano AMBIGUI»: il giorno in cui l'utente ne
        impara uno — che e' la funzione per cui il registro esiste — quel
        listino diventa legittimamente noto, e un collaudo che pretendeva
        AMBIGUO diventava rosso proprio quando il programma faceva il suo
        mestiere.  Quello che non deve succedere mai e' che uno dei sei
        adattatori scritti a mano se lo prenda: sarebbe un listino letto con le
        regole commerciali di un altro fornitore, e nessuno se ne accorgerebbe.
        """

        for nome in ("ACERO LISTINO SETTIMANA 26.xlsx",
                     "LISTINO QUERCIA AGGIORNATO DEL 06-08-2026.xlsx",
                     "ListinoGINEPRO 5 aggiornato al 25-02-2026.xlsx",
                     "Listino3_33.xlsx"):
            with self.subTest(listino=nome):
                esito = self.riconosci_listino(nome)
                self.assertNotIn(esito["adapter_id"], ADATTATORI_NATIVI)
                if esito["adapter_id"] is None:
                    # Finche' nessuno l'ha imparato: resta da interpretare, e
                    # lo dice senza scegliere a caso.
                    self.assertEqual(esito["state"], "AMBIGUO")
                    self.assertEqual(esito["confidence"], 0.0)

    def test_profilare_un_listino_vero_non_lo_tocca(self) -> None:
        """È già successo di riscrivere il listino vero durante una prova."""

        percorso = LISTINI / "LISTINO BETULLA VALIDO FINO AL 28-07-26.xlsx"
        if not percorso.is_file():
            self.skipTest(f"Manca il listino {percorso}")
        prima = percorso.stat()

        registro.riconosci(inspect_sources.profile_file(percorso)["details"])

        dopo = percorso.stat()
        self.assertEqual((prima.st_size, prima.st_mtime_ns), (dopo.st_size, dopo.st_mtime_ns))


class IListiniVeriControIlRegistroCheVaAlNegozio(unittest.TestCase):
    """Gli stessi listini, letti col registro **consegnato**.

    ⚠ Le prove qui sopra girano sulla copia congelata
    (`tests/fixtures/adapters_nativi.json`), e c'e' una ragione scritta: una
    suite che diventa rossa quando il programma impara uno schema insegna a non
    guardarla. Ma da quella scelta e' nato un buco che e' costato una conferma
    a mano ogni settimana per otto giorni: **i due registri divergono**, e
    nessuno se ne accorgeva.

    Su `cipresso_v1` la copia congelata dichiara «FIRST» e riga 1; quella
    consegnata, dal 14 agosto 2026, dichiara «Listino» e riga 2. Il listino
    CIPRESSO che arriva ogni settimana ha il foglio intitolato «Listino al
    <data>» e le intestazioni alla riga 1: con la copia congelata e' SCHEMA_NOTO
    — e la suite era verde — con quello che il negozio ha davvero non si
    apriva nemmeno il foglio.

    Questa classe guarda il registro che il negozio riceve. Il registro spedito
    non se lo riscrive piu' nessuno dal 19 agosto 2026 — quello che si impara
    va in `app/data/adattatori_imparati.json` — quindi non e' una prova che
    diventa rossa quando il programma fa il suo mestiere.
    """

    maxDiff = None

    def riconosci_consegnato(self, nome: str) -> dict[str, Any]:
        percorso = LISTINI / nome
        if not percorso.is_file():
            self.skipTest(f"Manca il listino {percorso}")
        return registro.riconosci(profilo_del_file(percorso), ADAPTERS_CONSEGNATO)

    def test_i_due_listini_di_cipresso_si_leggono_tutti_e_due(self) -> None:
        """CIPRESSO manda due schemi diversi, non uno con il nome del foglio
        che cambia: cambia anche la riga delle intestazioni, e uno dei due ha
        la colonna ORDINE che l'altro non ha.

        Misurato il 22 agosto 2026 leggendo davvero il listino datato con la
        mappatura delle due voci: quella con «FIRST» e riga 1 legge 3.564
        righe, quella con «Listino» e riga 2 si ferma su «Foglio non trovato».
        """

        datato = self.riconosci_consegnato("3listino_Cipresso.xlsx")
        self.assertEqual(datato["state"], "SCHEMA_NOTO")
        self.assertEqual(datato["adapter_id"], "cipresso_con_ordine_v1")

        # E l'altro schema non se lo prende la voce nuova.
        senza_ordine = self.riconosci_consegnato("Listino3_33.xlsx")
        self.assertEqual(senza_ordine["state"], "SCHEMA_NOTO")
        self.assertEqual(senza_ordine["adapter_id"], "cipresso_v1")

    def test_gli_altri_listini_veri_restano_dove_erano(self) -> None:
        """La voce in più non deve prendersi il documento di nessun altro."""

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
        """LARICE manda due canvass con schemi diversi, e devono restare due.

        Dal 4 settembre 2026 arriva «New Larice N°37(v.0)»: intestazioni vere
        alla riga 11, EAN in B, netto in K, 16 colonne. Quello di prima non ha
        nessuna intestazione, tiene l'EAN in R e ha almeno 18 colonne. Le due
        firme si escludono a vicenda apposta — una pretende le sue intestazioni,
        l'altra un numero di colonne che il nuovo non raggiunge — perche' il
        fornitore puo' tornare al formato di prima da una settimana all'altra.
        """

        nuovo = self.riconosci_consegnato("New Larice N°37(v.0).xls")
        self.assertEqual(nuovo["state"], "SCHEMA_NOTO")
        self.assertEqual(nuovo["adapter_id"], "larice_canvass_v1")

        vecchio = self.riconosci_consegnato("28.1 06-10lug.xlsx")
        self.assertEqual(vecchio["state"], "SCHEMA_NOTO")
        self.assertEqual(vecchio["adapter_id"], "larice_v1")

    def test_i_due_canvass_di_larice_sono_dello_stesso_fornitore(self) -> None:
        """Due voci, un fornitore solo: a valle LARICE resta LARICE.

        Con due `supplier_id` diversi il confronto vedrebbe due fornitori dove
        ce n'e' uno, e chi ha ordinato sul canvass vecchio si ritroverebbe le
        quantita' divise fra due ordini.
        """

        voci = {voce["id"]: voce for voce in registro.adattatori(ADAPTERS_CONSEGNATO)}
        self.assertEqual(voci["larice_v1"]["supplier_id"],
                         voci["larice_canvass_v1"]["supplier_id"])
        self.assertEqual(voci["larice_canvass_v1"]["supplier_id"], "larice")

    def test_il_canvass_nuovo_dichiara_dove_tiene_le_sue_offerte(self) -> None:
        """Le soglie con omaggio del formato nuovo non si leggono da sole.

        Il canvass nuovo scrive l'intestazione della soglia e la riga
        dell'omaggio nella **stessa** colonna E, e la mappatura guidata non sa
        dichiararlo: pretende quattro colonne distinte per la forma «blocchi».
        Se questa dichiarazione sparisce dal registro, le offerte di LARICE
        smettono di esistere senza che niente lo dica.
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
        """Due voci, un fornitore solo: a valle CIPRESSO resta CIPRESSO.

        Se le due dichiarassero `supplier_id` diversi, il confronto vedrebbe
        due fornitori dove ce n'e' uno, e le quantita' finirebbero divise fra
        due colonne di due ordini.
        """

        voci = {voce["id"]: voce for voce in registro.adattatori(ADAPTERS_CONSEGNATO)}
        self.assertEqual(voci["cipresso_v1"]["supplier_id"],
                         voci["cipresso_con_ordine_v1"]["supplier_id"])
        self.assertEqual(
            registro.nome_del_fornitore_fra("cipresso", voci.values()), "CIPRESSO",
        )

    def test_tutti_e_due_gli_schemi_sanno_scrivere_l_ordine(self) -> None:
        """Uno schema che si riconosce ma non si compila lascerebbe CIPRESSO
        fuori dalla compilazione a settimane alterne, senza dire perche'."""

        voci = {voce["id"]: voce for voce in registro.adattatori(ADAPTERS_CONSEGNATO)}
        for identificativo in ("cipresso_v1", "cipresso_con_ordine_v1"):
            with self.subTest(adattatore=identificativo):
                scrittura = registro.scrittura_ordine(voci[identificativo])
                self.assertTrue(scrittura, "questo schema non sa scrivere il proprio ordine")
                self.assertEqual(scrittura.get("order_column"), "G")


class IlNomeDelFornitoreInOgniFrase(unittest.TestCase):
    """La stessa regola sui nomi, ovunque, e in un posto solo.

    ⚠ Il nome leggibile e' stato scritto a mano in punti diversi del programma
    per tre cantieri di fila. R8 ha tolto le tabelle cablate; la revisione di
    regressione del 14 agosto 2026 ha trovato che restavano cinque frasi nel
    lanciatore e una nella mappatura guidata a fare `supplier_id.upper()`.
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
        """Il più lungo descrive il documento, non il fornitore."""

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
    """Una colonna scritta con una formula e' leggibile come tutte le altre.

    GINEPRO scrive il prezzo scontato come `=SUM(E4*(1-5%))`: il lettore apre il
    documento con `data_only=True` e ci trova 1,52, il profilo lo apriva con
    `data_only=False` e ci trovava il testo della formula.  Risultato
    misurato: `tipi_plausibili` dichiarava **0% numerica** una colonna di 4132
    prezzi, il documento usciva SCHEMA_VARIATO 0,98 e quel fornitore avrebbe
    chiesto la mappatura a mano ogni settimana, per sempre.
    """

    maxDiff = None

    def setUp(self) -> None:
        self.temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporanea.cleanup)
        self.radice = Path(self.temporanea.name)

    def listino(self, nome: str, prezzo: Any, *, righe: int = 12) -> Path:
        """Un listino minimo dove il prezzo netto e' quello che si dichiara.

        `prezzo` restituisce la cella: un numero, un testo, oppure la coppia
        (formula, valore gia' calcolato) — quest'ultima e' l'unica forma che
        descrive un documento uscito da Excel, e openpyxl da sola non la sa
        scrivere.
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
        """⚠ La verifica non si allenta: esiste perche' un prezzo diventato
        testo passava come SCHEMA_NOTO 0.99 con zero offerte."""

        percorso = self.listino("testuale.xlsx", lambda numero: f"1,{numero:02d}")
        esito = self.riconoscimento(percorso)

        tipi = next(voce for voce in esito["checks"] if voce["name"] == "tipi_plausibili")
        self.assertFalse(tipi["ok"], tipi["detail"])
        self.assertIn("0%", tipi["detail"])
        self.assertEqual(esito["state"], "SCHEMA_VARIATO")

    def test_una_formula_che_restituisce_testo_resta_testo(self) -> None:
        """Il conto guarda che cosa **vale** la formula, non che sia una formula."""

        percorso = self.listino(
            "formula-testo.xlsx", lambda numero: (f'=CONCATENATE("1,",{numero})', f"1,{numero}"))
        # La colonna e' fatta di formule, e il censimento le ha lette: e' il
        # loro risultato a essere testo, non il fatto che siano formule.
        colonna = next(voce for voce in inspect_sources.profile_file(percorso)["details"]["sheets"][0]["columns"]
                       if voce["index"] == 5)
        self.assertEqual(colonna["formula_values"], {"text": 12})

        esito = self.riconoscimento(percorso)

        tipi = next(voce for voce in esito["checks"] if voce["name"] == "tipi_plausibili")
        self.assertFalse(tipi["ok"], tipi["detail"])
        self.assertEqual(esito["state"], "SCHEMA_VARIATO")

    def test_una_formula_mai_calcolata_non_e_un_prezzo(self) -> None:
        """Un file che Excel non ha mai aperto non porta nessun valore.

        Li' la colonna e' davvero illeggibile — il lettore ci trova `None` — e
        dichiararla numerica sarebbe la bugia peggiore delle due.
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
        # Un documento senza formule non paga la seconda lettura, e non
        # dichiara di averla fatta.
        self.assertNotIn("formula_values_read", secondo)
        self.assertFalse(any("formula_values" in voce for voce in secondo["columns"]))

    def test_l_impronta_per_forma_continua_a_distinguere_le_formule(self) -> None:
        """⚠ Chi e' un documento e che cosa ci si legge sono due domande diverse.

        Contando le formule per il loro risultato anche nelle impronte per
        forma delle colonne, il listino ACERO vero — 18.644 formule — prende
        0,76 sull'impronta di LARICE, che di formule non ne ha nemmeno una, e
        si presenta come una sua variazione: un fornitore vero scambiato per un
        altro fornitore vero.
        """

        colonna = {"index": 15, "nonempty": 100, "types": {"formula": 100},
                   "formula_values": {"number": 100}}
        self.assertEqual(registro._quota(colonna, "number"), 0.0)
        self.assertEqual(registro._quota(colonna, "formula"), 1.0)
        self.assertEqual(registro._quota_leggibile(colonna, "number"), 1.0)


class DoveCominciaIlListinoTests(unittest.TestCase):
    """Il separatore che dichiara l'inizio dei dati, visto dal profilo."""

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
        """Senza, chi mappa vede il blocco promozionale e non il listino."""

        percorso = self.listino_con_blocco(promozionali=25)
        foglio = inspect_sources.profile_file(percorso)["details"]["sheets"][0]

        numeri = {voce["row"] for voce in foglio["section_rows"]}
        # Il separatore sta alla riga 27, oltre le prime venti che il profilo
        # manda comunque: senza queste righe l'anteprima non lo vedrebbe.
        self.assertEqual([voce["row"] for voce in foglio["section_breaks"]], [27])
        self.assertLessEqual({25, 26, 27, 28, 29, 30}, numeri)

    def test_un_etichetta_in_mezzo_ai_dati_non_gonfia_il_profilo(self) -> None:
        """Il listino LARICE vero ha 625 righe strette: sono gruppi, non inizi."""

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
        """Il profilo non porta tutto il documento: dirlo è meglio che fingere."""

        esito = registro._verifica_marcatore_dei_dati(
            {"column": "A", "equals": "LISTINO", "offset": 1},
            {"header_rows": [{"row": 1, "values": ["Articolo", "EAN"]}]})

        self.assertTrue(esito["ok"])
        self.assertIn("si cerca sul documento intero", esito["detail"])


class ITreListiniSenzaAdattatoreTests(unittest.TestCase):
    """Le due misure sui listini veri che dicono se il lavoro e' finito.

    Un motore che funziona solo sui documenti costruiti dal collaudo non serve
    a niente: QUERCIA e GINEPRO sono i due file su cui i due difetti sono stati
    misurati, e sono quelli che devono cambiare esito.
    """

    maxDiff = None

    def listino(self, nome: str) -> Path:
        percorso = LISTINI / nome
        if not percorso.is_file():
            self.skipTest(f"Manca il listino {percorso}")
        return percorso

    def test_il_prezzo_calcolato_di_ginepro_e_una_colonna_numerica(self) -> None:
        """Misurato: 4132 celle `=SUM(E4*(1-5%))` dichiarate 0% numeriche."""

        percorso = self.listino("ListinoGINEPRO 5 aggiornato al 25-02-2026.xlsx")
        foglio = profilo_del_file(percorso)["sheets"][0]

        colonna = next(voce for voce in foglio["columns"] if voce["letter"] == "F")
        self.assertGreater(colonna["types"].get("formula", 0), 4000)
        self.assertEqual(set(colonna["formula_values"]), {"number"})
        self.assertGreater(registro._quota_leggibile(colonna, "number"), 0.99)
        # E la colonna E, che il fornitore scrive come testo all'italiana
        # («1,6»), resta testo: il censimento non trasforma niente in numero, e
        # chi la dichiarasse come prezzo verrebbe fermato come prima.
        prezzo_testuale = next(voce for voce in foglio["columns"] if voce["letter"] == "E")
        self.assertNotIn("formula_values", prezzo_testuale)
        self.assertLess(registro._quota_leggibile(prezzo_testuale, "number", 1), 0.5)

    def test_il_separatore_di_quercia_e_nel_profilo_con_le_righe_attorno(self) -> None:
        """`A68 = 'LISTINO'`: l'unico separatore di tutto il file."""

        percorso = self.listino("LISTINO QUERCIA AGGIORNATO DEL 06-08-2026.xlsx")
        foglio = profilo_del_file(percorso)["sheets"][0]

        separatori = {voce["row"]: voce for voce in foglio["section_breaks"]}
        self.assertIn(68, separatori)
        self.assertEqual(separatori[68]["text"], "LISTINO")
        self.assertEqual(separatori[68]["letter"], "A")
        self.assertEqual(separatori[68]["data_from"], 69)
        numeri = {voce["row"] for voce in foglio["section_rows"]}
        # Le righe 66-74 non erano nel profilo: mandava 1-20, 2094-2098 e
        # 4180-4190, cioe' tutto tranne il punto in cui il listino comincia.
        self.assertLessEqual({66, 67, 68, 69, 70, 71}, numeri)


class DueScrittureInsiemeNonSiCancellanoAVicenda(unittest.TestCase):
    """⚠ Il registro si scriveva leggendo, modificando e riscrivendo, e quel
    giro non era protetto da niente.

    La scrittura del **file** e' atomica da sempre (temporaneo con `os.replace`),
    quindi nessuno ha mai letto un registro monco. Ma due scritture che si
    accavallano leggevano lo stesso documento di partenza, e la seconda a
    riscrivere cancellava la voce della prima: misurato il 22 agosto 2026, dieci
    scritture insieme e nel registro ne restava **una**.

    Non e' un caso di laboratorio: il servizio e' un `ThreadingHTTPServer`,
    `impara_adattatore` gira come processo a se' durante il ricalcolo, e ogni
    comparatore avviato dalla stessa cartella scrive lo stesso registro imparato
    — il percorso dei dati si sceglie all'avvio, quello del registro no.
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
            except Exception as errore:  # noqa: BLE001 - qualunque, va detto
                guasti.append(f"{type(errore).__name__}: {errore}")

        fili = [threading.Thread(target=scrivi, args=(numero,)) for numero in range(quante)]
        for filo in fili:
            filo.start()
        for filo in fili:
            filo.join(timeout=30)

        self.assertEqual(guasti, [])
        self.assertEqual(sorted(self.imparate()), sorted(f"forn{n}_v1" for n in range(quante)))

    def test_un_turno_abbandonato_non_blocca_il_registro_per_sempre(self) -> None:
        """⚠ E' il rischio che un lucchetto porta con se', ed e' peggio del
        difetto che chiude: un processo ucciso a meta' — l'antivirus, il PC
        spento — lascerebbe il registro chiuso, e un programma che non impara
        piu' e non lo dice non lo scopre nessuno.
        """

        documento = registro.percorso_imparato(self.spedito)
        documento.parent.mkdir(parents=True, exist_ok=True)
        lucchetto = documento.with_name(documento.name + ".lock")
        lucchetto.write_text("99999 0\n", encoding="utf-8")
        # Vecchio piu' della soglia: chi lo teneva non c'e' piu'.
        vecchio = time.time() - registro.TURNO_ABBANDONATO - 5
        os.utime(lucchetto, (vecchio, vecchio))

        registro.scrivi_adattatore(self.voce(1), self.spedito)

        self.assertEqual(self.imparate(), ["forn1_v1"])
        self.assertFalse(lucchetto.exists(), "il turno preso va restituito")

    def test_un_turno_di_un_altro_non_fa_perdere_la_voce(self) -> None:
        """Se il turno non si ottiene entro l'attesa si scrive lo stesso: il
        caso peggiore torna a essere quello di prima, non uno peggiore. Un
        adattatore appena confermato dall'utente non va perso perche' un altro
        processo e' lento."""

        documento = registro.percorso_imparato(self.spedito)
        documento.parent.mkdir(parents=True, exist_ok=True)
        lucchetto = documento.with_name(documento.name + ".lock")
        lucchetto.write_text("1 0\n", encoding="utf-8")  # fresco: non e' abbandonato

        registro.scrivi_adattatore(self.voce(2), self.spedito)

        self.assertEqual(self.imparate(), ["forn2_v1"])
        # E il turno di quell'altro resta suo: non lo si toglie a nessuno.
        self.assertTrue(lucchetto.exists())

    def test_il_lucchetto_non_resta_in_giro_dopo_una_scrittura_riuscita(self) -> None:
        registro.scrivi_adattatore(self.voce(3), self.spedito)

        documento = registro.percorso_imparato(self.spedito)
        self.assertFalse(documento.with_name(documento.name + ".lock").exists())

    def test_e_nemmeno_dopo_una_scrittura_rifiutata(self) -> None:
        """Una voce senza «id» non si scrive: il turno va restituito lo stesso,
        altrimenti il primo rifiuto chiuderebbe il registro per venti secondi."""

        registro.scrivi_adattatore(self.voce(4), self.spedito)
        with self.assertRaises(ValueError):
            registro.scrivi_adattatore(
                {"id": "forn4_v1", "supplier_id": "un_altro", "kind": "supplier"}, self.spedito,
            )

        documento = registro.percorso_imparato(self.spedito)
        self.assertFalse(documento.with_name(documento.name + ".lock").exists())


class IlCandidatoMancatoPerUnPelo(unittest.TestCase):
    """«Non e' nessuno» contro «e' il suo, meno questa cosa qui».

    ⚠ Il 22 agosto 2026 il listino BETULLA e' uscito AMBIGUO sul PC del negozio:
    qualcuno l'aveva aperto in Excel e risalvato con la cella C1 svuotata, e la
    parola ORDINE non c'era piu'. Le altre quattro obbligatorie c'erano tutte e
    tutte al loro posto. Il programma ha detto «Nessuna firma nota
    sufficiente», e chi l'ha letto ha confermato a mano la mappatura guidata,
    portandosi a casa un adattatore imparato sopra quello spedito.

    Lo stato resta AMBIGUO in tutti i casi: qui si prova quello che il
    programma **dice**, non che legga un listino a cui manca un pezzo.
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
        intestazioni[2] = None                      # la cella C1, «ORDINE»
        esito = registro.riconosci(
            profilo_del_file(self.listino(intestazioni)), self.registro_con_cinque_obbligatorie(),
        )

        # Lo stato non cambia: non si legge un listino a cui manca una colonna.
        self.assertEqual(esito["state"], "AMBIGUO")
        self.assertIsNone(esito["adapter_id"])
        # Cambia quello che il programma dice di sapere.
        self.assertEqual(esito["quasi_adapter_id"], "tizio_v1")
        self.assertEqual(esito["quasi_missing"], [{"header": "ORDINE", "column": "C"}])
        self.assertEqual(esito["quasi_present"], 4)
        frase = esito["evidence"][0]
        self.assertIn("TIZIO", frase)
        self.assertIn("«ORDINE»", frase)
        self.assertIn("colonna C", frase)
        self.assertIn("Excel", frase)

    def test_tre_intestazioni_mancanti_non_sono_un_pelo(self) -> None:
        """Dire «e' il listino di TIZIO» avendone viste due su cinque sarebbe
        una bugia detta con sicurezza, che e' peggio di «non lo riconosco»."""

        intestazioni = [None, None, None, "DESCRIZIONE", "PREZZO"]
        esito = registro.riconosci(
            profilo_del_file(self.listino(intestazioni)), self.registro_con_cinque_obbligatorie(),
        )

        self.assertEqual(esito["state"], "AMBIGUO")
        self.assertEqual(esito["evidence"], ["Nessuna firma nota sufficiente"])
        self.assertNotIn("quasi_adapter_id", esito)

    def test_le_intestazioni_che_restano_devono_stare_dove_il_registro_dice(self) -> None:
        """La difesa contro il falso riconoscimento.

        Un documento di un altro fornitore che per caso condivide quattro nomi
        di colonna non li ha quasi mai anche negli stessi posti: senza questa
        verifica, «sembra il listino di TIZIO» finirebbe addosso a chiunque.
        """

        # Le stesse intestazioni, meno una, ma tutte spostate di una colonna.
        intestazioni = [None, "EAN", "CODART", "DESCRIZIONE", "PREZZO"]
        esito = registro.riconosci(
            profilo_del_file(self.listino(intestazioni)), self.registro_con_cinque_obbligatorie(),
        )

        self.assertEqual(esito["evidence"], ["Nessuna firma nota sufficiente"])

    def test_il_listino_che_si_legge_non_passa_di_qui(self) -> None:
        """Un documento completo resta SCHEMA_NOTO: la frase nuova non deve
        comparire dove non c'e' niente che manca."""

        esito = registro.riconosci(
            profilo_del_file(self.listino(self.INTESTAZIONI)), self.registro_con_cinque_obbligatorie(),
        )

        self.assertEqual(esito["state"], "SCHEMA_NOTO")
        self.assertEqual(esito["adapter_id"], "tizio_v1")


class QuelloCheSImparaNonCancellaQuelloCheSiSpedisce(unittest.TestCase):
    """Gli adattatori `__locale`, e chi vince quando i candidati sono due.

    ⚠ Fino al 22 agosto 2026 una mappatura confermata su un fornitore spedito
    si scriveva con lo stesso `id`, e a parita' di `id` vince l'imparato: la
    voce spedita spariva sotto con tutto quello che porta e che la mappatura
    guidata non chiede. Sul PC del negozio e' successo su tre fornitori in un
    colpo solo, il 21 agosto alle 17:16.
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
        """`adattatore_base` non deve inventare una derivazione dove non c'è."""

        self.assertEqual(registro.adattatore_base("quercia_v1"), "quercia_v1")
        self.assertEqual(registro.adattatore_base(""), "")
        self.assertEqual(registro.adattatore_base(None), "")

    def test_gli_id_spediti_si_leggono_senza_l_imparato(self) -> None:
        """E' la domanda che serve per sapere se si sta per scrivere sopra
        qualcosa che il programma porta con se'."""

        spedito = registro_di_prova(self.radice / "adapters.json", [
            {"id": "tizio_v1", "supplier_id": "tizio"},
        ])
        registro_di_prova(registro.percorso_imparato(spedito), [
            {"id": "caio_v1", "supplier_id": "caio"},
        ])

        self.assertEqual(registro.identificativi_spediti(spedito), {"tizio_v1"})
        # Il registro effettivo invece li ha tutti e due: sono due domande
        # diverse, e confonderle rimetterebbe il difetto dov'era.
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
        """La regola che rende innocua la convivenza.

        Lo stesso documento somiglia a due adattatori: quello spedito, che pero'
        dichiara un foglio che qui non c'e' piu', e quello imparato sopra di lui,
        che dichiara quello giusto. A vincere non deve essere chi viene prima
        nell'elenco — sarebbe lo spedito — ma chi passa le verifiche.
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
        """Quando tutti e due leggono il documento senza un guasto vince
        l'imparato: e' la risposta piu' recente, e l'ha data qualcuno che aveva
        il documento davanti.

        ⚠ Fino al 22 agosto 2026 vinceva lo spedito, cioe' chi veniva prima
        nell'elenco, e quella riga teneva chiusa la strada della colonna
        d'ordine: la voce che nasce spostandola legge il documento esattamente
        come la spedita — cambia solo dove si scrive l'ordine — quindi le due
        pareggiano e la voce nuova restava inerte.
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
        """La regola nuova vale fra le due versioni della STESSA cosa.

        Fra un adattatore spedito e l'imparato di un altro fornitore non c'e'
        un piu' recente e un meno recente: c'e' solo l'ordine del registro, e
        quello non cambia. Allargare qui la regola vorrebbe dire far vincere un
        `caio_v1__locale` sopra un `tizio_v1` spedito che legge il documento
        altrettanto bene, per il solo fatto di essere stato imparato dopo.
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
        """Chi rilegge fra un mese deve trovarci dentro da dove si era partiti."""

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
        # E la spedita non e' stata toccata.
        spedite = json.loads(spedito.read_text(encoding="utf-8"))["adapters"]
        self.assertEqual([voce["id"] for voce in spedite], ["tizio_v1"])


class IntestazioniDeiLettoriDedicati(unittest.TestCase):
    """I lettori a schema noto pretendono quello che il REGISTRO dichiara.

    Il 4 settembre 2026 BETULLA ha scritto «Ordine» invece di «ORDINE». Il
    registro l'ha riconosciuto lo stesso — `normalizza` la cassa non la guarda —
    e `read_betulla`, che confrontava cinque nomi scritti nel codice lettera per
    lettera, ha messo il veto su un listino sano da 6.430 prodotti: cinque
    confronti fermi di fila con «Schema BETULLA non riconosciuto», e in negozio
    nessun ordine. Erano due definizioni della stessa cosa, e queste prove
    servono a tenerne una sola.
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
        """«Cod.Art.», «COD ART», «Cod. Art.»: il docstring di `normalizza` lo
        promette da sempre, e adesso vale anche per il lettore."""

        percorso = scrivi_foglio(self.radice / "betulla_punti.xlsx", [
            ["EAN", "Cod. Art.", "ORDINE", "Descr.Commerciale", "PzCt",
             "Cessione", "Pedana", "Iva", "TOTALI"],
            ["8000000000001", "C-001", None, "Prodotto Alfa", 6, 1.25, 60, 22, 0],
        ], "Listino")

        self.assertEqual(len(prepare_sources.read_betulla(percorso)), 1)

    def test_a_betulla_manca_una_colonna_e_il_motivo_si_legge(self) -> None:
        """Il rifiuto resta, e adesso dice quale colonna manca.

        «Schema BETULLA non riconosciuto» da solo non ha detto a nessuno che il
        problema era una parola in minuscolo.
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
        """Nessun elenco di intestazioni scritto nel codice del lettore."""

        self.assertEqual(
            sorted(prepare_sources.intestazioni_obbligatorie("betulla_v1")),
            ["cessione", "codart", "ean", "ordine", "pzct"],
        )
        self.assertNotIn('"CodArt"', PERCORSO_LETTORI.read_text(encoding="utf-8"))

    def test_noce_csv_letto_con_la_cassa_cambiata(self) -> None:
        """E le righe si leggono davvero, non solo la riga d'intestazione.

        Rendere tollerante il solo controllo sarebbe stato peggio del difetto:
        il CSV legge per nome, e ogni campo sarebbe uscito vuoto — un fornitore
        letto con zero righe utilizzabili e nessuno che lo dice.
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
    """Dal 5 settembre 2026: una voce imparata sopra una spedita vale finche'
    la spedita e' quella su cui e' nata.

    ⚠ Il caso vero, tre volte in due settimane: il titolare conferma una mappatura
    a mano (BETULLA e le offerte CIPRESSO il 21 agosto, il canvass nuovo di
    LARICE il 4 settembre), Daniele spedisce la voce fatta bene, e quella
    imparata le resta sopra finche' qualcuno non apre il file sul PC del
    negozio e la cancella. Da qui in poi lo fa il programma.
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
        """E' la correzione che finalmente arriva: Daniele cambia la voce,
        l'aggiornamento la porta al negozio, e l'imparata smette di coprirla."""

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
        """Sono tutte quelle imparate prima del 5 settembre 2026, cioe' quelle
        che `PROMPT_PC_NEGOZIO_ADATTATORI.md` faceva cancellare a mano — anche
        quando portano ancora l'id spedito, come le tre del 21 agosto."""

        registro_di_prova(registro.percorso_imparato(self.spedito), [
            {"id": "tizio_v1__locale", "supplier_id": "tizio", "derivato_da": "tizio_v1"},
            {"id": "tizio_v1", "supplier_id": "tizio", "header_signature": {"kind": "headers"}},
            {"id": "caio_v1", "supplier_id": "caio"},
        ])

        self.assertEqual(self.ids_effettivi(), ["tizio_v1", "caio_v1"])
        # E la spedita e' proprio la spedita, non la fotocopia imparata.
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
        # La seconda volta non c'e' piu' niente da spostare, e il file non si tocca.
        prima = registro.percorso_imparato(self.spedito).read_bytes()
        self.assertEqual(registro.metti_da_parte_le_superate(self.spedito), [])
        self.assertEqual(registro.percorso_imparato(self.spedito).read_bytes(), prima)

    def test_riconfermare_dalla_mappatura_rimette_in_gioco_la_voce(self) -> None:
        """Il giro completo: spedita cambiata, imparata superata, l'utente
        conferma di nuovo, e la voce nuova porta il timbro della spedita di
        adesso."""

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

