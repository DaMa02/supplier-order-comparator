"""Le copie `.xlsx` che si consegnano ai fornitori: che siano il loro listino.

Il 12 agosto 2026 la copia LARICE consegnabile aveva **34 celle della colonna
EAN** con scritto il testo «1235» al posto del nulla e aveva perso **641 titoli
di sezione** dalla colonna d'ordine — e il programma diceva che era andato tutto
bene.  Il writer Node non aveva **nessun** collaudo: questo file e' il suo.

Quattro cose valgono da sole l'intero file:

1. **Una stringa condivisa vuota non deve diventare il suo indice.**  La
   libreria che scrive gli `.xlsx` legge male quelle celle e ci mette dentro il
   numero della voce di `sharedStrings.xml`.  Il listino di prova se lo
   costruisce questo collaudo a mano, XML compreso: openpyxl una stringa
   condivisa vuota non la genera, e senza quella cella il difetto non si vede.
2. **Una riga che prodotto non e' non si cancella.**  Azzerare le quantita'
   preesistenti resta giusto — senza, si spedirebbero righe fantasma — ma su
   LARICE la colonna d'ordine porta i titoli delle sezioni, e cancellare tutta
   la colonna li portava via.  Una quantita' e' un numero.
3. **Non ci si fida della libreria.**  Dopo la scrittura si riapre la copia e la
   si confronta cella per cella con il listino di partenza: le uniche
   differenze ammesse sono nella colonna d'ordine.  Qualunque altra fa fallire
   la compilazione di quel fornitore, e la copia non esce.
4. **Quello che si ferma lo dice in italiano.**  Il listino cambiato dopo la
   verifica arrivava all'utente come traccia di Node con i percorsi assoluti,
   mentre il writer la frase pronta ce l'aveva gia'.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest import mock

from openpyxl import Workbook, load_workbook

SKILL_ROOT = Path(__file__).resolve().parents[1]
for cartella in (SKILL_ROOT / "app", SKILL_ROOT / "tests"):
    if str(cartella) not in sys.path:
        sys.path.insert(0, str(cartella))

import test_web_app as banco_web  # noqa: E402
from copia_fedele import (  # noqa: E402
    ConfrontoImpossibile,
    confronta_copia,
    frase_di_rifiuto,
)

WRITER = SKILL_ROOT / "scripts" / "write_supplier_orders.mjs"


# ---------------------------------------------------------------------------
# Un `.xlsx` costruito a mano, perche' openpyxl non sa fare quello che serve
# ---------------------------------------------------------------------------

_TIPI = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
    '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
    "</Types>"
)
_RELAZIONI = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
    "</Relationships>"
)
_RELAZIONI_LIBRO = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
    '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    "</Relationships>"
)
_STILI = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
    '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
    "<borders count=\"1\"><border/></borders>"
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    "</styleSheet>"
)


def listino_con_stringhe_vuote(percorso: Path, foglio: str = "Listino") -> Path:
    """Il listino di prova, scritto XML per XML.

    Perche' a mano: openpyxl **non genera** una voce di `sharedStrings.xml` con
    il testo vuoto, e senza quella voce referenziata da una cella il difetto che
    questo collaudo difende non esiste nel file.

    Quello che c'e' dentro, e che serve tutto:

    | riga | A (EAN)                      | B             | C (ordine)       |
    |------|------------------------------|---------------|------------------|
    | 1    | EAN                          | DESCRIZIONE   | ORDINE           |
    | 2    | *stringa condivisa vuota*    | PRODOTTO DUE  | 3 (preesistente) |
    | 3    | 8000000000003                | PRODOTTO TRE  | — (il piano: 5)  |
    | 4    | —                            | —             | SEZIONE SOLARI   |
    | 5    | *stringa condivisa vuota*    | PRODOTTO CIN. | 7 (preesistente) |
    """

    condivise = [
        "EAN",              # 0
        "DESCRIZIONE",      # 1
        "ORDINE",           # 2
        "",                 # 3  <- la voce vuota: e' tutto il punto
        "PRODOTTO DUE",     # 4
        "8000000000003",    # 5
        "PRODOTTO TRE",     # 6
        "SEZIONE SOLARI",   # 7
        "PRODOTTO CINQUE",  # 8
    ]
    voci = "".join(
        "<si><t/></si>" if not testo else f'<si><t xml:space="preserve">{testo}</t></si>'
        for testo in condivise
    )
    sst = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        f'count="{len(condivise)}" uniqueCount="{len(condivise)}">{voci}</sst>'
    )
    righe = (
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="C1" t="s"><v>2</v></c></row>'
        '<row r="2"><c r="A2" s="0" t="s"><v>3</v></c><c r="B2" t="s"><v>4</v></c><c r="C2"><v>3</v></c></row>'
        '<row r="3"><c r="A3" t="s"><v>5</v></c><c r="B3" t="s"><v>6</v></c></row>'
        '<row r="4"><c r="C4" t="s"><v>7</v></c></row>'
        '<row r="5"><c r="A5" s="0" t="s"><v>3</v></c><c r="B5" t="s"><v>8</v></c><c r="C5"><v>7</v></c></row>'
    )
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{righe}</sheetData></worksheet>"
    )
    libro = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{foglio}" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    percorso.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(percorso, "w", zipfile.ZIP_DEFLATED) as contenitore:
        contenitore.writestr("[Content_Types].xml", _TIPI)
        contenitore.writestr("_rels/.rels", _RELAZIONI)
        contenitore.writestr("xl/workbook.xml", libro)
        contenitore.writestr("xl/_rels/workbook.xml.rels", _RELAZIONI_LIBRO)
        contenitore.writestr("xl/styles.xml", _STILI)
        contenitore.writestr("xl/sharedStrings.xml", sst)
        contenitore.writestr("xl/worksheets/sheet1.xml", sheet)
    return percorso


def xlsx_a_mano(percorso: Path, corpo_foglio: str, foglio: str = "Listino") -> Path:
    """Un `.xlsx` con il corpo del foglio scritto a mano, tutto il resto minimo.

    Serve per le forme che openpyxl non genera mai da se': una `<dimension>`
    diversa dai dati veri, le righe scritte fuori ordine.  Le celle si scrivono
    come `inlineStr` o numeri, cosi' non serve una `sharedStrings` vera.
    """

    sst = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'count="0" uniqueCount="0"/>'
    )
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"{corpo_foglio}</worksheet>"
    )
    libro = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{foglio}" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    percorso.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(percorso, "w", zipfile.ZIP_DEFLATED) as contenitore:
        contenitore.writestr("[Content_Types].xml", _TIPI)
        contenitore.writestr("_rels/.rels", _RELAZIONI)
        contenitore.writestr("xl/workbook.xml", libro)
        contenitore.writestr("xl/_rels/workbook.xml.rels", _RELAZIONI_LIBRO)
        contenitore.writestr("xl/styles.xml", _STILI)
        contenitore.writestr("xl/sharedStrings.xml", sst)
        contenitore.writestr("xl/worksheets/sheet1.xml", sheet)
    return percorso


def listino_semplice(percorso: Path, foglio: str, righe: list[list[Any]]) -> Path:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = foglio
    for riga in righe:
        sheet.append(riga)
    workbook.save(percorso)
    workbook.close()
    return percorso


def node_disponibile() -> Path | None:
    candidato = Path(sys.executable).resolve().parents[1] / "node" / "bin" / "node.exe"
    if candidato.is_file():
        return candidato
    trovato = shutil.which("node")
    return Path(trovato) if trovato else None


# ---------------------------------------------------------------------------
# 1. Il writer Node, sul campo
# ---------------------------------------------------------------------------


class IlWriterNodeSulCampo(unittest.TestCase):
    """Una sola scrittura vera, e tutte le domande le si fanno a quella.

    La libreria ci mette una decina di secondi solo a caricarsi: rifare la
    scrittura per ogni asserzione allungherebbe la suite di minuti senza
    provare niente di piu'.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.node = node_disponibile()
        if cls.node is None:
            raise unittest.SkipTest("Node non disponibile per il writer XLSX")
        cls.temporanea = tempfile.TemporaryDirectory()
        cls.cartella = Path(cls.temporanea.name)
        cls.uscita = cls.cartella / "uscita"
        cls.sorgente = listino_con_stringhe_vuote(cls.cartella / "listino_larice.xlsx")
        cls.impronta_prima = hashlib.sha256(cls.sorgente.read_bytes()).hexdigest()
        cls.piano = {
            "orders": [
                {"supplier": "larice", "supplier_source_row": 3, "quantity": 5},
            ]
        }
        cls.esito = cls._scrivi(cls.sorgente, cls.piano, cls.impronta_prima)
        cls.copia = cls.uscita / "ORDINE_LARICE_listino_larice.xlsx"

    @classmethod
    def tearDownClass(cls) -> None:
        temporanea = getattr(cls, "temporanea", None)
        if temporanea is not None:
            temporanea.cleanup()

    @classmethod
    def _scrivi(cls, sorgente: Path, piano: dict[str, Any], impronta: str) -> subprocess.CompletedProcess:
        piano_path = cls.cartella / "final_order_plan.json"
        piano_path.write_text(json.dumps(piano), encoding="utf-8")
        config = cls.cartella / "writer_config.json"
        config.write_text(json.dumps({
            "supplier_files": {"larice": str(sorgente)},
            "supplier_write_rules": {"larice": {
                "sheet": "Listino",
                "order_column": "C",
                "data_start_row": 2,
                "header_row": 1,
                "expected_header": "ORDINE",
                "source_sha256": impronta,
            }},
        }), encoding="utf-8")
        ambiente = os.environ.copy()
        # Dal 5 settembre 2026 il writer non ha nessuna libreria da cercare: la
        # variabile resta puntata su una cartella che non esiste per provare che
        # nessuno la legge piu'.
        ambiente["OAI_NODE_MODULES"] = str(cls.cartella / "cache-che-non-c-e")
        return subprocess.run(
            [
                str(cls.node), str(WRITER),
                "--plan", str(piano_path),
                "--config", str(config),
                "--output-dir", str(cls.uscita),
            ],
            cwd=SKILL_ROOT / "scripts",
            env=ambiente,
            capture_output=True,
            text=True,
            # Node scrive in UTF-8; senza dirlo, su Windows Python legge con la
            # codifica della console e la frase italiana arriva sfigurata.
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
        )

    def valore(self, cella: str) -> Any:
        libro = load_workbook(self.copia, read_only=True, data_only=False)
        try:
            return libro["Listino"][cella].value
        finally:
            libro.close()

    def test_la_compilazione_riesce_senza_nessuna_libreria(self) -> None:
        """`OAI_NODE_MODULES` punta al nulla e la compilazione riesce lo stesso:
        dal 5 settembre 2026 il writer usa solo quello che Node ha in casa."""

        self.assertEqual(self.esito.returncode, 0, self.esito.stderr or self.esito.stdout)
        self.assertTrue(self.copia.is_file())
        self.assertEqual(self.esito.stderr.strip(), "")

    def test_una_stringa_condivisa_vuota_non_diventa_il_suo_indice(self) -> None:
        """Il difetto B1: nella copia LARICE vera erano 34 celle EAN con «1235».

        `A2` e `A5` nel listino di partenza sono la voce numero **3** di
        `sharedStrings.xml`, che e' testo vuoto.  Se la copia porta «3», la
        libreria ha consegnato l'indice al posto del valore.
        """

        for cella in ("A2", "A5"):
            with self.subTest(cella=cella):
                valore = self.valore(cella)
                self.assertNotEqual(str(valore), "3", "la copia porta l'indice invece del vuoto")
                self.assertIn(valore, (None, ""), f"{cella} dovrebbe essere vuota, è {valore!r}")

    def test_una_riga_che_non_e_un_prodotto_sopravvive(self) -> None:
        """Il difetto M1: la copia LARICE vera perdeva 641 titoli di sezione."""

        self.assertEqual(self.valore("C4"), "SEZIONE SOLARI")
        # E l'intestazione, che sta sopra la prima riga di dati, nemmeno si tocca.
        self.assertEqual(self.valore("C1"), "ORDINE")

    def test_una_quantita_preesistente_su_una_riga_prodotto_viene_azzerata(self) -> None:
        """Senza questo si spedirebbero le righe ordinate la settimana scorsa."""

        for cella in ("C2", "C5"):
            with self.subTest(cella=cella):
                self.assertIn(self.valore(cella), (None, ""))

    def test_la_quantita_del_piano_arriva_nella_sua_cella(self) -> None:
        self.assertEqual(self.valore("C3"), 5)

    def test_il_listino_di_partenza_non_viene_toccato(self) -> None:
        self.assertEqual(hashlib.sha256(self.sorgente.read_bytes()).hexdigest(), self.impronta_prima)

    def test_la_guardia_cella_per_cella_promuove_questa_copia(self) -> None:
        """La prova delle prove: dopo il writer, la copia regge il confronto."""

        esito = confronta_copia(
            self.sorgente, self.copia,
            colonna_ordine="C", prima_riga=2, quantita={3: 5}, foglio_ordine="Listino",
        )
        self.assertTrue(esito.fedele, esito.esempi_rifiutati)
        # Tre differenze ammesse: i due azzeramenti e la quantita' del piano.
        self.assertEqual(esito.differenze_ammesse, 3)
        self.assertGreater(esito.celle_confrontate, 0)


class IlWriterNonConosceColonnePerContoSuo(unittest.TestCase):
    """I-2 della revisione avversariale del 13 agosto 2026.

    Il writer portava un ripiego cablato — «betulla in C, larice in D» — e un
    percorso senza `--config`: una configurazione a cui manca la regola di un
    fornitore selezionato faceva compilare con la colonna scritta nel codice,
    bypassando il registro.  «L'unica definizione di come si scrive l'ordine»
    deve valere anche per il programma che scrive davvero: senza regola ci si
    ferma e lo si dice, senza configurazione non si parte proprio.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.node = node_disponibile()
        if cls.node is None:
            raise unittest.SkipTest("Node non disponibile per il writer XLSX")
        cls.temporanea = tempfile.TemporaryDirectory()
        cls.cartella = Path(cls.temporanea.name)
        cls.uscita = cls.cartella / "uscita"
        cls.sorgente = listino_con_stringhe_vuote(cls.cartella / "listino_larice.xlsx")
        piano_path = cls.cartella / "final_order_plan.json"
        piano_path.write_text(json.dumps({
            "orders": [{"supplier": "larice", "supplier_source_row": 3, "quantity": 5}],
        }), encoding="utf-8")
        cls.piano_path = piano_path

    @classmethod
    def tearDownClass(cls) -> None:
        temporanea = getattr(cls, "temporanea", None)
        if temporanea is not None:
            temporanea.cleanup()

    def _lancia(self, argomenti: list[str]) -> subprocess.CompletedProcess:
        ambiente = os.environ.copy()
        ambiente["OAI_NODE_MODULES"] = str(self.cartella / "cache-che-non-c-e")
        return subprocess.run(
            [str(self.node), str(WRITER), *argomenti],
            cwd=SKILL_ROOT / "scripts",
            env=ambiente,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
        )

    def test_senza_configurazione_non_si_parte(self) -> None:
        esito = self._lancia(["--plan", str(self.piano_path), "--output-dir", str(self.uscita)])

        self.assertNotEqual(esito.returncode, 0)
        self.assertIn("--config", esito.stderr + esito.stdout)

    def test_una_regola_mancante_ferma_invece_di_ripiegare_sul_cablato(self) -> None:
        """Il ripiego avrebbe scritto in colonna D per conto suo, senza registro."""

        config = self.cartella / "writer_config.json"
        config.write_text(json.dumps({
            "supplier_files": {"larice": str(self.sorgente)},
            "supplier_write_rules": {},
        }), encoding="utf-8")

        esito = self._lancia([
            "--plan", str(self.piano_path),
            "--config", str(config),
            "--output-dir", str(self.uscita),
        ])

        self.assertNotEqual(esito.returncode, 0)
        self.assertIn("Manca la regola di scrittura verificata per LARICE",
                      esito.stderr + esito.stdout)
        self.assertFalse(list(self.uscita.glob("ORDINE_*")) if self.uscita.is_dir() else [],
                         "nessuna copia deve nascere da una configurazione monca")


class IlWriterQuandoIlListinoECambiato(unittest.TestCase):
    """Il difetto I7: la frase italiana, non la traccia di Node."""

    def setUp(self) -> None:
        self.node = node_disponibile()
        if self.node is None:
            self.skipTest("Node non disponibile per il writer XLSX")
        temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(temporanea.cleanup)
        self.cartella = Path(temporanea.name)

    def test_il_listino_cambiato_dopo_la_verifica_lo_dice_in_italiano(self) -> None:
        sorgente = listino_semplice(
            self.cartella / "listino_larice.xlsx", "Listino",
            [["EAN", "DESCRIZIONE", "ORDINE"], ["8000000000003", "PRODOTTO", None]],
        )
        piano = self.cartella / "final_order_plan.json"
        piano.write_text(json.dumps({
            "orders": [{"supplier": "larice", "supplier_source_row": 2, "quantity": 1}],
        }), encoding="utf-8")
        config = self.cartella / "writer_config.json"
        config.write_text(json.dumps({
            "supplier_files": {"larice": str(sorgente)},
            "supplier_write_rules": {"larice": {
                "sheet": "Listino",
                "order_column": "C",
                "data_start_row": 2,
                # L'impronta di un altro file: e' il caso vero, il listino
                # ricaricato dopo la verifica.
                "source_sha256": "0" * 64,
            }},
        }), encoding="utf-8")

        esito = subprocess.run(
            [
                str(self.node), str(WRITER),
                "--plan", str(piano),
                "--config", str(config),
                "--output-dir", str(self.cartella / "uscita"),
            ],
            cwd=SKILL_ROOT / "scripts",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
        )

        self.assertNotEqual(esito.returncode, 0)
        righe = [riga.strip() for riga in esito.stderr.splitlines() if riga.strip()]
        marcate = [riga for riga in righe if riga.startswith("ERRORE_COMPILAZIONE:")]
        self.assertEqual(len(marcate), 1, esito.stderr)
        self.assertIn("Il listino LARICE è cambiato dopo la verifica", marcate[0])
        # ⚠ Niente traccia di Node e niente percorsi assoluti: e' quello che
        # l'utente si trovava scritto in pagina.
        self.assertNotIn("    at ", esito.stderr)
        self.assertNotIn(str(self.cartella), esito.stderr)
        self.assertFalse(any((self.cartella / "uscita").glob("*.xlsx")))


class LIntestazioneDellaColonnaDOrdine(unittest.TestCase):
    """Come si verifica l'intestazione lo dice la regola, non il nome del fornitore.

    Il 14 agosto 2026 il writer aveva scritto dentro un `if (supplier ===
    "cipresso")` che la colonna doveva essere «G» e l'intestazione «ORDINE».
    Quella settimana il listino CIPRESSO la colonna G ce l'aveva **vuota** —
    l'utente l'aveva confermato nella mappatura guidata e il registro lo
    dichiarava — e la compilazione si e' fermata per tutti e tre i fornitori:
    zero copie prodotte, per una regola scritta nel codice contro una regola
    dichiarata nel registro.

    Quello che resta e' una domanda sola, uguale per chiunque: la regola dice
    che testo deve esserci, oppure dice che l'utente ha confermato che quella
    cella e' vuota. Se non dice ne' l'una ne' l'altra non si scrive.
    """

    def setUp(self) -> None:
        self.node = node_disponibile()
        if self.node is None:
            self.skipTest("Node non disponibile per il writer XLSX")
        temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(temporanea.cleanup)
        self.cartella = Path(temporanea.name)

    def compila(self, righe: list[list], regola: dict, fornitore: str = "cipresso"):
        sorgente = listino_semplice(self.cartella / "listino.xlsx", "Listino", righe)
        piano = self.cartella / "final_order_plan.json"
        piano.write_text(json.dumps({
            "orders": [{"supplier": fornitore, "supplier_source_row": 2, "quantity": 3}],
        }), encoding="utf-8")
        config = self.cartella / "writer_config.json"
        config.write_text(json.dumps({
            "supplier_files": {fornitore: str(sorgente)},
            "supplier_write_rules": {fornitore: {"sheet": "Listino", **regola}},
        }), encoding="utf-8")
        esito = subprocess.run(
            [
                str(self.node), str(WRITER),
                "--plan", str(piano),
                "--config", str(config),
                "--output-dir", str(self.cartella / "uscita"),
            ],
            cwd=SKILL_ROOT / "scripts",
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=180, check=False,
        )
        prodotte = sorted(p.name for p in (self.cartella / "uscita").glob("*.xlsx"))
        marcate = [
            riga.strip() for riga in esito.stderr.splitlines()
            if riga.strip().startswith("ERRORE_COMPILAZIONE:")
        ]
        return esito, prodotte, marcate

    def test_un_intestazione_vuota_confermata_si_compila(self) -> None:
        """Il caso vero del 14 agosto: colonna G, cella G1 vuota, si scrive."""

        esito, prodotte, marcate = self.compila(
            [["COD", "DESCRIZIONE", "UM", "QT", "LISTINO", "EAN", None],
             ["019654", "PRODOTTO", "PZ", 6, 4.9, "8000000000003", None]],
            {"order_column": "G", "data_start_row": 2, "header_row": 1,
             "blank_header_confirmed": True},
        )

        self.assertEqual(esito.returncode, 0, esito.stderr)
        self.assertEqual(marcate, [])
        self.assertEqual(len(prodotte), 1, esito.stderr)

    def test_il_nome_del_fornitore_non_decide_piu_niente(self) -> None:
        """Stessa regola, fornitori diversi: stesso esito.

        E' la prova che il cablato non c'e' piu': con `cipresso` scritto nel
        codice questa regola veniva rifiutata e le altre no.
        """

        for fornitore in ("cipresso", "larice", "un_fornitore_imparato"):
            with self.subTest(fornitore=fornitore):
                esito, prodotte, _marcate = self.compila(
                    [["COD", "DESCR", None], ["019654", "PRODOTTO", None]],
                    {"order_column": "C", "data_start_row": 2, "header_row": 1,
                     "blank_header_confirmed": True},
                    fornitore=fornitore,
                )
                self.assertEqual(esito.returncode, 0, esito.stderr)
                self.assertEqual(len(prodotte), 1, esito.stderr)

    def test_la_cella_che_non_e_piu_vuota_ferma_la_scrittura(self) -> None:
        """Fra la mappatura confermata e oggi il fornitore può aver messo un titolo."""

        esito, prodotte, marcate = self.compila(
            [["COD", "DESCR", "QUANTITA'"], ["019654", "PRODOTTO", None]],
            {"order_column": "C", "data_start_row": 2, "header_row": 1,
             "blank_header_confirmed": True},
        )

        self.assertNotEqual(esito.returncode, 0)
        self.assertEqual(prodotte, [])
        self.assertEqual(len(marcate), 1, esito.stderr)
        self.assertIn("dice che quella cella è vuota", marcate[0])
        self.assertIn("QUANTITA'", marcate[0])

    def test_un_listino_senza_riga_di_intestazione_si_compila(self) -> None:
        """E' la forma vera della regola LARICE: intestazione non ne ha.

        ⚠ La prima versione di questa correzione pretendeva da tutti una
        dichiarazione sull'intestazione, e alla prova nel browser ha bloccato
        LARICE — cioe' aveva sostituito un cablato con una regola inventata.
        Qui si verifica **quello che la configurazione dichiara**, e chi non
        dichiara nessuna intestazione non ne ha una da verificare.
        """

        esito, prodotte, _marcate = self.compila(
            [["019654", "PRODOTTO", None], ["019655", "ALTRO", None]],
            {"order_column": "C", "data_start_row": 1},
            fornitore="larice",
        )

        self.assertEqual(esito.returncode, 0, esito.stderr)
        self.assertEqual(len(prodotte), 1, esito.stderr)

    def test_l_intestazione_attesa_si_verifica_come_prima(self) -> None:
        esito, prodotte, marcate = self.compila(
            [["COD", "DESCR", "TOTALE"], ["019654", "PRODOTTO", None]],
            {"order_column": "C", "data_start_row": 2, "header_row": 1,
             "expected_header": "ORDINE"},
        )

        self.assertNotEqual(esito.returncode, 0)
        self.assertEqual(prodotte, [])
        self.assertIn("Intestazione non verificata", marcate[0])


class IlWriterQuandoIlGuastoNonEPrevisto(unittest.TestCase):
    """La marca `ERRORE_COMPILAZIONE:` va SOLO sulle frasi scritte dal writer.

    ⚠ La revisione avversariale del 13 agosto 2026 ha mostrato che il `catch`
    finale marcava qualunque messaggio: in pagina arrivavano `ENOENT ... C:\\...`
    con i percorsi del computer e le chiavi di risorsa .NET della libreria
    (`Arg_ArgumentOutOfRangeException`), vestiti da spiegazione italiana.  Il
    dettaglio tecnico deve uscire NON marcato — cosi' resta sulla console — e
    la riga marcata deve essere una frase che Daniele puo' leggere.

    Questi collaudi non caricano la libreria (il guasto arriva prima), quindi
    costano meno di un secondo l'uno.
    """

    def setUp(self) -> None:
        self.node = node_disponibile()
        if self.node is None:
            self.skipTest("Node non disponibile per il writer XLSX")
        temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(temporanea.cleanup)
        self.cartella = Path(temporanea.name)
        self.config = self.cartella / "writer_config.json"
        self.config.write_text(json.dumps({"supplier_files": {}}), encoding="utf-8")

    def esegui(self, piano: Path, uscita: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(self.node), str(WRITER),
                "--plan", str(piano),
                "--config", str(self.config),
                "--output-dir", str(uscita),
            ],
            cwd=SKILL_ROOT / "scripts",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
        )

    def marcate(self, stderr: str) -> list[str]:
        return [
            riga.strip() for riga in stderr.splitlines()
            if riga.strip().startswith("ERRORE_COMPILAZIONE:")
        ]

    def test_un_piano_che_manca_da_una_frase_italiana_senza_percorsi(self) -> None:
        esito = self.esegui(self.cartella / "piano_che_non_esiste.json", self.cartella / "uscita")

        self.assertNotEqual(esito.returncode, 0)
        marcate = self.marcate(esito.stderr)
        self.assertEqual(len(marcate), 1, esito.stderr)
        self.assertIn("Il piano ordini non si riesce a leggere", marcate[0])
        # Il percorso e l'ENOENT stanno nel `cause`, fuori dalla marca: la
        # console li vede, la pagina no.
        self.assertNotIn("ENOENT", marcate[0])
        self.assertNotIn(str(self.cartella), marcate[0])
        self.assertIn("ENOENT", esito.stderr)

    # Solo Windows, e adesso lo dichiara. Il guasto lo provoca un nome di
    # cartella che Windows rifiuta: altrove `uscita<illegale>` e' un nome
    # valido, la scrittura riesce, e la prova falliva perche' NON c'era
    # l'errore che pretendeva. Restava rossa su ogni Mac senza che nessuno
    # avesse rotto niente.
    # ⚠ Manca il gemello che valga ovunque: un guasto imprevisto provocato in
    # un modo indipendente dal sistema operativo.
    @unittest.skipUnless(os.name == "nt", "solo Windows rifiuta questo nome di cartella")
    def test_un_guasto_imprevisto_da_la_frase_generica_e_il_dettaglio_resta_fuori_dalla_marca(self) -> None:
        piano = self.cartella / "final_order_plan.json"
        piano.write_text(json.dumps({"orders": []}), encoding="utf-8")
        # Una cartella d'uscita che Windows non puo' creare: il writer non ha
        # una frase per questo guasto, e non deve inventarla marcando l'errore
        # di sistema.
        esito = self.esegui(piano, self.cartella / 'uscita<illegale>')

        self.assertNotEqual(esito.returncode, 0)
        marcate = self.marcate(esito.stderr)
        self.assertEqual(len(marcate), 1, esito.stderr)
        self.assertIn("guasto imprevisto", marcate[0])
        self.assertIn("Il piano ordini è completo", marcate[0])
        self.assertNotIn(str(self.cartella), marcate[0])
        # Il dettaglio vero c'e', ma non marcato.
        self.assertIn("Error", esito.stderr.replace(marcate[0], ""))


class LaDecodificaDellUscitaDelWriter(banco_web.ConsegnaBase):
    """La frase del writer attraversa `subprocess` senza sfigurarsi.

    ⚠ Node scrive UTF-8.  Senza dirlo a `subprocess.run`, su Windows Python
    decodifica con cp1252 e «Il listino LARICE è cambiato» arrivava in pagina
    come «Il listino LARICE Ã¨ cambiato».  Il collaudo che c'era sostituiva
    `subprocess.run` con una funzione che ignorava gli argomenti, quindi la
    codifica non la osservava nessuno: la revisione avversariale del 13 agosto
    2026 l'ha riportata a cp1252 con la suite verde.  Qui lo spawn e' vero.
    """

    def setUp(self) -> None:
        super().setUp()
        self.node = node_disponibile()
        if self.node is None:
            self.skipTest("Node non disponibile per il writer XLSX")

    def test_la_e_accentata_del_writer_arriva_intera(self) -> None:
        script = self.root / "finto_writer.mjs"
        script.write_text(
            'console.error("ERRORE_COMPILAZIONE: il listino di prova è cambiato: '
            'perché adesso è un\'altra versione");\n'
            "process.exit(1);\n",
            encoding="utf-8",
        )
        self.writer_config.write_text(json.dumps({
            "node_executable": str(self.node),
            "writer_script": str(script),
            "supplier_files": {"larice": str(self.listini["larice"])},
        }), encoding="utf-8")
        cartella = self.orders_dir / "2026-08-13_0900"
        cartella.mkdir(parents=True)
        piano = cartella / "final_order_plan.json"
        piano.write_text(json.dumps({"orders": [{"supplier": "larice"}]}), encoding="utf-8")

        with self.assertRaises(ValueError) as errore:
            self.store.run_writer(piano, cartella)

        messaggio = str(errore.exception)
        self.assertIn("perché", messaggio)
        self.assertIn("è cambiato", messaggio)
        self.assertNotIn("Ã", messaggio)


# ---------------------------------------------------------------------------
# 1-bis. La riga di destinazione porta ancora il prodotto del piano?
# ---------------------------------------------------------------------------


def listino_con_ean(percorso: Path, foglio: str = "Listino") -> Path:
    """Il listino su cui si collauda la verifica della riga di destinazione.

    | riga | A (EAN)             | B (DESCRIZIONE)         | C (ORDINE) |
    |------|---------------------|-------------------------|------------|
    | 1    | EAN                 | DESCRIZIONE             | ORDINE     |
    | 2    | 8000000000002       | PRODOTTO DUE            | 4          |
    | 3    | 8000000000003       | PRODOTTO TRE            | —          |
    | 4    | 8000000000004       | ESPOSITORE MISTO        | —          |
    | 5    | —                   | SCATOLA REGALO NATALE   | —          |
    | 6    | 8000000000006 (num) | PRODOTTO SEI            | —          |
    | 7    | —                   | CAFFÈ  MISCELA-ORO 250g | —          |

    ⚠ La riga 6 porta l'EAN come **numero** e non come testo: e' la forma in cui
    i fogli di calcolo lo restituiscono meta' delle volte, ed e' la ragione per
    cui il confronto normalizza invece di guardare i byte.
    """

    return listino_semplice(percorso, foglio, [
        ["EAN", "DESCRIZIONE", "ORDINE"],
        ["8000000000002", "PRODOTTO DUE", 4],
        ["8000000000003", "PRODOTTO TRE", None],
        ["8000000000004", "ESPOSITORE MISTO", None],
        [None, "SCATOLA REGALO NATALE", None],
        [8000000000006, "PRODOTTO SEI", None],
        [None, "CAFFÈ  MISCELA-ORO 250g", None],
        ["8000000000008", "PRODOTTO OTTO", None],
    ])


def riepilogo_del_writer(stdout: str) -> dict[str, Any]:
    """Il riepilogo di fine lavoro, preso dalla riga marcata.

    ⚠ Su `stdout` non c'e' solo il writer: la libreria dei fogli di calcolo ci
    scrive «Inspect result written to file: C:\\...» a ogni salvataggio, e chi
    leggesse tutto `stdout` come JSON si fermerebbe su quella riga.  Il writer
    marca il riepilogo come marca gli errori, e qui si prende quella riga.
    """

    marca = "RIEPILOGO_COMPILAZIONE:"
    marcate = [riga.strip() for riga in stdout.splitlines() if riga.strip().startswith(marca)]
    if len(marcate) != 1:
        raise AssertionError(f"attesa una riga di riepilogo, trovate {len(marcate)}: {stdout!r}")
    return json.loads(marcate[0][len(marca):])


def esegui_writer(
    node: Path, cartella: Path, piano: dict[str, Any], config: dict[str, Any], uscita: Path,
) -> subprocess.CompletedProcess[str]:
    """Il writer vero, con il piano e la configurazione scritti su disco."""

    piano_path = cartella / "final_order_plan.json"
    piano_path.write_text(json.dumps(piano), encoding="utf-8")
    config_path = cartella / "writer_config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    ambiente = os.environ.copy()
    # Come sopra: la cache del runtime di sviluppo punta al nulla apposta.
    ambiente["OAI_NODE_MODULES"] = str(cartella / "cache-che-non-c-e")
    return subprocess.run(
        [
            str(node), str(WRITER),
            "--plan", str(piano_path),
            "--config", str(config_path),
            "--output-dir", str(uscita),
        ],
        cwd=SKILL_ROOT / "scripts",
        env=ambiente,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )


class LaVerificaDellaRigaDiDestinazione(unittest.TestCase):
    """Il difetto del 12 agosto 2026, dalla parte del writer Node.

    Un ordine e' finito sulla riga 2600 — olio Carapelli invece del prodotto
    atteso.  Per BETULLA, LARICE ed CIPRESSO la quantita' andava in `colonna+riga`
    alla cieca: in tutto il writer non c'era **una sola** occorrenza di EAN, e
    lo sha256 del listino difende dal file cambiato, non da un
    `supplier_source_row` sbagliato.  Il controllo esisteva solo per Noce,
    dentro `app/xls_writer.py`.

    Una sola scrittura vera, e tutte le domande le si fanno a quella: la
    libreria ci mette una decina di secondi solo a caricarsi.  Il fornitore e'
    apposta uno che nella tabella cablata del writer non c'e', cosi' la stessa
    compilazione collauda anche il nome leggibile che arriva dalla
    configurazione.
    """

    NOME_LEGGIBILE = "D'Alessio & Figli S.r.l."

    @classmethod
    def setUpClass(cls) -> None:
        cls.node = node_disponibile()
        if cls.node is None:
            raise unittest.SkipTest("Node non disponibile per il writer XLSX")
        cls.temporanea = tempfile.TemporaryDirectory()
        cls.cartella = Path(cls.temporanea.name)
        cls.uscita = cls.cartella / "uscita"
        cls.sorgente = listino_con_ean(cls.cartella / "listino_nuovo.xlsx")
        impronta = hashlib.sha256(cls.sorgente.read_bytes()).hexdigest()
        piano = {"orders": [
            # L'EAN coincide: la riga e' quella giusta e si scrive.
            {"supplier": "nuovo_fornitore", "supplier_source_row": 3,
             "supplier_ean": "8000000000003", "supplier_description": "PRODOTTO TRE", "quantity": 5},
            # L'espositore: il piano l'EAN non ce l'ha, il listino si'.  Avviso,
            # non errore — e' il caso che su LARICE bloccherebbe ordini veri.
            {"supplier": "nuovo_fornitore", "supplier_source_row": 4,
             "supplier_ean": "", "supplier_description": "ESPOSITORE MISTO", "quantity": 2},
            # Nessun EAN da nessuna parte: si guarda la descrizione, e non
            # coincide.  Avviso, non errore.
            {"supplier": "nuovo_fornitore", "supplier_source_row": 5,
             "supplier_ean": "", "supplier_description": "SCATOLA REGALO", "quantity": 1},
            # Lo stesso codice scritto in due modi.
            {"supplier": "nuovo_fornitore", "supplier_source_row": 6,
             "supplier_ean": "8000000000006.0", "supplier_description": "PRODOTTO SEI", "quantity": 3},
            # Solo la descrizione, e coincide a meno di accenti e punteggiatura.
            {"supplier": "nuovo_fornitore", "supplier_source_row": 7,
             "supplier_ean": "", "supplier_description": "caffe miscela oro 250G", "quantity": 7},
            # Il piano l'EAN non ce l'ha, il listino si', e la descrizione dice
            # un'altra merce: qui l'avviso ci vuole, e deve dire tutte e due le
            # cose.
            {"supplier": "nuovo_fornitore", "supplier_source_row": 8,
             "supplier_ean": "", "supplier_description": "TUTT'ALTRA MERCE", "quantity": 1},
        ]}
        config = {
            "supplier_files": {"nuovo_fornitore": str(cls.sorgente)},
            "supplier_write_rules": {"nuovo_fornitore": {
                "sheet": "Listino",
                "order_column": "C",
                "data_start_row": 2,
                "header_row": 1,
                "expected_header": "ORDINE",
                "source_sha256": impronta,
                "display_name": cls.NOME_LEGGIBILE,
                # La lettera e il numero 1-based sono tutti e due leciti.
                "verify": {"ean_column": "A", "description_column": 2},
            }},
        }
        cls.esito = esegui_writer(cls.node, cls.cartella, piano, config, cls.uscita)
        cls.copia = cls.uscita / "ORDINE_D_ALESSIO_FIGLI_S_R_L_listino_nuovo.xlsx"

    @classmethod
    def tearDownClass(cls) -> None:
        temporanea = getattr(cls, "temporanea", None)
        if temporanea is not None:
            temporanea.cleanup()

    def riepilogo(self) -> dict[str, Any]:
        return riepilogo_del_writer(self.esito.stdout)["supplier_copies"][0]

    def valore(self, cella: str) -> Any:
        libro = load_workbook(self.copia, read_only=True, data_only=False)
        try:
            return libro["Listino"][cella].value
        finally:
            libro.close()

    def test_la_compilazione_riesce_e_la_copia_esiste(self) -> None:
        self.assertEqual(self.esito.returncode, 0, self.esito.stderr or self.esito.stdout)
        self.assertTrue(self.copia.is_file(), sorted(p.name for p in self.uscita.iterdir()))

    def test_la_riga_con_l_ean_giusto_viene_scritta(self) -> None:
        self.assertEqual(self.valore("C3"), 5)

    def test_lo_stesso_codice_scritto_in_due_modi_e_lo_stesso_prodotto(self) -> None:
        """`8000000000006.0` nel piano, `8000000000006` nel listino: si scrive.

        ⚠ Senza la normalizzazione questa riga sarebbe un **errore** e il
        fornitore non riceverebbe niente, per una coda decimale.
        """

        self.assertEqual(self.valore("C6"), 3)

    def test_la_riga_senza_ean_nel_piano_la_conferma_la_descrizione(self) -> None:
        """L'espositore: Noce qui fallisce, il writer no — e non deve.

        La riga padre di un espositore LARICE l'EAN ce l'ha e il piano no:
        fermarsi bloccherebbe un ordine legittimo. Si scrive.

        ⚠ E non si avvisa nemmeno, **se la descrizione conferma la riga**. Prima
        questo ramo usciva con un avviso senza guardare la descrizione, che il
        registro dichiara: per gli espositori voleva dire un avviso a settimana
        su una riga che il listino conferma (revisione avversariale del 14
        agosto 2026).
        """

        self.assertEqual(self.valore("C4"), 2)
        avvisi = self.riepilogo()["warnings"]
        self.assertFalse([avviso for avviso in avvisi if "riga 4" in avviso], avvisi)

    def test_la_descrizione_diversa_si_scrive_lo_stesso_con_un_avviso(self) -> None:
        """La descrizione del piano può essere stata ripulita a monte."""

        self.assertEqual(self.valore("C5"), 1)
        avvisi = self.riepilogo()["warnings"]
        self.assertIn(
            "La riga 5 del listino D'Alessio & Figli S.r.l. è descritta «SCATOLA REGALO NATALE» "
            "e il piano dice «SCATOLA REGALO»: la quantità è stata scritta lo stesso, "
            "controlla che sia la riga giusta.",
            avvisi,
        )

    def test_una_descrizione_uguale_a_meno_di_accenti_non_avvisa_nessuno(self) -> None:
        """«caffe miscela oro 250G» e «CAFFÈ  MISCELA-ORO 250g» sono la stessa riga.

        Se il confronto fosse alla lettera, ogni riga senza EAN uscirebbe con
        un avviso e l'elenco degli avvisi non lo leggerebbe piu' nessuno.
        """

        self.assertEqual(self.valore("C7"), 7)
        self.assertEqual(len(self.riepilogo()["warnings"]), 2, self.riepilogo()["warnings"])

    def test_l_ean_solo_nel_listino_con_la_descrizione_che_smentisce_avvisa_di_tutto(self) -> None:
        """Le due cose insieme, perché sono due indizi diversi della stessa riga."""

        self.assertEqual(self.valore("C8"), 1)
        avvisi = self.riepilogo()["warnings"]
        self.assertIn(
            "La riga 8 del listino D'Alessio & Figli S.r.l. porta l'EAN 8000000000008, che il "
            "piano non dichiara, ed è descritta «PRODOTTO OTTO» mentre il piano dice "
            "«TUTT'ALTRA MERCE»: la quantità è stata scritta lo stesso, controlla che sia la "
            "riga giusta.",
            avvisi,
        )

    def test_il_riepilogo_conta_le_righe_verificate_e_quelle_no(self) -> None:
        """I numeri che l'utente deve poter leggere a fine compilazione."""

        riepilogo = self.riepilogo()
        self.assertEqual(riepilogo["verified_rows"], 4)
        self.assertEqual(riepilogo["unverifiable_rows"], 2)
        self.assertEqual(riepilogo["written_rows"], 6)
        # Verificate e non verificabili, sommate, fanno le righe del piano.
        self.assertEqual(
            riepilogo["verified_rows"] + riepilogo["unverifiable_rows"],
            riepilogo["written_rows"],
        )

    def test_il_nome_leggibile_arriva_dalla_configurazione(self) -> None:
        """Il difetto 2: senza `display_name` qui si leggerebbe `NUOVO_FORNITORE`."""

        riepilogo = self.riepilogo()
        self.assertEqual(riepilogo["supplier"], "nuovo_fornitore")
        self.assertEqual(riepilogo["supplier_name"], self.NOME_LEGGIBILE)

    def test_il_nome_del_file_porta_il_nome_leggibile_ripulito(self) -> None:
        """⚠ Il nome del fornitore finisce nel nome del file consegnato.

        `D'Alessio & Figli S.r.l.` cosi' com'e' farebbe un file che Windows non
        scrive: i caratteri che un nome di file non regge diventano un trattino
        basso, e la frase per l'utente resta quella dichiarata.
        """

        self.assertEqual(self.copia.name, "ORDINE_D_ALESSIO_FIGLI_S_R_L_listino_nuovo.xlsx")
        self.assertFalse(list(self.uscita.glob("*NUOVO_FORNITORE*")))
        self.assertEqual(Path(self.riepilogo()["destination"]), self.copia)


class LaPuliziaDelleCopieVecchie(unittest.TestCase):
    """Nella cartella d'uscita non deve restare l'ordine della volta scorsa.

    ⚠ La pulizia cancellava il nome che il writer costruisce **oggi**, e il
    nome lo fa il `display_name`: bastava che il fornitore fosse stato
    ribattezzato dall'ultima compilazione perche' la copia di allora si
    chiamasse in un altro modo, restasse li' e uscisse insieme a quella nuova
    — due ordini per lo stesso fornitore, con dentro numeri diversi, e niente
    che lo dica.  Oggi la cartella e' sempre nuova e il difetto non fa danno:
    e' vero finche' resta vero.

    Quello che invece non si tocca e' la consegna gia' rinominata
    (`Ordine BETULLINO — 14 agosto 2026.xlsx`): quella e' la compilazione
    precedente, non uno scarto.
    """

    def setUp(self) -> None:
        self.node = node_disponibile()
        if self.node is None:
            self.skipTest("Node non disponibile per il writer XLSX")
        temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(temporanea.cleanup)
        self.cartella = Path(temporanea.name)
        self.uscita = self.cartella / "uscita"
        self.uscita.mkdir()
        self.sorgente = listino_con_ean(self.cartella / "listino_nuovo.xlsx")
        self.impronta = hashlib.sha256(self.sorgente.read_bytes()).hexdigest()

    def test_la_copia_che_si_chiamava_in_un_altro_modo_non_resta(self) -> None:
        vecchia = self.uscita / "ORDINE_COME_CI_CHIAMAVAMO_IERI_listino_nuovo.xlsx"
        vecchia.write_bytes(b"l'ordine della settimana scorsa")
        consegnata = self.uscita / "Ordine BETULLINO — 13 agosto 2026.xlsx"
        consegnata.write_bytes(b"la consegna della volta prima, gia' rinominata")
        altro_listino = self.uscita / "ORDINE_BETULLINO_listino_di_un_altro.xlsx"
        altro_listino.write_bytes(b"un altro listino, non e' affar suo")
        piano = {"orders": [{
            "supplier": "nuovo_fornitore",
            "supplier_source_row": 3,
            "supplier_ean": "8000000000003",
            "supplier_description": "PRODOTTO TRE",
            "quantity": 5,
        }]}
        config = {
            "supplier_files": {"nuovo_fornitore": str(self.sorgente)},
            "supplier_write_rules": {"nuovo_fornitore": {
                "sheet": "Listino",
                "order_column": "C",
                "data_start_row": 2,
                "header_row": 1,
                "expected_header": "ORDINE",
                "source_sha256": self.impronta,
                "display_name": "BETULLINO",
            }},
        }

        esito = esegui_writer(self.node, self.cartella, piano, config, self.uscita)

        self.assertEqual(esito.returncode, 0, esito.stderr or esito.stdout)
        self.assertEqual(
            sorted(percorso.name for percorso in self.uscita.glob("*.xlsx")),
            [
                "ORDINE_BETULLINO_listino_di_un_altro.xlsx",
                "ORDINE_BETULLINO_listino_nuovo.xlsx",
                "Ordine BETULLINO — 13 agosto 2026.xlsx",
            ],
        )

    def test_la_copia_appena_prodotta_non_se_la_porta_via_un_altro_fornitore(self) -> None:
        """Due fornitori sullo stesso listino, uno ordinato e uno no.

        La pulizia del fornitore **non** ordinato lega sul listino di partenza,
        che qui e' lo stesso: senza le copie protette cancellerebbe l'ordine
        appena scritto, il writer uscirebbe con 0 dichiarando un file che non
        c'e', e a fermarsi sarebbe il servizio con una frase che manda a cercare
        il guasto nel posto sbagliato.
        """

        piano = {"orders": [{
            "supplier": "nuovo_fornitore",
            "supplier_source_row": 3,
            "supplier_ean": "8000000000003",
            "supplier_description": "PRODOTTO TRE",
            "quantity": 5,
        }]}
        config = {
            "supplier_files": {
                "nuovo_fornitore": str(self.sorgente),
                # Configurato, non ordinato, e con lo **stesso** listino.
                "fornitore_fermo": str(self.sorgente),
            },
            "supplier_write_rules": {"nuovo_fornitore": {
                "sheet": "Listino",
                "order_column": "C",
                "data_start_row": 2,
                "header_row": 1,
                "expected_header": "ORDINE",
                "source_sha256": self.impronta,
                "display_name": "BETULLINO",
            }},
        }

        esito = esegui_writer(self.node, self.cartella, piano, config, self.uscita)

        self.assertEqual(esito.returncode, 0, esito.stderr or esito.stdout)
        copia = self.uscita / "ORDINE_BETULLINO_listino_nuovo.xlsx"
        self.assertTrue(copia.is_file(), sorted(p.name for p in self.uscita.iterdir()))
        libro = load_workbook(copia, read_only=True)
        try:
            self.assertEqual(libro["Listino"]["C3"].value, 5)
        finally:
            libro.close()

    def test_chi_si_scrive_in_posizione_lo_dice_la_regola_non_il_nome(self) -> None:
        """⚠ Fino al 18 agosto 2026 questo writer saltava chi si chiamava
        «noce», in tre punti. Il nome però non c'entra: quello che decide è
        la procedura di scrittura che la configurazione dichiara — la stessa che
        `app/server.py` legge con `procedura_di_scrittura`.

        La prova rovescia i due nomi apposta: **«noce» senza procedura si
        compila**, un fornitore che si chiama in un altro modo e la dichiara
        **si salta**. Col nome cablato questa prova è impossibile da passare.
        """

        gemello = self.cartella / "listino_del_terzo.xlsx"
        shutil.copy2(self.sorgente, gemello)
        piano = {"orders": [
            {"supplier": "noce", "supplier_source_row": 3,
             "supplier_ean": "8000000000003", "supplier_description": "PRODOTTO TRE", "quantity": 5},
            {"supplier": "terzo", "supplier_source_row": 3,
             "supplier_ean": "8000000000003", "supplier_description": "PRODOTTO TRE", "quantity": 9},
        ]}
        config = {
            "supplier_files": {"noce": str(self.sorgente), "terzo": str(gemello)},
            "supplier_write_rules": {
                "noce": {
                    "sheet": "Listino",
                    "order_column": "C",
                    "data_start_row": 2,
                    "header_row": 1,
                    "expected_header": "ORDINE",
                    "source_sha256": self.impronta,
                    "display_name": "NOCE",
                },
                "terzo": {
                    "sheet": "Listino",
                    "order_column": "C",
                    "data_start_row": 2,
                    "header_row": 1,
                    "display_name": "TERZO",
                    "compilazione": "patch_xls_in_posizione",
                },
            },
        }

        esito = esegui_writer(self.node, self.cartella, piano, config, self.uscita)

        self.assertEqual(esito.returncode, 0, esito.stderr or esito.stdout)
        prodotte = sorted(percorso.name for percorso in self.uscita.iterdir())
        self.assertIn("ORDINE_NOCE_listino_nuovo.xlsx", prodotte)
        self.assertNotIn("ORDINE_TERZO_listino_del_terzo.xlsx", prodotte)
        riepilogo = json.loads(
            next(riga for riga in esito.stdout.splitlines() if riga.startswith("RIEPILOGO_COMPILAZIONE:"))
            .removeprefix("RIEPILOGO_COMPILAZIONE:")
        )
        # Le righe di chi si scrive altrove si contano lo stesso, per dirlo.
        self.assertEqual(riepilogo["noce_lines"], 1)
        self.assertEqual(
            [voce["supplier"] for voce in riepilogo["supplier_copies"]], ["noce"],
        )

    def test_la_pulizia_non_cancella_per_omonimia(self) -> None:
        """`listino_nuovo` è la coda di `mio_listino_nuovo`.

        ⚠ Rilievo della revisione avversariale del 14 agosto 2026: la pulizia
        riconosceva le copie vecchie dalla coda del nome, e la coda di un
        listino può essere la coda del listino di un altro fornitore. Il primo
        si portava via l'ordine del secondo. Vince la coda più lunga.
        """

        gemello = self.cartella / "mio_listino_nuovo.xlsx"
        shutil.copy2(self.sorgente, gemello)
        ordine_dell_altro = self.uscita / "ORDINE_NOCE_mio_listino_nuovo.xlsx"
        ordine_dell_altro.write_bytes(b"l'ordine dell'altro fornitore, da non toccare")
        piano = {"orders": [{
            "supplier": "nuovo_fornitore",
            "supplier_source_row": 3,
            "supplier_ean": "8000000000003",
            "supplier_description": "PRODOTTO TRE",
            "quantity": 5,
        }]}
        config = {
            "supplier_files": {
                "nuovo_fornitore": str(self.sorgente),
                # Noce non passa da questo writer — il suo documento lo
                # compila il servizio in posizione — quindi la sua copia qui non
                # la tocca nessun altro: e' il caso in cui l'omonimia si vede.
                "noce": str(gemello),
            },
            "supplier_write_rules": {
                "nuovo_fornitore": {
                    "sheet": "Listino",
                    "order_column": "C",
                    "data_start_row": 2,
                    "header_row": 1,
                    "expected_header": "ORDINE",
                    "source_sha256": self.impronta,
                    "display_name": "BETULLINO",
                },
                # ⚠ La regola di Noce c'e', e dichiara la sua procedura:
                # e' quello che il servizio locale scrive davvero in
                # `writer_config.json`. Senza, il banco descriveva una
                # configurazione che non esiste — ed era il banco a tenere in
                # piedi il `supplier === "noce"` cablato in questo writer.
                "noce": {
                    "sheet": "Foglio1",
                    "order_column": "I",
                    "data_start_row": 6,
                    "header_row": 5,
                    "compilazione": "patch_xls_in_posizione",
                },
            },
        }

        esito = esegui_writer(self.node, self.cartella, piano, config, self.uscita)

        self.assertEqual(esito.returncode, 0, esito.stderr or esito.stdout)
        self.assertTrue(ordine_dell_altro.is_file(),
                        sorted(percorso.name for percorso in self.uscita.iterdir()))
        self.assertTrue((self.uscita / "ORDINE_BETULLINO_listino_nuovo.xlsx").is_file())

    def test_due_fornitori_sullo_stesso_documento_fermano_la_compilazione(self) -> None:
        """Due nomi diversi che producono lo stesso file d'ordine.

        `nomePerIlFile` schiaccia in `_` tutto cio' che non e' una lettera o una
        cifra: «Sapori & Co.» e «Sapori Co» danno lo stesso nome. Consegnarne
        uno solo vorrebbe dire mandare a un fornitore la merce di un altro.
        """

        secondo = listino_con_ean(self.cartella / "altro_listino.xlsx")
        piano = {"orders": [
            {"supplier": "primo", "supplier_source_row": 3, "supplier_ean": "8000000000003",
             "supplier_description": "PRODOTTO TRE", "quantity": 5},
            {"supplier": "secondo", "supplier_source_row": 3, "supplier_ean": "8000000000003",
             "supplier_description": "PRODOTTO TRE", "quantity": 2},
        ]}
        regola = {
            "sheet": "Listino", "order_column": "C", "data_start_row": 2,
            "header_row": 1, "expected_header": "ORDINE",
        }
        # Due listini con lo stesso nome di file in due cartelle diverse, e due
        # nomi di fornitore che si riducono allo stesso: il documento d'ordine
        # sarebbe lo stesso per tutti e due.
        gemello = self.cartella / "gemello"
        gemello.mkdir()
        copia_listino = gemello / self.sorgente.name
        shutil.copy2(secondo, copia_listino)
        config = {
            "supplier_files": {"primo": str(self.sorgente), "secondo": str(copia_listino)},
            "supplier_write_rules": {
                "primo": {**regola, "display_name": "Sapori & Co.",
                          "source_sha256": self.impronta},
                "secondo": {**regola, "display_name": "Sapori Co",
                            "source_sha256": hashlib.sha256(copia_listino.read_bytes()).hexdigest()},
            },
        }

        esito = esegui_writer(self.node, self.cartella, piano, config, self.uscita)

        self.assertNotEqual(esito.returncode, 0)
        marcate = [riga.strip() for riga in esito.stderr.splitlines()
                   if riga.strip().startswith("ERRORE_COMPILAZIONE:")]
        self.assertEqual(len(marcate), 1, esito.stderr)
        self.assertIn("produrrebbero lo stesso documento d'ordine", marcate[0])
        self.assertFalse(list(self.uscita.glob("*.xlsx")) if self.uscita.is_dir() else [])


class QuandoLaRigaNonPortaPiuIlProdottoDelPiano(unittest.TestCase):
    """L'EAN che non coincide: per quel fornitore non esce niente.

    E il fratello di controllo: senza il blocco `verify` la stessa riga sbagliata
    si scrive come si e' sempre scritto.  Una configurazione vecchia non deve
    smettere di funzionare perche' il writer ha imparato un controllo nuovo.
    """

    def setUp(self) -> None:
        self.node = node_disponibile()
        if self.node is None:
            self.skipTest("Node non disponibile per il writer XLSX")
        temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(temporanea.cleanup)
        self.cartella = Path(temporanea.name)
        self.uscita = self.cartella / "uscita"
        self.sorgente = listino_con_ean(self.cartella / "listino_nuovo.xlsx")
        self.impronta = hashlib.sha256(self.sorgente.read_bytes()).hexdigest()
        # La riga 3 del listino porta 8000000000003: il piano ne chiede un'altra.
        self.piano = {"orders": [{
            "supplier": "nuovo_fornitore",
            "supplier_source_row": 3,
            "supplier_ean": "8000000000099",
            "supplier_description": "PRODOTTO TRE",
            "quantity": 5,
        }]}

    def regola(self, **extra: Any) -> dict[str, Any]:
        return {
            "supplier_files": {"nuovo_fornitore": str(self.sorgente)},
            "supplier_write_rules": {"nuovo_fornitore": {
                "sheet": "Listino",
                "order_column": "C",
                "data_start_row": 2,
                "header_row": 1,
                "expected_header": "ORDINE",
                "source_sha256": self.impronta,
                "display_name": "BETULLINO",
                **extra,
            }},
        }

    def test_l_ean_che_non_coincide_non_crea_nessuna_copia(self) -> None:
        esito = esegui_writer(
            self.node, self.cartella, self.piano,
            self.regola(verify={"ean_column": "A", "description_column": "B"}),
            self.uscita,
        )

        self.assertNotEqual(esito.returncode, 0)
        righe = [riga.strip() for riga in esito.stderr.splitlines() if riga.strip()]
        marcate = [riga for riga in righe if riga.startswith("ERRORE_COMPILAZIONE:")]
        self.assertEqual(len(marcate), 1, esito.stderr)
        self.assertEqual(
            marcate[0],
            "ERRORE_COMPILAZIONE: La riga 3 del listino BETULLINO porta l'EAN 8000000000003 "
            "e il piano si aspetta 8000000000099: non è più la riga su cui è stato costruito "
            "l'ordine. L'ordine BETULLINO non viene creato.",
        )
        # ⚠ Niente copia consegnabile: e' tutto il punto del controllo.
        self.assertFalse(
            list(self.uscita.glob("*.xlsx")) if self.uscita.is_dir() else [],
            "una riga sbagliata non deve lasciare sul disco un ordine da spedire",
        )
        # E niente traccia di Node in faccia all'utente.
        self.assertNotIn("    at ", esito.stderr)

    def piano_di_riga_3(self, descrizione: str = "PRODOTTO TRE") -> dict[str, Any]:
        """Un ordine sulla riga 3, con l'EAN che quella riga porta davvero."""

        return {"orders": [{
            "supplier": "nuovo_fornitore",
            "supplier_source_row": 3,
            "supplier_ean": "8000000000003",
            "supplier_description": descrizione,
            "quantity": 5,
        }]}

    def frase_marcata(self, esito: Any) -> str:
        marcate = [riga.strip() for riga in esito.stderr.splitlines()
                   if riga.strip().startswith("ERRORE_COMPILAZIONE:")]
        self.assertEqual(len(marcate), 1, esito.stderr)
        self.assertFalse(list(self.uscita.glob("*.xlsx")) if self.uscita.is_dir() else [])
        return marcate[0]

    def test_con_la_descrizione_che_conferma_la_riga_si_accusa_la_configurazione(self) -> None:
        """Il listino e' a posto: a essere sbagliata e' la colonna dichiarata.

        Con `ean_column` puntata sulla descrizione **ogni** riga risulta
        cambiata, e il writer diceva «non è più la riga su cui è stato costruito
        l'ordine»: chi legge va a cercare un guasto nel listino, che non ce
        l'ha.  Qui la descrizione coincide, quindi la riga e' quella giusta e lo
        si puo' dire per certo.
        """

        esito = esegui_writer(
            self.node, self.cartella, self.piano_di_riga_3(),
            self.regola(verify={"ean_column": "B", "description_column": "B"}),
            self.uscita,
        )

        self.assertNotEqual(esito.returncode, 0)
        self.assertEqual(
            self.frase_marcata(esito),
            "ERRORE_COMPILAZIONE: Nel listino BETULLINO la riga 3 è quella giusta — la "
            "descrizione coincide — ma l'EAN 8000000000003 del piano sta nella colonna A e non "
            "in B, che è la colonna dichiarata per il controllo: da correggere è la "
            "configurazione di BETULLINO, non il listino. L'ordine BETULLINO non viene creato.",
        )

    def test_senza_descrizione_si_dicono_tutte_e_due_le_possibilita(self) -> None:
        """⚠ Il rilievo della revisione avversariale del 14 agosto 2026.

        Trovare l'EAN del piano in un'altra colonna **non basta** a dire che la
        colonna dichiarata sia sbagliata: un fornitore che ripete il codice
        nella colonna dell'articolo lo mette anche su una riga che quel prodotto
        non ce l'ha più. Chi credesse alla frase sbagliata sposterebbe
        `ean_column` sul codice articolo, spegnendo per sempre questa difesa.
        Senza una descrizione da confrontare si dicono tutte e due le cose.
        """

        esito = esegui_writer(
            self.node, self.cartella, self.piano_di_riga_3(),
            self.regola(verify={"ean_column": "B"}),
            self.uscita,
        )

        self.assertNotEqual(esito.returncode, 0)
        frase = self.frase_marcata(esito)
        self.assertIn("o la colonna dichiarata è sbagliata, o quella riga non è più quella dell'ordine", frase)
        self.assertIn("colonna A della stessa riga", frase)
        self.assertNotIn("da correggere è la configurazione", frase)

    def test_se_la_descrizione_dice_un_altra_merce_la_riga_resta_l_imputata(self) -> None:
        """La riga porta l'EAN del piano in un'altra colonna, ma è un'altra merce.

        È il caso del 12 agosto 2026 — la riga 2600 che era olio Carapelli — con
        in più un fornitore che ripete il codice: la colonna dichiarata è giusta
        e a non tornare è la riga. La frase non deve mandare a toccare la
        configurazione.
        """

        esito = esegui_writer(
            self.node, self.cartella, self.piano_di_riga_3("OLIO EXTRAVERGINE CARAPELLI"),
            self.regola(verify={"ean_column": "B", "description_column": "B"}),
            self.uscita,
        )

        self.assertNotEqual(esito.returncode, 0)
        frase = self.frase_marcata(esito)
        self.assertIn("non è più la riga su cui è stato costruito l'ordine", frase)
        self.assertNotIn("configurazione", frase)

    def test_senza_verify_si_scrive_come_si_e_sempre_scritto(self) -> None:
        """Il fratello di controllo: stessa riga sbagliata, nessuna verifica.

        Serve a due cose: dice che la configurazione vecchia non si rompe, e
        dice che il collaudo qui sopra misura il **controllo** e non un guasto
        qualunque del listino di prova.
        """

        esito = esegui_writer(self.node, self.cartella, self.piano, self.regola(), self.uscita)

        self.assertEqual(esito.returncode, 0, esito.stderr or esito.stdout)
        copia = self.uscita / "ORDINE_BETULLINO_listino_nuovo.xlsx"
        self.assertTrue(copia.is_file(), sorted(p.name for p in self.uscita.iterdir()))
        libro = load_workbook(copia, read_only=True)
        try:
            self.assertEqual(libro["Listino"]["C3"].value, 5)
        finally:
            libro.close()
        riepilogo = riepilogo_del_writer(esito.stdout)["supplier_copies"][0]
        # Niente da confrontare: la riga si conta fra quelle non verificabili.
        self.assertEqual(riepilogo["verified_rows"], 0)
        self.assertEqual(riepilogo["unverifiable_rows"], 1)
        self.assertEqual(riepilogo["warnings"], [])


class IlNomeLeggibileNelleFrasi(unittest.TestCase):
    """Il difetto 2 dove lo legge l'utente: nelle frasi, non solo nei file.

    Questi due collaudi si fermano sulla configurazione, prima che la libreria
    dei fogli di calcolo si carichi: costano meno di un secondo l'uno.
    """

    def setUp(self) -> None:
        self.node = node_disponibile()
        if self.node is None:
            self.skipTest("Node non disponibile per il writer XLSX")
        temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(temporanea.cleanup)
        self.cartella = Path(temporanea.name)
        self.sorgente = listino_con_ean(self.cartella / "listino_nuovo.xlsx")
        self.piano = {"orders": [{
            "supplier": "nuovo_fornitore", "supplier_source_row": 3, "quantity": 1,
        }]}

    def _marcata(self, regola: dict[str, Any]) -> str:
        esito = esegui_writer(
            self.node, self.cartella, self.piano,
            {
                "supplier_files": {"nuovo_fornitore": str(self.sorgente)},
                "supplier_write_rules": {"nuovo_fornitore": regola},
            },
            self.cartella / "uscita",
        )
        self.assertNotEqual(esito.returncode, 0)
        marcate = [
            riga.strip() for riga in esito.stderr.splitlines()
            if riga.strip().startswith("ERRORE_COMPILAZIONE:")
        ]
        self.assertEqual(len(marcate), 1, esito.stderr)
        return marcate[0]

    def test_il_display_name_sostituisce_l_identificativo_tecnico(self) -> None:
        """⚠ Senza, l'utente si legge `NUOVO_FORNITORE`, underscore compreso."""

        # La regola non dichiara il foglio: ci si ferma subito, e la frase deve
        # gia' chiamare il fornitore con il suo nome.
        marcata = self._marcata({"order_column": "C", "display_name": "D'Alessio & Figli S.r.l."})

        self.assertIn("Manca il foglio verificato per D'Alessio & Figli S.r.l.", marcata)
        self.assertNotIn("NUOVO_FORNITORE", marcata)

    def test_senza_display_name_resta_il_ripiego_di_oggi(self) -> None:
        """La configurazione che il nome non lo dichiara non cambia comportamento."""

        marcata = self._marcata({"order_column": "C"})

        self.assertIn("Manca il foglio verificato per NUOVO_FORNITORE", marcata)


# ---------------------------------------------------------------------------
# 2. La guardia cella per cella, da sola
# ---------------------------------------------------------------------------


class LaGuardiaCellaPerCella(unittest.TestCase):
    def setUp(self) -> None:
        temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(temporanea.cleanup)
        self.cartella = Path(temporanea.name)
        self.originale = listino_semplice(
            self.cartella / "originale.xlsx", "Listino",
            [
                ["EAN", "DESCRIZIONE", "ORDINE"],
                ["8000000000001", "PRODOTTO UNO", 4],
                ["8000000000002", "PRODOTTO DUE", None],
                [None, None, "SEZIONE SOLARI"],
            ],
        )

    def copia_con(self, cambiamenti: dict[str, Any]) -> Path:
        copia = self.cartella / "copia.xlsx"
        shutil.copy2(self.originale, copia)
        libro = load_workbook(copia)
        try:
            for cella, valore in cambiamenti.items():
                libro["Listino"][cella] = valore
            libro.save(copia)
        finally:
            libro.close()
        return copia

    def confronta(self, copia: Path, quantita: dict[int, int] | None = None) -> Any:
        return confronta_copia(
            self.originale, copia,
            colonna_ordine="C", prima_riga=2, quantita=quantita or {}, foglio_ordine="Listino",
        )

    def test_la_copia_identica_e_fedele(self) -> None:
        esito = self.confronta(self.copia_con({}))
        self.assertTrue(esito.fedele)
        self.assertEqual(esito.differenze_ammesse, 0)
        # Il listino di prova porta 9 celle con qualcosa dentro: il confronto
        # le deve aver viste tutte (le celle vuote senza formato non contano
        # niente e non si contano).
        self.assertGreaterEqual(esito.celle_confrontate, 9)

    def test_una_differenza_fuori_dalla_colonna_d_ordine_viene_rifiutata(self) -> None:
        """Il caso B1: un EAN che nella copia non è più quello del listino."""

        esito = self.confronta(self.copia_con({"A2": "1235"}), quantita={2: 9})
        self.assertFalse(esito.fedele)
        self.assertEqual(esito.quante_rifiutate, 1)
        frase = frase_di_rifiuto("LARICE", esito)
        self.assertIn("non è fedele al listino di partenza", frase)
        self.assertIn("A2", frase)
        self.assertIn("8000000000001", frase)
        self.assertIn("1235", frase)
        self.assertIn("La copia non viene consegnata", frase)

    def test_la_quantita_del_piano_e_una_differenza_ammessa(self) -> None:
        esito = self.confronta(self.copia_con({"C3": 6}), quantita={3: 6})
        self.assertTrue(esito.fedele, esito.esempi_rifiutati)
        self.assertEqual(esito.differenze_ammesse, 1)

    def test_una_quantita_diversa_da_quella_del_piano_viene_rifiutata(self) -> None:
        """La cella è quella giusta, il numero no: è comunque un ordine sbagliato."""

        esito = self.confronta(self.copia_con({"C3": 7}), quantita={3: 6})
        self.assertFalse(esito.fedele)
        self.assertEqual(esito.quante_rifiutate, 1)

    def test_l_azzeramento_di_una_quantita_preesistente_e_ammesso(self) -> None:
        esito = self.confronta(self.copia_con({"C2": None}))
        self.assertTrue(esito.fedele, esito.esempi_rifiutati)
        self.assertEqual(esito.differenze_ammesse, 1)

    def test_un_titolo_di_sezione_cancellato_viene_rifiutato(self) -> None:
        """Il caso M1: la colonna d'ordine non è fatta solo di quantità."""

        esito = self.confronta(self.copia_con({"C4": None}))
        self.assertFalse(esito.fedele)
        self.assertEqual(esito.quante_rifiutate, 1)
        self.assertIn("SEZIONE SOLARI", frase_di_rifiuto("LARICE", esito))

    def test_una_cella_svuotata_fuori_dalla_colonna_d_ordine_viene_rifiutata(self) -> None:
        """⚠ L'azzeramento e' lecito **solo** dentro la colonna d'ordine.

        Un EAN e' fatto di cifre: se «era un numero, adesso e' vuota» valesse per
        tutte le colonne, un EAN sparito passerebbe per un azzeramento
        legittimo.  E' la differenza fra una guardia e un colabrodo.
        """

        esito = self.confronta(self.copia_con({"A2": None}))
        self.assertFalse(esito.fedele)
        self.assertIn("A2", frase_di_rifiuto("LARICE", esito))

    def test_l_intestazione_della_colonna_d_ordine_non_si_puo_toccare(self) -> None:
        """Sopra la prima riga di dati nessuna differenza è ammessa."""

        esito = self.confronta(self.copia_con({"C1": None}))
        self.assertFalse(esito.fedele)

    def test_sopra_la_prima_riga_di_dati_nemmeno_un_azzeramento_e_lecito(self) -> None:
        """La riga 2 qui e' intestazione, non dati: quello che c'e' scritto resta.

        `C2` porta il numero 4.  Dentro l'area dei dati svuotarlo sarebbe
        l'azzeramento di una quantita' preesistente; sopra la prima riga di dati
        e' una cella del listino che sparisce.
        """

        esito = confronta_copia(
            self.originale, self.copia_con({"C2": None}),
            colonna_ordine="C", prima_riga=3, quantita={}, foglio_ordine="Listino",
        )
        self.assertFalse(esito.fedele)

    def test_la_cella_vuota_e_la_stringa_vuota_sono_la_stessa_cosa(self) -> None:
        """In Excel si vedono uguali: chiamarle diverse fermerebbe un ordine giusto.

        ⚠ La stringa condivisa vuota va costruita a mano: openpyxl non la genera
        (scrive una cella senza niente e la rilegge come `None`), e la prima
        versione di questo collaudo — `A4 = ""` su un file di openpyxl —
        confrontava `None` con `None` senza mai toccare la tolleranza che dice
        di difendere.  L'ha scoperto la revisione avversariale del 13 agosto
        2026, spegnendola con la suite verde.  Sul listino LARICE vero questa
        tolleranza vale 35 celle a compilazione: senza, il fornitore piu'
        grosso non riceverebbe piu' nessun ordine.
        """

        originale = listino_con_stringhe_vuote(self.cartella / "con_vuote.xlsx")
        copia = self.cartella / "riscritta.xlsx"
        libro = load_workbook(originale)  # le celle A2/A5 arrivano come «»...
        try:
            libro.save(copia)  # ...e il file riscritto le porta come celle senza niente
        finally:
            libro.close()

        esito = confronta_copia(
            originale, copia,
            colonna_ordine="C", prima_riga=2, quantita={}, foglio_ordine="Listino",
        )
        self.assertTrue(esito.fedele, esito.esempi_rifiutati)

    def copia_con_formato(self, cambiamenti: dict[str, Any], formati: dict[str, str]) -> Path:
        copia = self.cartella / "copia.xlsx"
        shutil.copy2(self.originale, copia)
        libro = load_workbook(copia)
        try:
            for cella, valore in cambiamenti.items():
                libro["Listino"][cella] = valore
            for cella, formato in formati.items():
                libro["Listino"][cella].number_format = formato
            libro.save(copia)
        finally:
            libro.close()
        return copia

    def test_un_formato_che_cambia_il_numero_mostrato_viene_rifiutato(self) -> None:
        """Il fornitore legge il numero **mostrato**, non la memoria del file.

        `C2` resta 4 in memoria ma con formato `0` un prezzo come 1,75 si
        leggerebbe «2».  La revisione avversariale del 13 agosto 2026 ha
        costruito la manomissione e la prima guardia la prometteva al
        fornitore senza dire niente.
        """

        esito = self.confronta(self.copia_con_formato({}, {"C2": "0.00"}))
        self.assertFalse(esito.fedele)
        frase = frase_di_rifiuto("LARICE", esito)
        self.assertIn("formato numerico", frase)
        self.assertIn("C2", frase)

    def test_il_formato_che_nasconde_la_quantita_scritta_viene_rifiutato(self) -> None:
        """`;;;` sulla quantita' del piano: il programma crede di aver chiesto
        6 colli e il fornitore vede una cella vuota."""

        esito = self.confronta(
            self.copia_con_formato({"C3": 6}, {"C3": ";;;"}), quantita={3: 6}
        )
        self.assertFalse(esito.fedele)

    def test_un_formato_diverso_su_una_cella_vuota_non_ferma_niente(self) -> None:
        """Su una cella vuota nessun formato mostra nulla: fermarsi li'
        rifiuterebbe un ordine giusto per una veste che non si vede."""

        esito = self.confronta(self.copia_con_formato({}, {"A4": ";;;"}))
        self.assertTrue(esito.fedele, esito.esempi_rifiutati)

    def test_un_foglio_che_sparisce_viene_rifiutato(self) -> None:
        copia = self.cartella / "copia.xlsx"
        listino_semplice(copia, "Un altro nome", [["EAN", "DESCRIZIONE", "ORDINE"]])
        esito = self.confronta(copia)
        self.assertFalse(esito.fedele)
        self.assertIn("i fogli della copia non sono quelli", esito.esempi_rifiutati[0])

    def test_si_nominano_i_primi_casi_e_si_contano_tutti(self) -> None:
        esito = self.confronta(self.copia_con({"A2": "X", "A3": "Y", "B2": "Z", "B3": "W"}))
        self.assertEqual(esito.quante_rifiutate, 4)
        self.assertEqual(len(esito.esempi_rifiutati), 3)
        self.assertIn("4 celle sono diverse", frase_di_rifiuto("LARICE", esito))

    def test_senza_la_colonna_d_ordine_il_confronto_si_ferma(self) -> None:
        """Senza sapere dov'è l'ordine non si distingue il lecito dal guasto."""

        with self.assertRaises(ConfrontoImpossibile):
            confronta_copia(
                self.originale, self.copia_con({}),
                colonna_ordine="", prima_riga=2, quantita={},
            )

    def test_un_documento_che_non_si_apre_lo_dice(self) -> None:
        rotto = self.cartella / "rotto.xlsx"
        rotto.write_bytes(b"non sono un foglio di calcolo")
        with self.assertRaises(ConfrontoImpossibile) as errore:
            self.confronta(rotto)
        self.assertIn("non riesco a riaprire la copia", str(errore.exception))


class IlPianoEIlContratto(unittest.TestCase):
    """Non «non c'e' niente di troppo», ma «c'e' tutto quello che serve».

    ⚠ Il difetto misurato il 14 agosto 2026.  La guardia entrava in azione solo
    dove la copia era **diversa** dal listino: una quantita' che il writer non
    scrive lascia la cella identica a com'era, nessun ramo la incontrava, e la
    copia usciva «fedele» con una riga d'ordine in meno.  Misurato allora:

        copia completa            fedele=True  ammesse=3  rifiutate=0
        copia senza la riga 6     fedele=True  ammesse=2  rifiutate=0

    Il dato per accorgersene c'era gia': `quantita` **e'** l'elenco delle righe
    che dovevano essere scritte.
    """

    def setUp(self) -> None:
        temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(temporanea.cleanup)
        self.cartella = Path(temporanea.name)
        self.originale = listino_semplice(
            self.cartella / "originale.xlsx", "Listino",
            [
                ["EAN", "DESCRIZIONE", "ORDINE"],
                # C2 porta la quantita' ordinata la settimana scorsa.
                ["8000000000001", "PRODOTTO UNO", 4],
                ["8000000000002", "PRODOTTO DUE", None],
                ["8000000000003", "PRODOTTO TRE", None],
                [None, None, "SEZIONE SOLARI"],
                ["8000000000006", "PRODOTTO SEI", None],
            ],
        )

    def copia_con(self, cambiamenti: dict[str, Any]) -> Path:
        copia = self.cartella / "copia.xlsx"
        shutil.copy2(self.originale, copia)
        libro = load_workbook(copia)
        try:
            for cella, valore in cambiamenti.items():
                libro["Listino"][cella] = valore
            libro.save(copia)
        finally:
            libro.close()
        return copia

    def confronta(self, cambiamenti: dict[str, Any], quantita: dict[int, int]) -> Any:
        return confronta_copia(
            self.originale, self.copia_con(cambiamenti),
            colonna_ordine="C", prima_riga=2, quantita=quantita, foglio_ordine="Listino",
        )

    # -- il difetto ---------------------------------------------------------

    def test_una_riga_del_piano_che_non_arriva_nella_copia_non_e_fedele(self) -> None:
        """Il piano chiede tre righe, la copia ne porta due: non si consegna.

        Nella copia non c'e' **niente** di troppo — nessuna cella cambiata
        fuori posto — ed e' esattamente per questo che il difetto e' vissuto
        tanto: la guardia contava solo quello.
        """

        esito = self.confronta(
            {"C2": None, "C3": 5, "C4": 6},  # la riga 6 il writer se l'e' persa
            quantita={3: 5, 4: 6, 6: 7},
        )

        self.assertFalse(esito.fedele)
        self.assertEqual(esito.righe_mancanti, [6])
        self.assertEqual(esito.righe_richieste, 3)
        # Nessuna cella di troppo: il guasto e' tutto nell'altra misura.
        self.assertEqual(esito.quante_rifiutate, 0)
        frase = frase_di_rifiuto("LARICE", esito)
        self.assertIn("riga 6", frase)
        self.assertIn("delle 3 righe d'ordine richieste", frase)
        self.assertIn("La copia non viene consegnata", frase)
        # E non si accusa il listino di avere celle cambiate: manderebbe a
        # cercare il guasto dalla parte opposta.
        self.assertNotIn("celle sono diverse", frase)
        self.assertNotIn("cella è diversa", frase)

    # -- che non si rompa quello che funzionava -----------------------------

    def test_la_copia_completa_resta_fedele(self) -> None:
        """Il fratello di controllo: le stesse tre righe, tutte scritte."""

        esito = self.confronta(
            {"C2": None, "C3": 5, "C4": 6, "C6": 7},
            quantita={3: 5, 4: 6, 6: 7},
        )

        self.assertTrue(esito.fedele, esito.esempi_rifiutati)
        self.assertEqual(esito.righe_mancanti, [])
        self.assertEqual(esito.righe_richieste, 3)
        # Le tre quantita' del piano piu' l'azzeramento della settimana scorsa.
        self.assertEqual(esito.differenze_ammesse, 4)

    def test_l_azzeramento_della_settimana_prima_non_e_una_riga_richiesta(self) -> None:
        """`C2` si svuota perche' il piano **non** chiede niente per quella riga.

        E' la ragione per cui le righe attese si contano su `quantita` e non
        sulle differenze ammesse: un azzeramento e' una differenza ammessa che
        nel piano non c'e', e pretendere «tante differenze quante righe» lo
        conterebbe due volte o lo scambierebbe per una riga scritta.
        """

        esito = self.confronta({"C2": None, "C3": 5}, quantita={3: 5})

        self.assertTrue(esito.fedele, esito.esempi_rifiutati)
        self.assertEqual(esito.righe_richieste, 1)
        self.assertEqual(esito.righe_mancanti, [])
        self.assertEqual(esito.differenze_ammesse, 2)

    def test_una_quantita_gia_giusta_nel_listino_non_e_una_riga_mancante(self) -> None:
        """⚠ Il piano chiede 4 alla riga 2, e nel listino c'e' gia' scritto 4.

        La copia e' identica all'originale e **giusta**: il fornitore legge i
        4 colli che il piano ha chiesto.  Se la regola fosse «ogni riga del
        piano deve aver prodotto una differenza», questa copia perfetta
        verrebbe buttata via — e chi riordina ogni settimana le stesse
        quantita' non riceverebbe mai un ordine.  Si guarda il valore che la
        copia porta, non quanto e' cambiata.
        """

        esito = self.confronta({}, quantita={2: 4})

        self.assertTrue(esito.fedele, esito.esempi_rifiutati)
        self.assertEqual(esito.differenze_ammesse, 0)
        self.assertEqual(esito.righe_mancanti, [])

    # -- che cosa legge l'utente --------------------------------------------

    def test_i_due_guasti_si_dicono_diversi(self) -> None:
        """Una cella di troppo e una riga che manca: due frasi, non una."""

        esito = self.confronta(
            {"A3": "1235", "C3": 5},  # EAN rovinato, e la riga 4 non scritta
            quantita={3: 5, 4: 6},
        )

        self.assertFalse(esito.fedele)
        self.assertEqual(esito.quante_rifiutate, 1)
        self.assertEqual(esito.righe_mancanti, [4])
        frase = frase_di_rifiuto("LARICE", esito)
        self.assertIn("1 cella è diversa fuori dalle celle dell'ordine", frase)
        self.assertIn("A3", frase)
        self.assertIn("Inoltre alla copia manca 1 delle 2 righe d'ordine richieste", frase)
        self.assertIn("riga 4", frase)

    def test_si_nominano_le_prime_righe_mancanti_e_si_contano_tutte(self) -> None:
        """Un elenco lungo una pagina non lo legge nessuno: primi casi e totale."""

        esito = self.confronta({}, quantita={2: 9, 3: 5, 4: 6, 6: 7})

        self.assertEqual(esito.righe_mancanti, [2, 3, 4, 6])
        frase = frase_di_rifiuto("LARICE", esito)
        self.assertIn("mancano 4 delle 4 righe d'ordine richieste", frase)
        self.assertIn("righe 2, 3 e 4, e un'altra", frase)

    def test_la_frase_delle_celle_di_troppo_e_quella_di_sempre(self) -> None:
        """Requisito esplicito: sulle copie con celle cambiate non cambia nulla.

        La frase si confronta **per intero**, non a pezzi: e' l'unico modo di
        accorgersi che una parola in piu' si e' infilata dentro un messaggio
        che l'utente conosce gia'.
        """

        esito = self.confronta({"B3": "ALTRO PRODOTTO"}, quantita={})

        self.assertEqual(
            frase_di_rifiuto("LARICE", esito),
            "La copia per LARICE non è fedele al listino di partenza: 1 cella è diversa "
            "fuori dalle celle dell'ordine (B3 nel listino di partenza è «PRODOTTO DUE» "
            "e nella copia è «ALTRO PRODOTTO»). La copia non viene consegnata; "
            "il listino di partenza è rimasto invariato.",
        )


class LaGuardiaSuDueFogli(unittest.TestCase):
    """Le promesse sui fogli secondari, provate su un libro che ne ha due.

    ⚠ La revisione avversariale del 13 agosto 2026 ha spento «la guardia guarda
    tutti i fogli» e «la tolleranza vale solo sul foglio dell'ordine» con la
    suite verde: ogni fixture della guardia aveva un foglio solo, quindi il
    ramo dei fogli secondari non lo attraversava nessuno.  Questi collaudi
    esistono per quello, e per il fornitore che riceverebbe condizioni di
    pagamento sbagliate da un'«ottimizzazione» che salta i fogli senza ordine.
    """

    def setUp(self) -> None:
        temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(temporanea.cleanup)
        self.cartella = Path(temporanea.name)
        self.originale = self.cartella / "originale.xlsx"
        workbook = Workbook()
        listino = workbook.active
        listino.title = "Listino"
        for riga in (
            ["EAN", "DESCRIZIONE", "ORDINE"],
            ["8000000000001", "PRODOTTO UNO", 4],
            ["8000000000002", "PRODOTTO DUE", None],
        ):
            listino.append(riga)
        condizioni = workbook.create_sheet("Condizioni")
        for riga in (
            ["PAGAMENTO", "60 GG", None],
            ["CONSEGNA", "FRANCO MAGAZZINO", None],
            # ⚠ C3 porta un numero: svuotarlo somiglia in tutto all'azzeramento
            # di una quantita' preesistente, ma questo non e' il foglio
            # dell'ordine.
            [None, None, 9],
        ):
            condizioni.append(riga)
        workbook.save(self.originale)
        workbook.close()

    def copia_con(self, foglio: str, cambiamenti: dict[str, Any]) -> Path:
        copia = self.cartella / "copia.xlsx"
        shutil.copy2(self.originale, copia)
        libro = load_workbook(copia)
        try:
            for cella, valore in cambiamenti.items():
                libro[foglio][cella] = valore
            libro.save(copia)
        finally:
            libro.close()
        return copia

    def confronta(self, copia: Path, quantita: dict[int, int] | None = None) -> Any:
        return confronta_copia(
            self.originale, copia,
            colonna_ordine="C", prima_riga=2, quantita=quantita or {}, foglio_ordine="Listino",
        )

    def test_la_copia_identica_a_due_fogli_e_fedele(self) -> None:
        esito = self.confronta(self.copia_con("Listino", {}))
        self.assertTrue(esito.fedele, esito.esempi_rifiutati)

    def test_una_differenza_su_un_foglio_secondario_viene_rifiutata(self) -> None:
        """«60 GG» che diventa «30 GG» sono condizioni di pagamento sbagliate."""

        esito = self.confronta(self.copia_con("Condizioni", {"B1": "30 GG"}))
        self.assertFalse(esito.fedele)
        frase = frase_di_rifiuto("LARICE", esito)
        self.assertIn("Condizioni", frase)
        self.assertIn("B1", frase)
        self.assertIn("60 GG", frase)

    def test_la_tolleranza_della_colonna_d_ordine_non_vale_sui_fogli_secondari(self) -> None:
        """La colonna C esiste su tutti i fogli; l'ordine sta solo su «Listino».

        E' la regola che il docstring di `confronta_copia` dichiara: negli
        altri fogli nessuna differenza e' ammessa, nemmeno nella colonna con la
        stessa lettera, nemmeno se somiglia a un azzeramento lecito.
        """

        esito = self.confronta(self.copia_con("Condizioni", {"C3": None}))
        self.assertFalse(esito.fedele)
        self.assertIn("Condizioni", frase_di_rifiuto("LARICE", esito))

    def test_l_azzeramento_resta_lecito_sul_foglio_dell_ordine(self) -> None:
        """Il fratello di controllo: la stessa differenza, sul foglio giusto."""

        esito = self.confronta(self.copia_con("Listino", {"C2": None}))
        self.assertTrue(esito.fedele, esito.esempi_rifiutati)
        self.assertEqual(esito.differenze_ammesse, 1)


class LaGuardiaSulleFormeLecite(unittest.TestCase):
    """Un `.xlsx` lecito ma poco comune non deve far rifiutare una copia perfetta.

    Due forme trovate dalla revisione avversariale del 13 agosto 2026, entrambe
    permesse dall'OOXML: una `<dimension>` piu' stretta dei dati veri (la
    modalita' `read_only` di openpyxl ci credeva e leggeva un listino monco) e
    le righe scritte fuori ordine nel file (l'appaiamento per posizione
    confrontava la riga 5 dell'originale con la riga 2 della copia).  In
    entrambi i casi la guardia rifiutava una copia **perfetta** accusando celle
    innocenti, e il fornitore non riceveva mai il suo ordine.
    """

    RIGHE = {
        1: '<row r="1"><c r="A1" t="inlineStr"><is><t>EAN</t></is></c><c r="B1" t="inlineStr"><is><t>DESCRIZIONE</t></is></c><c r="C1" t="inlineStr"><is><t>ORDINE</t></is></c></row>',
        2: '<row r="2"><c r="A2" t="inlineStr"><is><t>8000000000001</t></is></c><c r="B2" t="inlineStr"><is><t>PRODOTTO UNO</t></is></c><c r="C2"><v>4</v></c></row>',
        3: '<row r="3"><c r="A3" t="inlineStr"><is><t>8000000000002</t></is></c><c r="B3" t="inlineStr"><is><t>PRODOTTO DUE</t></is></c></row>',
        4: '<row r="4"><c r="C4" t="inlineStr"><is><t>SEZIONE SOLARI</t></is></c></row>',
        5: '<row r="5"><c r="A5" t="inlineStr"><is><t>8000000000005</t></is></c><c r="B5" t="inlineStr"><is><t>PRODOTTO CINQUE</t></is></c><c r="C5"><v>7</v></c></row>',
    }

    def setUp(self) -> None:
        temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(temporanea.cleanup)
        self.cartella = Path(temporanea.name)

    def foglio(self, ordine_delle_righe: list[int], dimension: str | None = None) -> str:
        dichiarazione = f'<dimension ref="{dimension}"/>' if dimension else ""
        corpo = "".join(self.RIGHE[numero] for numero in ordine_delle_righe)
        return f"{dichiarazione}<sheetData>{corpo}</sheetData>"

    def test_una_dimension_prudente_non_fa_rifiutare_una_copia_perfetta(self) -> None:
        """La copia della libreria non porta `<dimension>`: se la guardia si
        fida della dichiarazione dell'originale, legge un listino monco e
        accusa la copia di aver inventato le celle che stanno oltre."""

        originale = xlsx_a_mano(self.cartella / "originale.xlsx", self.foglio([1, 2, 3, 4, 5], dimension="A1:B3"))
        copia = xlsx_a_mano(self.cartella / "copia.xlsx", self.foglio([1, 2, 3, 4, 5]))
        esito = confronta_copia(
            originale, copia,
            colonna_ordine="C", prima_riga=2, quantita={}, foglio_ordine="Listino",
        )
        self.assertTrue(esito.fedele, esito.esempi_rifiutati)

    def test_le_righe_scritte_fuori_ordine_si_confrontano_per_numero_di_riga(self) -> None:
        """Ogni `<row>` dichiara il suo numero: l'ordine nel file non conta.
        La libreria le riscrive in salita, e la copia resta perfetta."""

        originale = xlsx_a_mano(self.cartella / "originale.xlsx", self.foglio([1, 5, 3, 2, 4]))
        copia = xlsx_a_mano(self.cartella / "copia.xlsx", self.foglio([1, 2, 3, 4, 5]))
        esito = confronta_copia(
            originale, copia,
            colonna_ordine="C", prima_riga=2, quantita={}, foglio_ordine="Listino",
        )
        self.assertTrue(esito.fedele, esito.esempi_rifiutati)

    def test_le_quantita_si_appaiano_al_numero_di_riga_vero_anche_se_il_foglio_comincia_dopo(self) -> None:
        """Un foglio le cui prime righe non esistono proprio (la prima cella e'
        alla riga 3): `iter_rows` parte dalla prima riga vera, e chi contasse
        le **posizioni** sposterebbe le quantita' del piano su righe sbagliate
        — la riga 4 del piano diventerebbe la posizione 2, la tolleranza
        cadrebbe altrove, e una copia giusta verrebbe rifiutata."""

        r3 = '<row r="3"><c r="A3" t="inlineStr"><is><t>8000000000003</t></is></c><c r="B3" t="inlineStr"><is><t>PRODOTTO TRE</t></is></c></row>'
        r4 = '<row r="4"><c r="A4" t="inlineStr"><is><t>8000000000004</t></is></c><c r="B4" t="inlineStr"><is><t>PRODOTTO QUATTRO</t></is></c></row>'
        r4_compilata = r4.replace("</row>", '<c r="C4"><v>6</v></c></row>')
        r5 = '<row r="5"><c r="C5" t="inlineStr"><is><t>SEZIONE SOLARI</t></is></c></row>'
        originale = xlsx_a_mano(self.cartella / "originale.xlsx", f"<sheetData>{r3}{r4}{r5}</sheetData>")
        copia = xlsx_a_mano(self.cartella / "copia.xlsx", f"<sheetData>{r3}{r4_compilata}{r5}</sheetData>")

        esito = confronta_copia(
            originale, copia,
            colonna_ordine="C", prima_riga=2, quantita={4: 6}, foglio_ordine="Listino",
        )
        self.assertTrue(esito.fedele, esito.esempi_rifiutati)
        self.assertEqual(esito.differenze_ammesse, 1)

    def test_le_righe_fuori_ordine_non_nascondono_una_manomissione(self) -> None:
        """Il fratello di controllo: il confronto per numero di riga vero non
        e' una tolleranza, e una cella cambiata si vede lo stesso."""

        originale = xlsx_a_mano(self.cartella / "originale.xlsx", self.foglio([1, 5, 3, 2, 4]))
        manomessa = self.RIGHE | {
            2: self.RIGHE[2].replace("PRODOTTO UNO", "PRODOTTO UNO MANOMESSO"),
        }
        corpo = "<sheetData>" + "".join(manomessa[n] for n in [1, 2, 3, 4, 5]) + "</sheetData>"
        copia = xlsx_a_mano(self.cartella / "copia.xlsx", corpo)
        esito = confronta_copia(
            originale, copia,
            colonna_ordine="C", prima_riga=2, quantita={}, foglio_ordine="Listino",
        )
        self.assertFalse(esito.fedele)
        self.assertIn("B2", frase_di_rifiuto("LARICE", esito))


# ---------------------------------------------------------------------------
# 3. La guardia dentro la compilazione, dove la vede l'utente
# ---------------------------------------------------------------------------


class ScrittoreCheNonScrive(banco_web.ScrittoreFinto):
    """Consegna il listino **tale e quale**: la colonna d'ordine resta vuota.

    E' il guasto del 14 agosto 2026 in persona, ed e' quello che lo scrittore
    finto faceva senza volerlo fino a quel giorno: `shutil.copy2` e basta.  La
    copia viene rimessa a posto **dopo** il lavoro della classe base, cosi'
    questo collaudo dice la stessa cosa qualunque cosa la base scriva domani.
    """

    def __call__(self, plan_path: Path, destinazione: Path) -> Any:
        generati, prodotte, avvisi = super().__call__(plan_path, destinazione)
        for _fornitore, sorgente, copia in prodotte:
            shutil.copy2(sorgente, copia)
        return generati, prodotte, avvisi


class ScrittoreCheGuasta(banco_web.ScrittoreFinto):
    """Come lo scrittore finto, ma sporca una cella della copia di un fornitore."""

    def __init__(self, fornitori: dict[str, Path], *, guasto: str, cella: str, valore: Any) -> None:
        super().__init__(fornitori)
        self.guasto = guasto
        self.cella = cella
        self.valore = valore

    def __call__(self, plan_path: Path, destinazione: Path) -> Any:
        generati, prodotte, avvisi = super().__call__(plan_path, destinazione)
        for fornitore, _sorgente, copia in prodotte:
            if fornitore != self.guasto:
                continue
            libro = load_workbook(copia)
            try:
                libro[libro.sheetnames[0]][self.cella] = self.valore
                libro.save(copia)
            finally:
                libro.close()
        return generati, prodotte, avvisi


class LaGuardiaDentroLaCompilazione(banco_web.ConsegnaBase):
    def compila(self, scrittore: Any, fornitore: str = "larice") -> dict[str, Any]:
        with mock.patch.object(self.store, "writer_configuration_issues", return_value=[]), \
                mock.patch.object(self.store, "run_writer", scrittore):
            return self.store.compile(self.snapshot(2, fornitore))

    def test_una_copia_infedele_non_viene_consegnata(self) -> None:
        """⚠ La differenza sta in `A`, non nella colonna d'ordine (`D`)."""

        scrittore = ScrittoreCheGuasta(
            {"larice": self.listini["larice"]},
            guasto="larice", cella="A2", valore="1235",
        )
        esito = self.compila(scrittore)

        self.assertEqual(esito["status"], "PLAN_READY")
        self.assertEqual(len(esito["writerIssues"]), 1)
        avviso = esito["writerIssues"][0]
        self.assertIn("LARICE", avviso)
        self.assertIn("non è fedele al listino di partenza", avviso)
        self.assertIn("A2", avviso)
        self.assertIn(avviso, esito["message"])
        # Nessuna copia a meta' consegnabile: il documento non c'e' piu'.
        cartella = self.orders_dir / esito["cartella"]
        self.assertFalse(list(cartella.glob("*.xlsx")), list(cartella.iterdir()))
        self.assertTrue((cartella / "final_order_plan.json").is_file())
        # E l'avviso finisce anche nell'audit, non solo nella risposta.
        audit = json.loads((cartella / "compilazione.json").read_text(encoding="utf-8"))
        self.assertIn(avviso, audit["avvisi"])

    def test_una_copia_infedele_non_porta_via_quelle_buone(self) -> None:
        scrittore = ScrittoreCheGuasta(
            {"larice": self.listini["larice"], "betulla": self.listini["betulla"]},
            guasto="larice", cella="B2", valore="ALTRO PRODOTTO",
        )
        esito = self.compila(scrittore, fornitore="betulla")

        self.assertEqual(esito["status"], "FILES_READY")
        cartella = self.orders_dir / esito["cartella"]
        rimasti = sorted(voce.name for voce in cartella.glob("*.xlsx"))
        self.assertEqual(len(rimasti), 1, rimasti)
        self.assertIn("BETULLA", rimasti[0])
        self.assertIn("Attenzione", esito["message"])
        self.assertIn("LARICE", esito["message"])

    def test_una_copia_fedele_passa_e_non_dice_niente(self) -> None:
        esito = self.compila(banco_web.ScrittoreFinto({"larice": self.listini["larice"]}))

        self.assertEqual(esito["status"], "FILES_READY")
        self.assertEqual(esito["writerIssues"], [])
        self.assertNotIn("Attenzione", esito["message"])

    def test_una_copia_senza_la_quantita_del_piano_non_viene_consegnata(self) -> None:
        """Il difetto del 14 agosto 2026, dove lo vede l'utente.

        Una copia del listino senza dentro le quantita' del piano: fino a oggi
        la compilazione diceva «pronto», rinominava la copia, la metteva nello
        zip e nello storico — un ordine da consegnare al fornitore con dentro
        zero righe ordinate.
        """

        esito = self.compila(ScrittoreCheNonScrive({"larice": self.listini["larice"]}))

        self.assertEqual(esito["status"], "PLAN_READY")
        self.assertEqual(len(esito["writerIssues"]), 1)
        avviso = esito["writerIssues"][0]
        self.assertIn("LARICE", avviso)
        self.assertIn("riga d'ordine richiesta", avviso)
        self.assertIn("riga 10", avviso)
        self.assertIn(avviso, esito["message"])
        # La copia a meta' non resta sul disco, come per le altre infedelta'.
        cartella = self.orders_dir / esito["cartella"]
        self.assertFalse(list(cartella.glob("*.xlsx")), list(cartella.iterdir()))

    def test_una_copia_che_non_si_riesce_a_verificare_non_si_consegna(self) -> None:
        class ScrittoreCheRompe(banco_web.ScrittoreFinto):
            def __call__(self, plan_path: Path, destinazione: Path) -> Any:
                generati, prodotte, avvisi = super().__call__(plan_path, destinazione)
                for _fornitore, _sorgente, copia in prodotte:
                    copia.write_bytes(b"non sono un foglio di calcolo")
                return generati, prodotte, avvisi

        esito = self.compila(ScrittoreCheRompe({"larice": self.listini["larice"]}))

        self.assertEqual(esito["status"], "PLAN_READY")
        self.assertEqual(len(esito["writerIssues"]), 1)
        self.assertIn("non è stata verificata", esito["writerIssues"][0])
        self.assertFalse(list((self.orders_dir / esito["cartella"]).glob("*.xlsx")))


# ---------------------------------------------------------------------------
# 3-bis. Il riepilogo di fine lavoro, letto dal servizio
# ---------------------------------------------------------------------------


class IlPrezzoDiUnOfferta(unittest.TestCase):
    """`offer_pricing`: i due versi del calcolo, non uno solo.

    ⚠ Il ramo che ricava il prezzo del pezzo dal prezzo d'ordine non era
    misurato da nessuno — il suo simmetrico ha dodici test (revisione
    dell'integrità dei test, 14 agosto 2026). È il ramo che serve a un'offerta
    che dichiara solo il totale del collo, e un fattore sbagliato lì dentro
    falsa il confronto fra fornitori, che si fa **sempre sul prezzo al pezzo**.
    """

    def prezzo(self, **campi: Any) -> dict[str, Any] | None:
        return banco_web.SERVER.offer_pricing(campi)

    def test_dal_prezzo_del_pezzo_si_ricava_quello_del_collo(self) -> None:
        self.assertEqual(
            self.prezzo(unitPriceNet=1.5, quantityFactor=6),
            {"factor": 6.0, "unitPriceNet": 1.5, "orderUnitPriceNet": 9.0},
        )

    def test_dal_prezzo_del_collo_si_ricava_quello_del_pezzo(self) -> None:
        self.assertEqual(
            self.prezzo(orderUnitPriceNet=9.0, quantityFactor=6),
            {"factor": 6.0, "unitPriceNet": 1.5, "orderUnitPriceNet": 9.0},
        )

    def test_senza_fattore_dichiarato_il_pezzo_e_il_collo(self) -> None:
        """Ripiego prudente: un collo di uno costa quanto il collo."""

        self.assertEqual(
            self.prezzo(orderUnitPriceNet=9.0),
            {"factor": 1.0, "unitPriceNet": 9.0, "orderUnitPriceNet": 9.0},
        )

    def test_senza_nessun_prezzo_non_si_inventa_niente(self) -> None:
        self.assertIsNone(self.prezzo(quantityFactor=6))
        self.assertIsNone(self.prezzo(orderUnitPriceNet=-1.0, quantityFactor=6))


class LImprontaDellArticoloConfermato(banco_web.ConsegnaBase):
    """Che cosa è stato confermato, non solo che qualcosa lo è stato.

    ⚠ Il consumatore di questa impronta — `_ripulisci_stato`, che dopo il
    ricalcolo toglie la spunta se l'articolo è cambiato — ha tre test e tutti
    costruiscono lo stato **a mano**, con l'impronta già dentro. Chi la scrive
    non era misurato da nessuno: si poteva togliere la riga e restavano 507 test
    verdi (revisione dell'integrità dei test, 14 agosto 2026). E senza,
    ogni conferma scadrebbe a ogni ricalcolo: l'utente rimetterebbe tutte le
    spunte ogni settimana.
    """

    def test_il_salvataggio_scrive_l_impronta_di_quello_che_e_stato_confermato(self) -> None:
        self.store.save_state(self.snapshot(2, "larice"))

        stato = json.loads(self.store.state_path.read_text(encoding="utf-8"))
        voce = next(riga for riga in stato["products"] if riga["id"] == "product-standard")
        self.assertTrue(voce["confirmed"])
        offerta = next(
            candidata
            for prodotto in json.loads(self.review_path.read_text(encoding="utf-8"))["products"]
            if prodotto["id"] == "product-standard"
            for candidata in prodotto["offers"]
            if candidata["supplierId"] == "larice"
        )
        self.assertEqual(voce["confirmedArticle"], banco_web.SERVER.impronta_articolo(offerta))

    def test_senza_conferma_non_c_e_nessuna_impronta(self) -> None:
        snapshot = self.snapshot(2, "larice")
        snapshot["products"][0]["confirmed"] = False

        self.store.save_state(snapshot)

        stato = json.loads(self.store.state_path.read_text(encoding="utf-8"))
        voce = next(riga for riga in stato["products"] if riga["id"] == "product-standard")
        self.assertNotIn("confirmedArticle", voce)


class GliAvvisiDellaConsegna(banco_web.ConsegnaBase):
    """Un nome che non si è potuto dare non deve restare una notizia di passaggio.

    ⚠ `os.rename` su Windows fallisce se qualcuno tiene aperto il file — Excel,
    OneDrive, l'antivirus. Il documento resta giusto e consegnabile con il nome
    brutto, e il programma lo diceva **solo** dentro `message`: il riquadro
    restava verde col pulsante primario, e al ricaricamento della pagina la
    notizia spariva del tutto, perché la voce dell'elenco non portava gli avvisi
    dell'audit (revisione di regressione del 14 agosto 2026).
    """

    def compila_con_la_rinomina_rotta(self) -> dict[str, Any]:
        def rename_che_fallisce(sorgente: Any, destinazione: Any) -> None:
            raise PermissionError(32, "Impossibile accedere al file. Il file è in uso")

        with mock.patch.object(banco_web.SERVER.os, "rename", rename_che_fallisce):
            return self.compila_con_listini(2)

    def test_la_risposta_ha_un_campo_per_gli_avvisi_della_consegna(self) -> None:
        esito = self.compila_con_la_rinomina_rotta()

        self.assertEqual(esito["status"], "FILES_READY")
        self.assertEqual(len(esito["deliveryIssues"]), 1, esito["deliveryIssues"])
        self.assertIn("è rimasto con questo nome", esito["deliveryIssues"][0])
        # Il documento c'è ed è consegnabile: l'avviso non è un rifiuto.
        self.assertEqual(esito["zipUrl"], f"/ordini/{esito['cartella']}/zip")

    def test_l_avviso_sopravvive_al_ricaricamento_della_pagina(self) -> None:
        esito = self.compila_con_la_rinomina_rotta()

        voce = banco_web.CONSEGNA.voce(self.orders_dir / esito["cartella"])

        self.assertTrue(any("è rimasto con questo nome" in avviso for avviso in voce["avvisi"]),
                        voce["avvisi"])

    def test_una_consegna_pulita_non_ha_avvisi(self) -> None:
        esito = self.compila_con_listini(2)

        self.assertEqual(esito["deliveryIssues"], [])
        self.assertEqual(banco_web.CONSEGNA.voce(self.orders_dir / esito["cartella"])["avvisi"], [])


class LaSchedaCheRestaIndietroDopoUnaCompilazione(banco_web.ConsegnaBase):
    """Chi ha riscritto lo stato conta, e la frase deve dirlo.

    ⚠ `compile` salva lo stato **prima** di preparare i listini. Se dopo
    quel punto qualcosa va storto, la scheda resta indietro di una versione e il
    salvataggio successivo veniva rifiutato con «un'altra scheda del comparatore
    ha salvato dopo di te»: l'utente andava a cercare un collega che non esiste,
    e il pulsante «riprova» non poteva funzionare perché rifà il salvataggio per
    primo (revisione di regressione del 14 agosto 2026).
    """

    def stato_sul_disco(self) -> dict[str, Any]:
        return json.loads(self.store.state_path.read_text(encoding="utf-8"))

    def test_dopo_una_compilazione_lo_stato_dice_chi_l_ha_scritto(self) -> None:
        self.compila_con_listini(2)

        self.assertEqual(self.stato_sul_disco()["stateVersionOrigin"], "compilazione")

    def test_un_salvataggio_normale_resta_della_scheda(self) -> None:
        self.store.save_state(self.snapshot(1))

        self.assertEqual(self.stato_sul_disco()["stateVersionOrigin"], "scheda")

    def test_la_scheda_superata_dalla_propria_compilazione_lo_legge(self) -> None:
        self.compila_con_listini(2)
        # La scheda ha ancora in mano la versione di prima.
        sorpassato = {**self.snapshot(3), "stateVersion": 0}

        _pulito, errori, _confronto = self.store.validate_snapshot(sorpassato, for_compile=False)

        self.assertEqual(len(errori), 1, errori)
        self.assertEqual(errori[0]["code"], "STATO_SOVRASCRITTO")
        self.assertEqual(errori[0]["origin"], "compilazione")
        self.assertEqual(errori[0]["stateVersion"], self.stato_sul_disco()["stateVersion"])
        frase = banco_web.SERVER.frase_di_un_errore(errori[0])
        self.assertIn("le ha già riscritte l'ultima compilazione", frase)
        self.assertNotIn("un'altra scheda", frase)

    def test_la_scheda_superata_da_un_altra_scheda_legge_la_frase_di_prima(self) -> None:
        self.store.save_state(self.snapshot(1))
        sorpassato = {**self.snapshot(3), "stateVersion": 0}

        _pulito, errori, _confronto = self.store.validate_snapshot(sorpassato, for_compile=False)

        self.assertEqual(errori[0]["origin"], "scheda")
        self.assertIn("un'altra scheda", banco_web.SERVER.frase_di_un_errore(errori[0]))


class IlNomeCheArrivaAlFornitore(banco_web.ConsegnaBase):
    """L'ultimo anello: il nome con cui il documento parte davvero.

    ⚠ Il writer costruisce la copia intermedia con il nome del **registro**, e
    un passo dopo `rinomina_listini` rifaceva il nome leggibile
    dall'identificativo tecnico: il documento allegato alla mail si chiamava
    «Ordine NUOVO_FORNITORE_1 — 14 agosto 2026.xlsx», underscore compresi —
    esattamente la stringa che il cantiere R8 dichiarava di aver tolto
    (revisione di regressione del 14 agosto 2026).
    """

    def test_il_nome_leggibile_non_torna_all_identificativo_tecnico(self) -> None:
        cartella = self.orders_dir / "2026-08-14_1200"
        cartella.mkdir(parents=True)
        prodotta = banco_web.listino_finto(cartella / "ORDINE_NUOVO_FORNITORE_1_listino.xlsx")
        momento = datetime(2026, 8, 14, 12, 0)

        rinominate, problemi = banco_web.SERVER.ReviewStore.rinomina_listini(
            [("nuovo_fornitore_1", self.listini["larice"], prodotta)], momento,
        )

        self.assertEqual(problemi, [])
        self.assertEqual(rinominate[0][2].name, "Ordine NUOVO FORNITORE 1 — 14 agosto 2026.xlsx")
        self.assertTrue(rinominate[0][2].is_file())

    def test_i_quattro_fornitori_di_oggi_si_chiamano_come_prima(self) -> None:
        cartella = self.orders_dir / "2026-08-14_1201"
        cartella.mkdir(parents=True)
        prodotta = banco_web.listino_finto(cartella / "ORDINE_LARICE_listino.xlsx")

        rinominate, _problemi = banco_web.SERVER.ReviewStore.rinomina_listini(
            [("larice", self.listini["larice"], prodotta)], datetime(2026, 8, 14, 12, 0),
        )

        self.assertEqual(rinominate[0][2].name, "Ordine LARICE — 14 agosto 2026.xlsx")


class DueOrdiniSullaStessaRigaDelListino(banco_web.ConsegnaBase):
    """⚠ Il bloccante della revisione avversariale del 14 agosto 2026.

    I due compilatori — il writer Node e `xls_writer` per Noce — sommano le
    quantita' di due righe del piano che puntano alla stessa riga del listino, e
    per due articoli del gestionale che sono lo stesso articolo del fornitore
    quella somma e' giusta.

    Non lo e' fra un collo e un espositore, e il programma sa costruire il caso
    da solo: l'offerta di un espositore porta il numero di riga del suo **collo
    padre**, e la riga padre resta ordinabile per conto suo. Quattro colli piu'
    sei espositori diventavano un `10` in una cella sola, e il fornitore leggeva
    dieci di qualcosa che nessuno aveva ordinato.
    """

    def ordine(self, **campi: Any) -> dict[str, Any]:
        base = {
            "product_id": "prodotto",
            "description": "PRODOTTO STANDARD",
            "supplier": "larice",
            "supplier_source_row": 10,
            "quantity": 4,
            "desired_quantity_unit": "colli",
            "quantity_factor": 6,
        }
        return {**base, **campi}

    def test_un_collo_e_un_espositore_sulla_stessa_riga_fermano_la_compilazione(self) -> None:
        errori = banco_web.SERVER.ReviewStore.righe_di_listino_contese([
            self.ordine(),
            self.ordine(product_id="espositore", description="ESPOSITORE MISTO",
                        quantity=6, desired_quantity_unit="espositori", quantity_factor=96),
        ])

        self.assertEqual(len(errori), 1, errori)
        self.assertEqual(errori[0]["code"], "RIGA_LISTINO_CONTESA")
        self.assertEqual(errori[0]["sourceRow"], 10)
        frase = banco_web.SERVER.frase_di_un_errore(errori[0])
        self.assertIn("stessa riga 10", frase)
        self.assertIn("ESPOSITORE MISTO", frase)
        self.assertIn("un numero solo", frase)

    def test_lo_stesso_articolo_del_fornitore_per_due_articoli_del_gestionale_si_somma(self) -> None:
        """L'altra meta' della regola: qui la somma e' quello che si vuole.

        Due codici del gestionale che sono lo stesso articolo del fornitore
        ordinano la stessa merce nella stessa unita': fermarsi sarebbe togliere
        all'utente un ordine legittimo.
        """

        self.assertEqual(banco_web.SERVER.ReviewStore.righe_di_listino_contese([
            self.ordine(product_id="uno"),
            self.ordine(product_id="due", quantity=3),
        ]), [])

    def test_righe_diverse_dello_stesso_fornitore_non_si_contendono_niente(self) -> None:
        self.assertEqual(banco_web.SERVER.ReviewStore.righe_di_listino_contese([
            self.ordine(product_id="uno"),
            self.ordine(product_id="due", supplier_source_row=11),
        ]), [])

    def test_la_compilazione_vera_si_ferma_e_lo_dice(self) -> None:
        """La guardia collegata: dal confronto fino al rifiuto della compilazione."""

        confronto = json.loads(self.review_path.read_text(encoding="utf-8"))
        espositore = next(voce for voce in confronto["products"] if voce["id"] == "display-solbao-96")
        # L'espositore porta il numero di riga del suo collo padre: e' quello che
        # `display_offer` scrive davvero.
        espositore["offers"][0]["sourceRow"] = 10
        self.review_path.write_text(json.dumps(confronto), encoding="utf-8")
        snapshot = {
            "runId": "run-sintetica",
            "currentStep": 3,
            "acceptBelowThreshold": True,
            "products": [
                {"id": "product-standard", "quantity": 4, "selectedSupplierId": "larice", "confirmed": True},
                {"id": "display-solbao-96", "quantity": 6, "selectedSupplierId": "larice", "confirmed": True},
            ],
        }

        with self.assertRaises(banco_web.SnapshotError) as fermata:
            self.store.compile(snapshot)

        codici = {str(voce.get("code")) for voce in fermata.exception.errors}
        self.assertIn("RIGA_LISTINO_CONTESA", codici, fermata.exception.errors)
        # E non resta in giro nessuna cartella di consegna a meta'.
        self.assertFalse(list(self.orders_dir.glob("*")) if self.orders_dir.is_dir() else [])


class IlRiepilogoDelWriter(banco_web.ConsegnaBase):
    """Il writer dichiara che cosa ha prodotto: il servizio gli crede.

    ⚠ Il nome della copia lo costruisce il writer con il `display_name` che la
    regola di scrittura porta dal registro — «Sapori & Co.» diventa
    `ORDINE_SAPORI_CO_<listino>.xlsx` — mentre il servizio se lo ricalcolava
    dall'identificativo tecnico.  Per i quattro fornitori di oggi i due nomi
    coincidono per combinazione, perche' l'identificativo maiuscolo *e'* il
    nome; al primo fornitore imparato con un nome vero la compilazione
    sarebbe fallita con la copia giusta li' accanto, e l'utente avrebbe letto
    «Il writer non ha creato la copia prevista».

    L'altra meta' del riepilogo sono le righe che il writer ha scritto **senza
    poter controllare** che fossero quelle giuste: le sue frasi finivano sulla
    console del programma, cioe' in nessun posto che qualcuno guardi.
    """

    def prepara(self, fornitore: str, listino: Path) -> tuple[Path, Path]:
        """Configurazione di scrittura, cartella della compilazione e piano."""

        node = self.root / "node.exe"
        node.write_bytes(b"")
        script = self.root / "writer.mjs"
        script.write_bytes(b"")
        self.writer_config.write_text(json.dumps({
            "node_executable": str(node),
            "writer_script": str(script),
            "supplier_files": {fornitore: str(listino)},
        }), encoding="utf-8")
        cartella = self.orders_dir / "2026-08-14_1130"
        cartella.mkdir(parents=True)
        piano = cartella / "final_order_plan.json"
        piano.write_text(json.dumps({"orders": [{"supplier": fornitore}]}), encoding="utf-8")
        return piano, cartella

    def esegui(
        self,
        piano: Path,
        cartella: Path,
        *,
        crea: str | None,
        riepilogo: dict[str, Any] | None,
    ) -> Any:
        """Node sostituito: crea il documento che gli si dice e stampa il riepilogo.

        ⚠ Su `stdout` c'e' anche la riga della libreria dei fogli di calcolo,
        apposta: e' il motivo per cui il riepilogo e' marcato invece di essere
        tutto quello che il writer stampa.
        """

        def esecuzione_finta(command: list[str], **_altro: Any) -> Any:
            destinazione = Path(command[command.index("--output-dir") + 1])
            if crea:
                banco_web.listino_finto(destinazione / crea)
            uscita = "Inspect result written to file: C:\\temp\\copia.inspect.ndjson\n"
            if riepilogo is not None:
                uscita += "RIEPILOGO_COMPILAZIONE: " + json.dumps(riepilogo) + "\n"
                # Il blocco indentato per gli occhi, che il writer vero stampa
                # dopo quello marcato: non deve confondere chi legge.
                uscita += json.dumps(riepilogo, indent=2) + "\n"
            return subprocess.CompletedProcess(command, 0, uscita, "")

        with mock.patch.object(banco_web.SERVER.subprocess, "run", esecuzione_finta):
            return self.store.run_writer(piano, cartella)

    @staticmethod
    def copia_dichiarata(cartella: Path, nome: str, fornitore: str, **numeri: Any) -> dict[str, Any]:
        return {
            "supplier_copies": [{
                "supplier": fornitore,
                "supplier_name": numeri.pop("supplier_name", fornitore.upper()),
                "destination": str(cartella / nome),
                "written_rows": 1,
                "verified_rows": numeri.pop("verified_rows", 1),
                "unverifiable_rows": numeri.pop("unverifiable_rows", 0),
                "warnings": numeri.pop("warnings", []),
                **numeri,
            }],
            "noce_lines": 0,
            "noce_note": None,
        }

    def test_il_nome_della_copia_lo_dichiara_il_writer(self) -> None:
        """Il fornitore imparato: identificativo `sapori`, nome «Sapori & Co.»."""

        listino = banco_web.listino_finto(self.root / "listino_sapori.xlsx", "SAPORI")
        piano, cartella = self.prepara("sapori", listino)
        nome = "ORDINE_SAPORI_CO_listino_sapori.xlsx"

        generati, prodotte, avvisi = self.esegui(
            piano, cartella,
            crea=nome,
            riepilogo=self.copia_dichiarata(cartella, nome, "sapori", supplier_name="Sapori & Co."),
        )

        self.assertEqual(prodotte, [("sapori", listino.resolve(), cartella / nome)])
        self.assertIn(cartella / nome, generati)
        self.assertEqual(avvisi, [])
        # Il nome che il servizio avrebbe ricalcolato non esiste sul disco: e'
        # esattamente la differenza che questa prova difende.
        self.assertFalse((cartella / "ORDINE_SAPORI_listino_sapori.xlsx").exists())

    def test_un_fornitore_che_il_riepilogo_non_nomina_ferma_la_compilazione(self) -> None:
        """Il writer ha parlato e questo fornitore non l'ha nominato.

        Il documento col nome che ci aspettiamo c'e' anche, e non basta: se lo
        avessimo preso, avremmo consegnato un file che il writer non dichiara
        di aver scritto in questa compilazione.
        """

        piano, cartella = self.prepara("larice", self.listini["larice"])

        with self.assertRaises(ValueError) as errore:
            self.esegui(
                piano, cartella,
                crea="ORDINE_LARICE_listino_larice.xlsx",
                riepilogo={"supplier_copies": [], "noce_lines": 0},
            )

        self.assertIn("LARICE", str(errore.exception))
        self.assertIn("non dichiara nessuna copia", str(errore.exception))

    def test_una_copia_saltata_dal_writer_non_si_prende_lo_stesso(self) -> None:
        """`skipped` è il writer che dice «configurato, ma non ordinato»."""

        piano, cartella = self.prepara("larice", self.listini["larice"])
        riepilogo = self.copia_dichiarata(cartella, "ORDINE_LARICE_listino_larice.xlsx", "larice")
        riepilogo["supplier_copies"][0]["destination"] = None
        riepilogo["supplier_copies"][0]["skipped"] = True

        with self.assertRaises(ValueError) as errore:
            self.esegui(piano, cartella, crea="ORDINE_LARICE_listino_larice.xlsx", riepilogo=riepilogo)

        self.assertIn("non dichiara nessuna copia", str(errore.exception))

    def test_una_copia_dichiarata_fuori_dalla_cartella_non_si_consegna(self) -> None:
        """La consegna è la cartella datata: da lì escono lo zip e l'elenco."""

        piano, cartella = self.prepara("larice", self.listini["larice"])
        estranea = self.root / "ORDINE_LARICE_listino_larice.xlsx"
        banco_web.listino_finto(estranea)
        riepilogo = self.copia_dichiarata(cartella, "x.xlsx", "larice")
        riepilogo["supplier_copies"][0]["destination"] = str(estranea)

        with self.assertRaises(ValueError) as errore:
            self.esegui(piano, cartella, crea=None, riepilogo=riepilogo)

        self.assertIn("fuori dalla cartella della compilazione", str(errore.exception))

    def test_le_righe_scritte_senza_verifica_si_contano(self) -> None:
        """Nessuna frase per riga, ma tre righe nessuno le ha controllate."""

        piano, cartella = self.prepara("larice", self.listini["larice"])
        nome = "ORDINE_LARICE_listino_larice.xlsx"

        _generati, _prodotte, avvisi = self.esegui(
            piano, cartella,
            crea=nome,
            riepilogo=self.copia_dichiarata(
                cartella, nome, "larice",
                supplier_name="LARICE", verified_rows=2, unverifiable_rows=3,
            ),
        )

        self.assertEqual(len(avvisi), 1, avvisi)
        self.assertIn("LARICE", avvisi[0])
        self.assertIn("3 righe su 5", avvisi[0])
        self.assertIn("senza poter verificare", avvisi[0])

    def test_una_riga_sola_si_dice_al_singolare(self) -> None:
        piano, cartella = self.prepara("larice", self.listini["larice"])
        nome = "ORDINE_LARICE_listino_larice.xlsx"

        _generati, _prodotte, avvisi = self.esegui(
            piano, cartella,
            crea=nome,
            riepilogo=self.copia_dichiarata(
                cartella, nome, "larice", verified_rows=4, unverifiable_rows=1,
            ),
        )

        self.assertEqual(len(avvisi), 1, avvisi)
        self.assertIn("una riga su 5", avvisi[0])

    def test_le_righe_che_hanno_gia_la_loro_frase_non_si_contano_due_volte(self) -> None:
        """L'avviso del writer nomina la riga: ripeterlo come numero è rumore."""

        piano, cartella = self.prepara("larice", self.listini["larice"])
        nome = "ORDINE_LARICE_listino_larice.xlsx"
        detta = (
            "Il piano non dice quale prodotto sia la riga 10, ma il listino LARICE lì porta "
            "l'EAN 8000000000010: la quantità è stata scritta lo stesso, controlla che sia la "
            "riga giusta."
        )

        _generati, _prodotte, avvisi = self.esegui(
            piano, cartella,
            crea=nome,
            riepilogo=self.copia_dichiarata(
                cartella, nome, "larice",
                verified_rows=4, unverifiable_rows=1, warnings=[detta],
            ),
        )

        self.assertEqual(avvisi, [detta])

    def test_gli_avvisi_del_writer_arrivano_dove_li_legge_l_utente(self) -> None:
        """Fino a oggi morivano sulla console: la copia usciva senza una parola."""

        detta = (
            "La riga 10 del listino LARICE è descritta «ALTRO PRODOTTO» e il piano dice "
            "«PRODOTTO STANDARD»: la quantità è stata scritta lo stesso, controlla che sia "
            "la riga giusta."
        )
        scrittore = banco_web.ScrittoreFinto({"larice": self.listini["larice"]}, avvisi=(detta,))
        with mock.patch.object(self.store, "writer_configuration_issues", return_value=[]), \
                mock.patch.object(self.store, "run_writer", scrittore):
            esito = self.store.compile(self.snapshot(2, "larice"))

        # La copia si consegna: l'avviso non e' un rifiuto.
        self.assertEqual(esito["status"], "FILES_READY")
        self.assertEqual(esito["writerIssues"], [detta])
        # ⚠ Nel messaggio il **numero**, non il testo: la pagina l'elenco ce
        # l'ha gia' da `writerIssues`, e ripeterlo lo faceva comparire due volte
        # (revisione avversariale del 14 agosto 2026). Il numero resta perche'
        # lo storico delle compilazioni mostra il messaggio e non l'elenco.
        self.assertIn("Su un listino preparato c'è una segnalazione da leggere.", esito["message"])
        self.assertNotIn(detta, esito["message"])
        cartella = self.orders_dir / esito["cartella"]
        self.assertEqual(len(list(cartella.glob("*.xlsx"))), 1)
        # E restano scritti anche nell'audit, che e' il documento che sopravvive
        # alla pagina.
        audit = json.loads((cartella / "compilazione.json").read_text(encoding="utf-8"))
        self.assertIn(detta, audit["avvisi"])

    def test_l_avviso_del_writer_non_diventa_il_motivo_di_una_copia_mai_creata(self) -> None:
        """«La quantità è stata scritta lo stesso» dentro «non è stato creato niente».

        Quando tutte le copie vengono scartate, il motivo sono le copie
        scartate. Gli avvisi del writer parlano di righe **scritte**: messi lì
        dentro si contraddicono a vicenda (revisione del 14 agosto 2026).
        """

        detta = (
            "La riga 10 del listino LARICE è descritta «ALTRO» e il piano dice «PRODOTTO "
            "STANDARD»: la quantità è stata scritta lo stesso, controlla che sia la riga giusta."
        )
        scrittore = ScrittoreCheNonScrive({"larice": self.listini["larice"]}, avvisi=(detta,))
        with mock.patch.object(self.store, "writer_configuration_issues", return_value=[]), \
                mock.patch.object(self.store, "run_writer", scrittore):
            esito = self.store.compile(self.snapshot(2, "larice"))

        self.assertEqual(esito["status"], "PLAN_READY")
        self.assertIn("Non sono state create copie dei listini", esito["message"])
        self.assertNotIn(detta, esito["message"])
        self.assertIn("riga d'ordine richiesta", esito["message"])
        # Nell'elenco invece ci sono tutti e due, ed è il posto giusto.
        self.assertIn(detta, esito["writerIssues"])
        self.assertEqual(len(esito["writerIssues"]), 2, esito["writerIssues"])

    def test_l_avviso_del_writer_sta_davanti_a_quello_della_copia_scartata(self) -> None:
        """Due voci diverse: una parla della copia consegnata, l'altra di quella no."""

        detta = "Di LARICE 2 righe su 6 sono state scritte senza poter verificare che fossero le righe giuste."
        scrittore = ScrittoreCheGuasta(
            {"larice": self.listini["larice"], "betulla": self.listini["betulla"]},
            guasto="larice", cella="B2", valore="ALTRO PRODOTTO",
        )
        scrittore.avvisi = [detta]
        with mock.patch.object(self.store, "writer_configuration_issues", return_value=[]), \
                mock.patch.object(self.store, "run_writer", scrittore):
            esito = self.store.compile(self.snapshot(2, "betulla"))

        self.assertEqual(esito["status"], "FILES_READY")
        self.assertEqual(len(esito["writerIssues"]), 2, esito["writerIssues"])
        self.assertEqual(esito["writerIssues"][0], detta)
        self.assertIn("non è fedele al listino di partenza", esito["writerIssues"][1])


# ---------------------------------------------------------------------------
# 4. Che cosa legge l'utente quando il writer si ferma
# ---------------------------------------------------------------------------


class IlLanciatoreConIlSoloNode(unittest.TestCase):
    """Dal 5 settembre 2026 al writer basta Node.

    La libreria .NET in WebAssembly che ricostruiva il file da capo non c'e'
    piu': `scripts/lib/xlsx_in_posizione.mjs` tocca il solo XML del foglio con
    la libreria standard.  Il lanciatore non deve piu' cercare un
    `node_modules`, ne' scriverlo in configurazione — un `node_modules`
    dichiarato e poi assente fermava la compilazione per una cartella che non
    serve (difetto I5, 12 agosto 2026).
    """

    @classmethod
    def setUpClass(cls) -> None:
        import importlib.util

        percorso = SKILL_ROOT / "app" / "launcher.py"
        spec = importlib.util.spec_from_file_location("compara_ordini_launcher_writer", percorso)
        if spec is None or spec.loader is None:
            raise unittest.SkipTest("Impossibile importare il lanciatore")
        modulo = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = modulo
        spec.loader.exec_module(modulo)
        cls.launcher = modulo

    def test_basta_node_e_la_configurazione_non_dichiara_cartelle(self) -> None:
        if node_disponibile() is None:
            self.skipTest("Node non disponibile")
        runtime = self.launcher.find_node_runtime()
        self.assertIsNotNone(runtime, "con Node sul PC il writer deve accendersi")
        self.assertFalse(hasattr(runtime, "node_modules"))
        temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(temporanea.cleanup)
        destinazione = Path(temporanea.name) / "writer_config.json"
        setup = self.launcher.prepare_writer_config(
            {"run": {"id": "x"}, "files": []}, write=True, destinazione=destinazione,
        )
        self.assertIsNotNone(setup.node_runtime)
        config = json.loads(destinazione.read_text(encoding="utf-8"))
        self.assertNotIn("node_modules", config)
        self.assertIn("node_executable", config)

    def test_senza_node_manca_soltanto_node(self) -> None:
        with mock.patch.object(self.launcher, "find_node_runtime", return_value=None):
            _node, _sorgenti, _regole, mancanti, _avvisi = self.launcher.writer_readiness({"files": []})
        self.assertEqual([voce for voce in mancanti if "Node" in voce], ["Node 18+"])


class LaSpiegazioneDelWriter(unittest.TestCase):
    def spiega(self, stderr: str, stdout: str = "") -> str:
        return banco_web.ReviewStore.spiegazione_del_writer(stderr, stdout)

    def test_la_frase_marcata_e_quella_che_arriva(self) -> None:
        stderr = (
            "Avviso: qualcosa di tecnico\n"
            "ERRORE_COMPILAZIONE: Il listino LARICE è cambiato dopo la verifica: non creo "
            "copie finché non viene ricontrollato.\n"
        )
        self.assertEqual(
            self.spiega(stderr),
            "Il listino LARICE è cambiato dopo la verifica: non creo copie finché non viene ricontrollato.",
        )

    def test_una_traccia_di_node_non_arriva_mai_all_utente(self) -> None:
        """⚠ È quello che l'utente si trovava scritto in pagina il 12 agosto."""

        stderr = (
            "file:///C:/Users/HP/Desktop/comparatore-ordini/scripts/write.mjs:196\n"
            "    throw new Error(...)\n"
            "          ^\n"
            "Error: Il listino LARICE non coincide\n"
            "    at prepareCopy (file:///C:/Users/HP/.../write.mjs:196:11)\n"
            "    at async file:///C:/Users/HP/.../write.mjs:321:21\n"
            "Node.js v24.14.0\n"
        )
        spiegazione = self.spiega(stderr)
        self.assertNotIn("C:", spiegazione)
        self.assertNotIn("at ", spiegazione)
        self.assertIn("si è fermato senza spiegare perché", spiegazione)
        self.assertIn("Il piano ordini è completo", spiegazione)

    def test_una_marca_vuota_non_conta_come_spiegazione(self) -> None:
        self.assertIn("senza spiegare perché", self.spiega("ERRORE_COMPILAZIONE:   \n"))


if __name__ == "__main__":
    unittest.main()
