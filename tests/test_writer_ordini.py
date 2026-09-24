"""The `.xlsx` copies delivered to suppliers: they must still be the supplier's price list.

The Node writer had no test coverage at all before this file: a real supplier
copy could gain 34 stray "1235" strings in the EAN column, or lose 641
section titles from the order column, and the program would still report
success.

Four things this file protects, in one line each:

1. An empty shared string must never become its own index. The library
   that writes `.xlsx` files misreads those cells and stores the
   `sharedStrings.xml` entry index instead. The test price list is built by
   hand, XML included: openpyxl never generates an empty shared-string
   entry, so without it the defect can't show up in the file.
2. A row that isn't a product must not be erased. Zeroing out existing
   quantities is still correct — without it, ghost rows would be shipped —
   but on LARICE the order column also carries section titles, and clearing
   the whole column took those with it. A quantity is a number.
3. The library is never trusted blindly. After writing, the copy is
   reopened and compared cell by cell against the source price list: the
   only differences allowed are in the order column. Any other difference
   fails compilation for that supplier, and the copy is never delivered.
4. Whatever stops the run says so in Italian, not as a raw Node stack
   trace with absolute paths: a price list changed after the check must
   reach the user through the writer's own sentence.
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
# A `.xlsx` built by hand, because openpyxl can't produce what these tests need
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
    """Test price list, written XML by hand.

    Built by hand because openpyxl never generates an empty
    `sharedStrings.xml` entry, and without a cell referencing one the
    defect this test protects against can't exist in the file.

    Every row below is needed:

    | row | A (EAN)                    | B             | C (order)        |
    |-----|-----------------------------|---------------|-------------------|
    | 1   | EAN                         | DESCRIZIONE   | ORDINE            |
    | 2   | *empty shared string*       | PRODOTTO DUE  | 3 (pre-existing)  |
    | 3   | 8000000000003                | PRODOTTO TRE  | — (plan: 5)       |
    | 4   | —                            | —             | SEZIONE SOLARI    |
    | 5   | *empty shared string*       | PRODOTTO CIN. | 7 (pre-existing)  |
    """

    condivise = [
        "EAN",              # 0
        "DESCRIZIONE",      # 1
        "ORDINE",           # 2
        "",                 # 3  <- the empty entry: this is the whole point
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
    """A `.xlsx` with a hand-written sheet body, everything else minimal.

    For shapes openpyxl never generates on its own: a `<dimension>` that
    doesn't match the real data, rows written out of order. Cells are
    written as `inlineStr` or numbers, so no real `sharedStrings` is needed.
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
# 1. The Node writer, exercised end to end
# ---------------------------------------------------------------------------


class IlWriterNodeSulCampo(unittest.TestCase):
    """A single real write, with every assertion made against it.

    The library alone takes about ten seconds just to load: repeating the
    write for every assertion would add minutes to the suite without
    testing anything more.
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
        # The writer has no library to look up: the variable stays pointed at
        # a folder that doesn't exist, to prove nothing reads it anymore.
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
            # Node writes UTF-8; without this, Python on Windows reads with
            # the console codepage and the Italian sentence comes out garbled.
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
        """`OAI_NODE_MODULES` points at nothing and compilation still succeeds:
        the writer only ever uses what Node ships with."""

        self.assertEqual(self.esito.returncode, 0, self.esito.stderr or self.esito.stdout)
        self.assertTrue(self.copia.is_file())
        self.assertEqual(self.esito.stderr.strip(), "")

    def test_una_stringa_condivisa_vuota_non_diventa_il_suo_indice(self) -> None:
        """`A2` and `A5` must stay empty in the copy, not turn into "3".

        `A2` and `A5` in the source price list reference entry number 3
        of `sharedStrings.xml`, which is empty text. If the copy shows "3",
        the library delivered the index instead of the value.
        """

        for cella in ("A2", "A5"):
            with self.subTest(cella=cella):
                valore = self.valore(cella)
                self.assertNotEqual(str(valore), "3", "la copia porta l'indice invece del vuoto")
                self.assertIn(valore, (None, ""), f"{cella} dovrebbe essere vuota, è {valore!r}")

    def test_una_riga_che_non_e_un_prodotto_sopravvive(self) -> None:
        """A section title in the order column must not be erased as if it were a quantity."""

        self.assertEqual(self.valore("C4"), "SEZIONE SOLARI")
        # The header, above the first data row, is untouched too.
        self.assertEqual(self.valore("C1"), "ORDINE")

    def test_una_quantita_preesistente_su_una_riga_prodotto_viene_azzerata(self) -> None:
        """Without this, rows ordered in a previous run would ship again."""

        for cella in ("C2", "C5"):
            with self.subTest(cella=cella):
                self.assertIn(self.valore(cella), (None, ""))

    def test_la_quantita_del_piano_arriva_nella_sua_cella(self) -> None:
        self.assertEqual(self.valore("C3"), 5)

    def test_il_listino_di_partenza_non_viene_toccato(self) -> None:
        self.assertEqual(hashlib.sha256(self.sorgente.read_bytes()).hexdigest(), self.impronta_prima)

    def test_la_guardia_cella_per_cella_promuove_questa_copia(self) -> None:
        """The test that matters most: after the writer runs, the copy passes the fidelity check."""

        esito = confronta_copia(
            self.sorgente, self.copia,
            colonna_ordine="C", prima_riga=2, quantita={3: 5}, foglio_ordine="Listino",
        )
        self.assertTrue(esito.fedele, esito.esempi_rifiutati)
        # Three allowed differences: the two zeroed rows and the plan quantity.
        self.assertEqual(esito.differenze_ammesse, 3)
        self.assertGreater(esito.celle_confrontate, 0)


class IlWriterNonConosceColonnePerContoSuo(unittest.TestCase):
    """The writer must never guess an order column on its own.

    The writer must never fall back to a hardcoded column — such as
    "betulla in C, larice in D" — and must never run without `--config`: a
    configuration missing the rule for a selected supplier must not compile
    using a column baked into the code, bypassing the registry. The
    registry is meant to be the only definition of how an order is
    written, and that must hold for the program that actually writes it
    too: no rule means it stops and says so, no config means it never
    starts.
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
        """A fallback would write to column D on its own, bypassing the registry."""

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
    """A changed price list must stop the run with the Italian sentence, not a raw Node stack trace."""

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
                # A different file's fingerprint: this is the real case,
                # a price list reloaded after the check ran.
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
        # No Node stack trace and no absolute paths: this is what reaches
        # the user on the page.
        self.assertNotIn("    at ", esito.stderr)
        self.assertNotIn(str(self.cartella), esito.stderr)
        self.assertFalse(any((self.cartella / "uscita").glob("*.xlsx")))


class LIntestazioneDellaColonnaDOrdine(unittest.TestCase):
    """How the header is checked comes from the registry rule, never from the supplier's name.

    The writer must never hardcode a check like "if supplier is cipresso,
    the column must be G and the header must be ORDINE". A supplier whose
    column G header is legitimately blank — confirmed by the user in the
    guided mapping and declared in the registry — must still compile: a
    rule baked into the code disagreeing with the registry's own rule
    would fail compilation for every supplier, producing zero copies.

    What remains is a single question, the same for every supplier: does the
    rule say what text must be there, or does it say the user confirmed that
    cell is blank? If it says neither, nothing gets written.
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
        """A confirmed blank header still compiles: column G, cell G1 empty, writes fine."""

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
        """Same rule, different suppliers: same outcome.

        Proves no name is special-cased: a hardcoded check tied to
        `cipresso` would accept this rule for that name and reject it for
        the others.
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
        """The supplier may have added a title since the mapping was confirmed."""

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
        """A supplier with no header row at all must still compile, like LARICE.

        Requiring every supplier to declare a header expectation would block
        a supplier that genuinely has no header row, trading one hardcoded
        rule for another invented one. What gets checked is only what the
        configuration declares; a supplier that declares no header has
        nothing to verify.
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
    """The `ERRORE_COMPILAZIONE:` marker must only tag sentences the writer itself wrote.

    A `catch` block that tags every message it catches would leak raw
    `ENOENT ... C:\\...` errors with machine paths, and the library's .NET
    resource keys (`Arg_ArgumentOutOfRangeException`), onto the page
    dressed up as an Italian explanation. Technical detail must stay
    unmarked, so it only reaches the console, and the marked line must be a
    sentence a non-technical reader can understand.

    These tests never load the library (the failure happens before that
    point), so each one costs well under a second.
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
        # The path and the ENOENT code live in `cause`, outside the marked
        # sentence: the console sees them, the page doesn't.
        self.assertNotIn("ENOENT", marcate[0])
        self.assertNotIn(str(self.cartella), marcate[0])
        self.assertIn("ENOENT", esito.stderr)

    # Windows-only, declared as such. The failure needs a folder name that
    # Windows rejects: elsewhere `uscita<illegale>` is a valid name, the
    # write succeeds, and the test would fail for lacking the error it
    # expects, failing on every Mac without anything actually being broken.
    # Missing: an OS-independent counterpart that triggers an unexpected
    # failure the same way on any platform.
    @unittest.skipUnless(os.name == "nt", "solo Windows rifiuta questo nome di cartella")
    def test_un_guasto_imprevisto_da_la_frase_generica_e_il_dettaglio_resta_fuori_dalla_marca(self) -> None:
        piano = self.cartella / "final_order_plan.json"
        piano.write_text(json.dumps({"orders": []}), encoding="utf-8")
        # An output folder Windows can't create: the writer has no sentence
        # for this failure, and must not invent one by marking the raw
        # system error as its own.
        esito = self.esegui(piano, self.cartella / 'uscita<illegale>')

        self.assertNotEqual(esito.returncode, 0)
        marcate = self.marcate(esito.stderr)
        self.assertEqual(len(marcate), 1, esito.stderr)
        self.assertIn("guasto imprevisto", marcate[0])
        self.assertIn("Il piano ordini è completo", marcate[0])
        self.assertNotIn(str(self.cartella), marcate[0])
        # The real detail is there, just unmarked.
        self.assertIn("Error", esito.stderr.replace(marcate[0], ""))


class LaDecodificaDellUscitaDelWriter(banco_web.ConsegnaBase):
    """The writer's sentence must survive `subprocess` unmangled.

    Node writes UTF-8. Without telling `subprocess.run` that, Python on
    Windows decodes with cp1252 and "Il listino LARICE è cambiato" would
    reach the page as "Il listino LARICE Ã¨ cambiato". A test that mocked
    `subprocess.run` with a function ignoring its arguments could pass with
    that regression in place, since nothing observed the encoding. This one
    spawns the real process.
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
# 1-bis. Does the target row still carry the product the plan expects?
# ---------------------------------------------------------------------------


def listino_con_ean(percorso: Path, foglio: str = "Listino") -> Path:
    """Test price list for checking that the target row matches the plan.

    | row | A (EAN)             | B (DESCRIZIONE)         | C (ORDINE) |
    |-----|---------------------|-------------------------|------------|
    | 1   | EAN                 | DESCRIZIONE             | ORDINE     |
    | 2   | 8000000000002       | PRODOTTO DUE            | 4          |
    | 3   | 8000000000003       | PRODOTTO TRE            | —          |
    | 4   | 8000000000004       | ESPOSITORE MISTO        | —          |
    | 5   | —                   | SCATOLA REGALO NATALE   | —          |
    | 6   | 8000000000006 (num) | PRODOTTO SEI            | —          |
    | 7   | —                   | CAFFÈ  MISCELA-ORO 250g | —          |

    Row 6 carries the EAN as a number, not text: spreadsheets return it
    that way about half the time, which is why the comparison normalizes
    instead of comparing raw values.
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
    """The end-of-run summary, taken from the marked line.

    `stdout` isn't only the writer's own output: the spreadsheet library
    writes "Inspect result written to file: C:\\..." on every save, so
    parsing all of `stdout` as JSON would choke on that line. The writer
    marks its summary the same way it marks its errors; this picks out
    that one line.
    """

    marca = "RIEPILOGO_COMPILAZIONE:"
    marcate = [riga.strip() for riga in stdout.splitlines() if riga.strip().startswith(marca)]
    if len(marcate) != 1:
        raise AssertionError(f"attesa una riga di riepilogo, trovate {len(marcate)}: {stdout!r}")
    return json.loads(marcate[0][len(marca):])


def esegui_writer(
    node: Path, cartella: Path, piano: dict[str, Any], config: dict[str, Any], uscita: Path,
) -> subprocess.CompletedProcess[str]:
    """Run the real writer, with the plan and config written to disk."""

    piano_path = cartella / "final_order_plan.json"
    piano_path.write_text(json.dumps(piano), encoding="utf-8")
    config_path = cartella / "writer_config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    ambiente = os.environ.copy()
    # As above: deliberately points the dev-runtime cache at nothing.
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
    """Guards against an order landing on the wrong row of the price list.

    Without this check, a `supplier_source_row` mismatch could put an order
    on the wrong product row entirely: the quantity was written to
    `column+row` blindly, with no EAN check anywhere in the writer, and the
    price list's sha256 only defends against a changed file, not a wrong
    row number. A row-level check existed only for Noce, inside
    `app/xls_writer.py`.

    A single real write, with every assertion made against it: the library
    alone takes about ten seconds just to load. The supplier used here is
    deliberately one absent from the writer's hardcoded table, so the same
    run also exercises the display name coming from the configuration.
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
            # EAN matches: this is the right row, so it writes.
            {"supplier": "nuovo_fornitore", "supplier_source_row": 3,
             "supplier_ean": "8000000000003", "supplier_description": "PRODOTTO TRE", "quantity": 5},
            # The display: the plan has no EAN, the price list does. Warning,
            # not an error — this is the case that would block a legitimate
            # order on LARICE.
            {"supplier": "nuovo_fornitore", "supplier_source_row": 4,
             "supplier_ean": "", "supplier_description": "ESPOSITORE MISTO", "quantity": 2},
            # No EAN on either side: falls back to the description, and it
            # doesn't match. Warning, not an error.
            {"supplier": "nuovo_fornitore", "supplier_source_row": 5,
             "supplier_ean": "", "supplier_description": "SCATOLA REGALO", "quantity": 1},
            # Same code written two different ways.
            {"supplier": "nuovo_fornitore", "supplier_source_row": 6,
             "supplier_ean": "8000000000006.0", "supplier_description": "PRODOTTO SEI", "quantity": 3},
            # Description only, matching up to accents and punctuation.
            {"supplier": "nuovo_fornitore", "supplier_source_row": 7,
             "supplier_ean": "", "supplier_description": "caffe miscela oro 250G", "quantity": 7},
            # The plan has no EAN, the price list does, and the description
            # names different goods: a warning is required here, naming both
            # mismatches.
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
                # Either a column letter or a 1-based column number is valid.
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
        """`8000000000006.0` in the plan, `8000000000006` in the price list: still writes.

        Without normalization this row would be an error and the
        supplier would receive nothing, over a stray decimal tail.
        """

        self.assertEqual(self.valore("C6"), 3)

    def test_la_riga_senza_ean_nel_piano_la_conferma_la_descrizione(self) -> None:
        """The display: Noce fails on this case, the writer must not.

        A LARICE display's parent row has an EAN in the price list but not
        in the plan: stopping here would block a legitimate order, so it
        writes.

        No warning either, as long as the description confirms the row.
        A branch that warns without checking the description the registry
        declares would warn, for displays, on a row the price list itself
        confirms.
        """

        self.assertEqual(self.valore("C4"), 2)
        avvisi = self.riepilogo()["warnings"]
        self.assertFalse([avviso for avviso in avvisi if "riga 4" in avviso], avvisi)

    def test_la_descrizione_diversa_si_scrive_lo_stesso_con_un_avviso(self) -> None:
        """The plan's description may have been cleaned up upstream."""

        self.assertEqual(self.valore("C5"), 1)
        avvisi = self.riepilogo()["warnings"]
        self.assertIn(
            "La riga 5 del listino D'Alessio & Figli S.r.l. è descritta «SCATOLA REGALO NATALE» "
            "e il piano dice «SCATOLA REGALO»: la quantità è stata scritta lo stesso, "
            "controlla che sia la riga giusta.",
            avvisi,
        )

    def test_una_descrizione_uguale_a_meno_di_accenti_non_avvisa_nessuno(self) -> None:
        """"caffe miscela oro 250G" and "CAFFÈ  MISCELA-ORO 250g" are the same row.

        A literal comparison would warn on every EAN-less row, and the
        warning list would stop being worth reading.
        """

        self.assertEqual(self.valore("C7"), 7)
        self.assertEqual(len(self.riepilogo()["warnings"]), 2, self.riepilogo()["warnings"])

    def test_l_ean_solo_nel_listino_con_la_descrizione_che_smentisce_avvisa_di_tutto(self) -> None:
        """Both mismatches reported together: they're two different signals about the same row."""

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
        """The numbers the user should be able to read at the end of a run."""

        riepilogo = self.riepilogo()
        self.assertEqual(riepilogo["verified_rows"], 4)
        self.assertEqual(riepilogo["unverifiable_rows"], 2)
        self.assertEqual(riepilogo["written_rows"], 6)
        # Verified and unverifiable rows add up to the plan's rows.
        self.assertEqual(
            riepilogo["verified_rows"] + riepilogo["unverifiable_rows"],
            riepilogo["written_rows"],
        )

    def test_il_nome_leggibile_arriva_dalla_configurazione(self) -> None:
        """Without `display_name`, this summary would show `NUOVO_FORNITORE` instead."""

        riepilogo = self.riepilogo()
        self.assertEqual(riepilogo["supplier"], "nuovo_fornitore")
        self.assertEqual(riepilogo["supplier_name"], self.NOME_LEGGIBILE)

    def test_il_nome_del_file_porta_il_nome_leggibile_ripulito(self) -> None:
        """The supplier's display name ends up in the delivered file name.

        `D'Alessio & Figli S.r.l.` as-is would produce a file name Windows
        can't write: characters a file name can't hold become an
        underscore, while the sentence shown to the user keeps the
        declared display name.
        """

        self.assertEqual(self.copia.name, "ORDINE_D_ALESSIO_FIGLI_S_R_L_listino_nuovo.xlsx")
        self.assertFalse(list(self.uscita.glob("*NUOVO_FORNITORE*")))
        self.assertEqual(Path(self.riepilogo()["destination"]), self.copia)


class LaPuliziaDelleCopieVecchie(unittest.TestCase):
    """A previous run's order file must not linger in the output folder.

    Cleanup that only deletes the exact name the writer would build for
    the current run — a name that comes from `display_name` — would miss a
    supplier renamed since the previous compilation: the old copy keeps
    its old name, survives the cleanup, and ships alongside the new one —
    two orders for the same supplier, with different numbers, and nothing
    flagging it. The output folder is always freshly created, so this
    failure mode can't occur with the current layout; this test keeps it
    that way.

    What must stay untouched is a delivery already renamed by hand (e.g.
    `Ordine BETULLINO — 14 agosto 2026.xlsx`): that's a previous
    compilation, not stale output to discard.
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
        """Two suppliers on the same price list, one ordered and one not.

        Cleanup for the supplier that is not ordered targets the source
        price list, which here is the same file: without protecting fresh
        copies, it could delete the order just written, the writer would
        report success for a file that no longer exists, and any failure
        downstream would point at the wrong place.
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
                # Configured, not ordered, and with the same price list.
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
        """Which write procedure applies comes from the registry rule, never from the supplier's name.

        The writer must never special-case the name "noce".
        What actually decides the write procedure is the configuration's
        declared procedure — the same field `app/server.py` reads as
        `procedura_di_scrittura`.

        This test swaps the two names on purpose: "noce" with no declared
        procedure compiles normally, while a differently-named supplier
        that does declare one is skipped. A hardcoded name check could
        never pass this test.
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
        # Rows written by a different procedure are still counted, to report them.
        self.assertEqual(riepilogo["noce_lines"], 1)
        self.assertEqual(
            [voce["supplier"] for voce in riepilogo["supplier_copies"]], ["noce"],
        )

    def test_la_pulizia_non_cancella_per_omonimia(self) -> None:
        """`listino_nuovo` is a filename suffix of `mio_listino_nuovo`.

        The cleanup recognized stale copies by matching a filename suffix,
        and one supplier's price-list name can be a suffix of another
        supplier's. Cleaning up the first could take the second's order
        with it. The longest matching suffix wins.
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
                # Noce doesn't go through this writer — its document is
                # compiled by the in-place service — so nothing else should
                # touch its copy here: this is the case where the name
                # collision would show up.
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
                # Noce's rule is present and declares its own procedure,
                # matching what the local service actually writes into
                # `writer_config.json`. Without it, this test would describe
                # a configuration that doesn't exist — and would be the
                # thing propping up a hardcoded `supplier === "noce"` check
                # in this writer.
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
        """Two different supplier names that resolve to the same order file.

        `nomePerIlFile` collapses anything that isn't a letter or digit
        into `_`: "Sapori & Co." and "Sapori Co" produce the same name.
        Delivering only one would mean sending one supplier's goods to
        another.
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
        # Two price lists with the same file name in two different folders,
        # and two supplier names that collapse to the same one: the order
        # document would be identical for both.
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
    """A mismatched EAN must produce no copy at all for that supplier.

    Plus its control case: with no `verify` block, the same mismatched row
    is written the way it always was. An old configuration must not stop
    working just because the writer gained a new check.
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
        # Row 3 of the price list carries 8000000000003; the plan expects a different one.
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
        # No deliverable copy: that's the whole point of the check.
        self.assertFalse(
            list(self.uscita.glob("*.xlsx")) if self.uscita.is_dir() else [],
            "una riga sbagliata non deve lasciare sul disco un ordine da spedire",
        )
        # And no Node stack trace reaches the user.
        self.assertNotIn("    at ", esito.stderr)

    def piano_di_riga_3(self, descrizione: str = "PRODOTTO TRE") -> dict[str, Any]:
        """An order on row 3, with the EAN that row actually carries."""

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
        """The price list is fine: it's the declared column that's wrong.

        With `ean_column` pointed at the description column, every row
        would look mismatched. A message reading "non è più la riga su cui è
        stato costruito l'ordine" (not the row the order was built on) would
        send the reader to look for a fault in the price list, which has
        none. Here the description matches, so the row is confirmed correct
        and the message can say so.
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
        """Without a description to compare, the message must name both possibilities.

        Finding the plan's EAN in a different column is not enough to
        conclude the declared column is wrong: a supplier that repeats the
        item code in another column can also put it on a row that no
        longer has that product. A message that claimed the column was
        wrong could lead someone to repoint `ean_column` at that code,
        permanently disabling this check. With no description available to
        confirm either way, both possibilities must be stated.
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
        """The row carries the plan's EAN in another column, but it's different goods.

        A supplier that repeats the item code across columns can put it on
        a row for the wrong product: the declared column is fine, and it's
        the row that's wrong. The message must not point at the
        configuration.
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
        """Control case: same mismatched row, no verify block configured.

        Confirms two things: an old configuration keeps working, and the
        test above is really measuring the check, not an incidental
        defect of the test price list.
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
        # Nothing to compare against: the row counts as unverifiable.
        self.assertEqual(riepilogo["verified_rows"], 0)
        self.assertEqual(riepilogo["unverifiable_rows"], 1)
        self.assertEqual(riepilogo["warnings"], [])


class IlNomeLeggibileNelleFrasi(unittest.TestCase):
    """The display name must reach the user in error sentences too, not just in file names.

    Both tests here stop at the configuration step, before the spreadsheet
    library loads, so each one costs well under a second.
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
        """Without it, the user would read `NUOVO_FORNITORE`, underscore included."""

        # The rule doesn't declare the sheet: this fails immediately, and
        # the message must already use the supplier's display name.
        marcata = self._marcata({"order_column": "C", "display_name": "D'Alessio & Figli S.r.l."})

        self.assertIn("Manca il foglio verificato per D'Alessio & Figli S.r.l.", marcata)
        self.assertNotIn("NUOVO_FORNITORE", marcata)

    def test_senza_display_name_resta_il_ripiego_di_oggi(self) -> None:
        """A configuration with no declared name keeps today's fallback behavior."""

        marcata = self._marcata({"order_column": "C"})

        self.assertIn("Manca il foglio verificato per NUOVO_FORNITORE", marcata)


# ---------------------------------------------------------------------------
# 2. The cell-by-cell fidelity guard, in isolation
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
        # The test price list has 9 non-empty cells: the comparison must
        # have seen all of them (unformatted empty cells carry no
        # information and aren't counted).
        self.assertGreaterEqual(esito.celle_confrontate, 9)

    def test_una_differenza_fuori_dalla_colonna_d_ordine_viene_rifiutata(self) -> None:
        """An EAN in the copy that doesn't match the source price list."""

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
        """The cell is right, the number isn't: still a wrong order."""

        esito = self.confronta(self.copia_con({"C3": 7}), quantita={3: 6})
        self.assertFalse(esito.fedele)
        self.assertEqual(esito.quante_rifiutate, 1)

    def test_l_azzeramento_di_una_quantita_preesistente_e_ammesso(self) -> None:
        esito = self.confronta(self.copia_con({"C2": None}))
        self.assertTrue(esito.fedele, esito.esempi_rifiutati)
        self.assertEqual(esito.differenze_ammesse, 1)

    def test_un_titolo_di_sezione_cancellato_viene_rifiutato(self) -> None:
        """The order column can hold more than quantities — a section title too."""

        esito = self.confronta(self.copia_con({"C4": None}))
        self.assertFalse(esito.fedele)
        self.assertEqual(esito.quante_rifiutate, 1)
        self.assertIn("SEZIONE SOLARI", frase_di_rifiuto("LARICE", esito))

    def test_una_cella_svuotata_fuori_dalla_colonna_d_ordine_viene_rifiutata(self) -> None:
        """Zeroing is only allowed inside the order column.

        An EAN is made of digits: if "was a number, now empty" were
        tolerated in every column, a deleted EAN would pass as a
        legitimate zeroing. That distinction is what makes this a guard
        instead of a sieve.
        """

        esito = self.confronta(self.copia_con({"A2": None}))
        self.assertFalse(esito.fedele)
        self.assertIn("A2", frase_di_rifiuto("LARICE", esito))

    def test_l_intestazione_della_colonna_d_ordine_non_si_puo_toccare(self) -> None:
        """No difference is allowed above the first data row."""

        esito = self.confronta(self.copia_con({"C1": None}))
        self.assertFalse(esito.fedele)

    def test_sopra_la_prima_riga_di_dati_nemmeno_un_azzeramento_e_lecito(self) -> None:
        """Row 2 here is a header, not data: whatever it holds must stay.

        `C2` holds the number 4. Inside the data area, clearing it would be
        the legitimate zeroing of a pre-existing quantity; above the first
        data row it is just a price-list cell disappearing.
        """

        esito = confronta_copia(
            self.originale, self.copia_con({"C2": None}),
            colonna_ordine="C", prima_riga=3, quantita={}, foglio_ordine="Listino",
        )
        self.assertFalse(esito.fedele)

    def test_la_cella_vuota_e_la_stringa_vuota_sono_la_stessa_cosa(self) -> None:
        """An empty cell and an empty string look identical in Excel; treating them as different would block a valid order.

        The empty shared string has to be built by hand: openpyxl never
        generates one (it writes a cell with nothing in it, which reads
        back as `None`), so `A4 = ""` on an openpyxl-built file would only
        ever compare `None` against `None`, never exercising the tolerance
        this test is meant to protect. On the real LARICE price list this
        tolerance covers 35 cells per run; without it, that supplier's
        largest order would stop being delivered at all.
        """

        originale = listino_con_stringhe_vuote(self.cartella / "con_vuote.xlsx")
        copia = self.cartella / "riscritta.xlsx"
        libro = load_workbook(originale)  # cells A2/A5 come back as ""...
        try:
            libro.save(copia)  # ...and the rewritten file has them as empty cells
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
        """The supplier reads the number as displayed, not the value stored in the file.

        `C2` stays 4 in memory, but with number format `0` a price like
        1.75 would display as "2". An earlier version of this guard let
        that kind of change through unnoticed.
        """

        esito = self.confronta(self.copia_con_formato({}, {"C2": "0.00"}))
        self.assertFalse(esito.fedele)
        frase = frase_di_rifiuto("LARICE", esito)
        self.assertIn("formato numerico", frase)
        self.assertIn("C2", frase)

    def test_il_formato_che_nasconde_la_quantita_scritta_viene_rifiutato(self) -> None:
        """`;;;` on the plan's quantity: the program believes it ordered
        6 cartons while the supplier sees an empty cell."""

        esito = self.confronta(
            self.copia_con_formato({"C3": 6}, {"C3": ";;;"}), quantita={3: 6}
        )
        self.assertFalse(esito.fedele)

    def test_un_formato_diverso_su_una_cella_vuota_non_ferma_niente(self) -> None:
        """No number format shows anything on an empty cell: rejecting it
        there would reject a valid order over formatting no one can see."""

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
        """Without knowing where the order column is, a legitimate change can't be told apart from a defect."""

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
    """The guard must check not just "nothing extra" but "everything required is there".

    A guard that only acts where the copy differs from the price list
    misses a quantity the writer failed to write: that cell stays exactly
    as it was, no branch ever catches it, and the copy comes back
    "faithful" with one order row silently missing:

        full copy               fedele=True  ammesse=3  rifiutate=0
        copy missing row 6      fedele=True  ammesse=2  rifiutate=0

    The data needed to catch this was already available: `quantita` is
    the list of rows that were supposed to be written.
    """

    def setUp(self) -> None:
        temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(temporanea.cleanup)
        self.cartella = Path(temporanea.name)
        self.originale = listino_semplice(
            self.cartella / "originale.xlsx", "Listino",
            [
                ["EAN", "DESCRIZIONE", "ORDINE"],
                # C2 carries the quantity from a previous run's order.
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

    # -- the defect this guards against --------------------------------------

    def test_una_riga_del_piano_che_non_arriva_nella_copia_non_e_fedele(self) -> None:
        """The plan asks for three rows, the copy has two: it must not be delivered.

        The copy has nothing extra — no cell changed out of place —
        which is exactly why this defect went unnoticed for so long: the
        guard only ever checked for that.
        """

        esito = self.confronta(
            {"C2": None, "C3": 5, "C4": 6},  # row 6 got dropped by the writer
            quantita={3: 5, 4: 6, 6: 7},
        )

        self.assertFalse(esito.fedele)
        self.assertEqual(esito.righe_mancanti, [6])
        self.assertEqual(esito.righe_richieste, 3)
        # No extra cell: the whole defect shows up in the other measure.
        self.assertEqual(esito.quante_rifiutate, 0)
        frase = frase_di_rifiuto("LARICE", esito)
        self.assertIn("riga 6", frase)
        self.assertIn("delle 3 righe d'ordine richieste", frase)
        self.assertIn("La copia non viene consegnata", frase)
        # And the price list isn't blamed for changed cells: that would
        # point the search for the defect in the wrong direction.
        self.assertNotIn("celle sono diverse", frase)
        self.assertNotIn("cella è diversa", frase)

    # -- must not break what already worked -----------------------------------

    def test_la_copia_completa_resta_fedele(self) -> None:
        """Control case: the same three rows, all written."""

        esito = self.confronta(
            {"C2": None, "C3": 5, "C4": 6, "C6": 7},
            quantita={3: 5, 4: 6, 6: 7},
        )

        self.assertTrue(esito.fedele, esito.esempi_rifiutati)
        self.assertEqual(esito.righe_mancanti, [])
        self.assertEqual(esito.righe_richieste, 3)
        # The three plan quantities plus the previous run's zeroed row.
        self.assertEqual(esito.differenze_ammesse, 4)

    def test_l_azzeramento_della_settimana_prima_non_e_una_riga_richiesta(self) -> None:
        """`C2` is cleared because the plan asks for nothing on that row.

        This is why expected rows are counted from `quantita` and not from
        the allowed differences: a zeroing is an allowed difference the
        plan never asked for, and counting "as many differences as rows"
        would either double-count it or mistake it for a written row.
        """

        esito = self.confronta({"C2": None, "C3": 5}, quantita={3: 5})

        self.assertTrue(esito.fedele, esito.esempi_rifiutati)
        self.assertEqual(esito.righe_richieste, 1)
        self.assertEqual(esito.righe_mancanti, [])
        self.assertEqual(esito.differenze_ammesse, 2)

    def test_una_quantita_gia_giusta_nel_listino_non_e_una_riga_mancante(self) -> None:
        """The plan asks for 4 on row 2, and the price list already shows 4.

        The copy is identical to the original and correct: the supplier
        reads the 4 cartons the plan asked for. A rule of "every plan row
        must have produced a difference" would reject this perfectly valid
        copy, and a supplier reordering the same quantity every week would
        never receive an order. What matters is the value the copy carries,
        not how much it changed.
        """

        esito = self.confronta({}, quantita={2: 4})

        self.assertTrue(esito.fedele, esito.esempi_rifiutati)
        self.assertEqual(esito.differenze_ammesse, 0)
        self.assertEqual(esito.righe_mancanti, [])

    # -- che cosa legge l'utente --------------------------------------------

    def test_i_due_guasti_si_dicono_diversi(self) -> None:
        """An extra cell and a missing row: two separate messages, not one."""

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
        """A page-long list would go unread: show the first cases and a total."""

        esito = self.confronta({}, quantita={2: 9, 3: 5, 4: 6, 6: 7})

        self.assertEqual(esito.righe_mancanti, [2, 3, 4, 6])
        frase = frase_di_rifiuto("LARICE", esito)
        self.assertIn("mancano 4 delle 4 righe d'ordine richieste", frase)
        self.assertIn("righe 2, 3 e 4, e un'altra", frase)

    def test_la_frase_delle_celle_di_troppo_e_quella_di_sempre(self) -> None:
        """The message for changed cells must stay exactly as before.

        The sentence is compared in full, not piece by piece: it's the
        only way to catch an extra word slipping into a message the user
        already recognizes.
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
    """The guard's promises about secondary sheets, exercised on a workbook that has two.

    Every earlier fixture for the guard used a single-sheet workbook, so
    the code path handling secondary sheets was never actually exercised:
    a broken "the guard checks every sheet" or "the order-column tolerance
    only applies to the order sheet" could pass the suite untested. These
    tests exist for that, and for the supplier who would otherwise receive
    wrong payment terms from an "optimization" that skips sheets with no
    order column.
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
            # C3 holds a number: clearing it looks exactly like zeroing a
            # pre-existing quantity, but this isn't the order sheet.
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
        """"60 GG" becoming "30 GG" is a wrong payment term."""

        esito = self.confronta(self.copia_con("Condizioni", {"B1": "30 GG"}))
        self.assertFalse(esito.fedele)
        frase = frase_di_rifiuto("LARICE", esito)
        self.assertIn("Condizioni", frase)
        self.assertIn("B1", frase)
        self.assertIn("60 GG", frase)

    def test_la_tolleranza_della_colonna_d_ordine_non_vale_sui_fogli_secondari(self) -> None:
        """Column C exists on every sheet; the order column is only on "Listino".

        The rule `confronta_copia`'s docstring declares: on other sheets no
        difference is allowed, not even in the column with the same
        letter, not even one that looks like a legitimate zeroing.
        """

        esito = self.confronta(self.copia_con("Condizioni", {"C3": None}))
        self.assertFalse(esito.fedele)
        self.assertIn("Condizioni", frase_di_rifiuto("LARICE", esito))

    def test_l_azzeramento_resta_lecito_sul_foglio_dell_ordine(self) -> None:
        """Control case: the same difference, on the right sheet."""

        esito = self.confronta(self.copia_con("Listino", {"C2": None}))
        self.assertTrue(esito.fedele, esito.esempi_rifiutati)
        self.assertEqual(esito.differenze_ammesse, 1)


class LaGuardiaSulleFormeLecite(unittest.TestCase):
    """A valid but uncommon `.xlsx` shape must not make a perfect copy get rejected.

    Two shapes covered here, both permitted by OOXML: a `<dimension>`
    narrower than the actual data (openpyxl's `read_only` mode trusts it
    and reads a truncated price list), and rows written out of order in
    the file (position-based pairing would compare the original's row 5
    against the copy's row 2). In both cases the guard would reject a
    perfect copy by blaming innocent cells, and the supplier would
    never receive their order.
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
        """The library's own copy carries no `<dimension>`: if the guard
        trusted the original's declared range, it would read a truncated
        price list and accuse the copy of inventing cells beyond it."""

        originale = xlsx_a_mano(self.cartella / "originale.xlsx", self.foglio([1, 2, 3, 4, 5], dimension="A1:B3"))
        copia = xlsx_a_mano(self.cartella / "copia.xlsx", self.foglio([1, 2, 3, 4, 5]))
        esito = confronta_copia(
            originale, copia,
            colonna_ordine="C", prima_riga=2, quantita={}, foglio_ordine="Listino",
        )
        self.assertTrue(esito.fedele, esito.esempi_rifiutati)

    def test_le_righe_scritte_fuori_ordine_si_confrontano_per_numero_di_riga(self) -> None:
        """Every `<row>` declares its own number: file order doesn't matter.
        The library rewrites rows in ascending order, and the copy stays perfect."""

        originale = xlsx_a_mano(self.cartella / "originale.xlsx", self.foglio([1, 5, 3, 2, 4]))
        copia = xlsx_a_mano(self.cartella / "copia.xlsx", self.foglio([1, 2, 3, 4, 5]))
        esito = confronta_copia(
            originale, copia,
            colonna_ordine="C", prima_riga=2, quantita={}, foglio_ordine="Listino",
        )
        self.assertTrue(esito.fedele, esito.esempi_rifiutati)

    def test_le_quantita_si_appaiano_al_numero_di_riga_vero_anche_se_il_foglio_comincia_dopo(self) -> None:
        """A sheet whose first rows don't exist at all (the first cell is on
        row 3): `iter_rows` starts from the first real row, and counting by
        position instead would shift the plan's quantities onto the
        wrong rows — row 4 of the plan would become position 2, the
        tolerance would land elsewhere, and a correct copy would be rejected."""

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
        """Control case: comparing by true row number isn't a tolerance —
        a genuinely changed cell still gets caught."""

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
# 3. The guard inside compilation, where the user actually sees it
# ---------------------------------------------------------------------------


class ScrittoreCheNonScrive(banco_web.ScrittoreFinto):
    """Delivers the price list as-is: the order column stays empty.

    This reproduces the plainest possible failure to write anything: a
    bare `shutil.copy2`. The copy is put back into this state after the
    base class does its own work, so this test says the same thing
    regardless of what the base class writes in the future.
    """

    def __call__(self, plan_path: Path, destinazione: Path) -> Any:
        generati, prodotte, avvisi = super().__call__(plan_path, destinazione)
        for _fornitore, sorgente, copia in prodotte:
            shutil.copy2(sorgente, copia)
        return generati, prodotte, avvisi


class ScrittoreCheGuasta(banco_web.ScrittoreFinto):
    """Like the fake writer, but corrupts one cell of a supplier's copy."""

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
        """The difference is in column `A`, not in the order column (`D`)."""

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
        # No half-delivered copy: the document simply doesn't exist.
        cartella = self.orders_dir / esito["cartella"]
        self.assertFalse(list(cartella.glob("*.xlsx")), list(cartella.iterdir()))
        self.assertTrue((cartella / "final_order_plan.json").is_file())
        # And the warning also lands in the audit log, not just in the response.
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
        """A copy missing the plan's quantities entirely, where the user actually sees it.

        A price-list copy with none of the plan's quantities written in:
        compilation would otherwise report success, rename the copy, and
        put it in the zip and the order history — an order delivered to
        the supplier with zero rows actually ordered.
        """

        esito = self.compila(ScrittoreCheNonScrive({"larice": self.listini["larice"]}))

        self.assertEqual(esito["status"], "PLAN_READY")
        self.assertEqual(len(esito["writerIssues"]), 1)
        avviso = esito["writerIssues"][0]
        self.assertIn("LARICE", avviso)
        self.assertIn("riga d'ordine richiesta", avviso)
        self.assertIn("riga 10", avviso)
        self.assertIn(avviso, esito["message"])
        # The half-written copy doesn't stay on disk, same as other fidelity failures.
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
# 3-bis. End-of-run summary, as read by the service
# ---------------------------------------------------------------------------


class IlPrezzoDiUnOfferta(unittest.TestCase):
    """`offer_pricing`: both directions of the calculation, not just one.

    The branch that derives the per-piece price from the order price had
    no coverage, unlike its symmetric counterpart. It's the branch that
    matters for an offer declaring only the carton total, and a wrong
    factor there skews the supplier comparison, which is always done
    on the per-piece price.
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
        """Safe fallback: a carton of one costs as much as the carton."""

        self.assertEqual(
            self.prezzo(orderUnitPriceNet=9.0),
            {"factor": 1.0, "unitPriceNet": 9.0, "orderUnitPriceNet": 9.0},
        )

    def test_senza_nessun_prezzo_non_si_inventa_niente(self) -> None:
        self.assertIsNone(self.prezzo(quantityFactor=6))
        self.assertIsNone(self.prezzo(orderUnitPriceNet=-1.0, quantityFactor=6))


class LImprontaDellArticoloConfermato(banco_web.ConsegnaBase):
    """What was confirmed, not just that something was.

    The consumer of this fingerprint — `_ripulisci_stato`, which clears a
    confirmation after a recompute if the item changed — has its own tests,
    but all of them build the state by hand, with the fingerprint
    already in place. Nothing tested the code that actually writes it: that
    line could be deleted and the rest of the suite would stay green.
    Without it, every confirmation would expire on every recompute, and the
    user would have to re-check every item each week.
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
    """A rename that failed must not be a fleeting notice that disappears.

    `os.rename` on Windows fails if something else has the file open —
    Excel, OneDrive, antivirus. The document is still correct and
    deliverable, just under its unrenamed name. Reporting this only inside
    `message` would leave the panel green with its primary button, and
    reloading the page would lose the notice entirely, because the
    order-list entry wouldn't carry the audit's warnings.
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
        # The document exists and is deliverable: the warning isn't a rejection.
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
    """Who last wrote the state matters, and the message must say so.

    `compile` saves state before preparing the price lists. If
    something fails after that point, the open sheet is left one version
    behind. Rejecting the next save with "another comparator sheet has
    saved since you opened this one" would send the user looking for a
    colleague who doesn't exist, and the "retry" button couldn't work
    either, since it saves first, before retrying.
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
        # The open sheet still holds the previous version.
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
    """The last link in the chain: the name the document actually ships with.

    The writer builds its intermediate copy using the registry's name.
    A step later, `rinomina_listini` must keep that display name rather
    than rebuild it from the technical identifier: getting this wrong
    would make the document attached to the email end up named
    "Ordine NUOVO_FORNITORE_1 — 14 agosto 2026.xlsx", underscores included —
    exactly the raw identifier the display-name work was meant to hide.
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
    """Two plan rows pointing at the same price-list row must not silently sum into one cell.

    Both compilers — the Node writer and `xls_writer` for Noce — sum the
    quantities of two plan rows that point to the same price-list row, and
    for two management-software items that are the same supplier item, that
    sum is correct.

    It isn't correct between a carton and a display, and the program can
    build that case on its own: a display's offer carries the row number of
    its parent carton, while the parent row stays independently
    orderable. Four cartons plus six displays would turn into a single
    cell reading `10`, and the supplier would read ten of something nobody
    ordered.
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
        """The other half of the rule: here summing is exactly what's wanted.

        Two management-software codes that are the same supplier item order
        the same goods in the same unit: refusing this would deny the user
        a legitimate order.
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
        """The guard end to end: from the comparison data to compilation refusing to run."""

        confronto = json.loads(self.review_path.read_text(encoding="utf-8"))
        espositore = next(voce for voce in confronto["products"] if voce["id"] == "display-solbao-96")
        # The display carries the row number of its parent carton: that's
        # what `display_offer` actually writes.
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
        # And no half-finished delivery folder is left behind.
        self.assertFalse(list(self.orders_dir.glob("*")) if self.orders_dir.is_dir() else [])


class IlRiepilogoDelWriter(banco_web.ConsegnaBase):
    """The writer declares what it produced, and the service must trust that declaration.

    The writer builds the copy's name from the `display_name` the write
    rule carries from the registry — "Sapori & Co." becomes
    `ORDINE_SAPORI_CO_<listino>.xlsx`. The service must read that
    declared name rather than recompute it from the technical identifier:
    for a supplier whose uppercase identifier happens to equal its name
    the two names would coincide by chance anyway, but for a learned
    supplier with a real display name, recomputing would fail compilation
    with the correct copy sitting right next to it, and the user would
    read "the writer didn't create the expected copy".

    The other half of this summary is the rows the writer wrote without
    being able to verify they were the right ones: those messages used
    to end up only on the program's console, where nobody looks.
    """

    def prepara(self, fornitore: str, listino: Path) -> tuple[Path, Path]:
        """Set up the write configuration, compilation folder, and plan."""

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
        """Fake Node: creates the requested document and prints the summary.

        `stdout` deliberately also carries the spreadsheet library's own
        line, which is exactly why the summary is marked instead of being
        assumed to be everything the writer prints.
        """

        def esecuzione_finta(command: list[str], **_altro: Any) -> Any:
            destinazione = Path(command[command.index("--output-dir") + 1])
            if crea:
                banco_web.listino_finto(destinazione / crea)
            uscita = "Inspect result written to file: C:\\temp\\copia.inspect.ndjson\n"
            if riepilogo is not None:
                uscita += "RIEPILOGO_COMPILAZIONE: " + json.dumps(riepilogo) + "\n"
                # The indented, human-readable block the real writer prints
                # after the marked one: must not confuse the reader.
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
        """A learned supplier: identifier `sapori`, display name "Sapori & Co."."""

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
        # The name the service would have recomputed doesn't exist on disk:
        # that's exactly the difference this test protects.
        self.assertFalse((cartella / "ORDINE_SAPORI_listino_sapori.xlsx").exists())

    def test_un_fornitore_che_il_riepilogo_non_nomina_ferma_la_compilazione(self) -> None:
        """The writer's summary never names this supplier's copy.

        A document with the expected name exists on disk too, and that
        isn't enough: taking it anyway would deliver a file the writer
        never declared it wrote in this run.
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
        """`skipped` is how the writer says "configured, but not ordered"."""

        piano, cartella = self.prepara("larice", self.listini["larice"])
        riepilogo = self.copia_dichiarata(cartella, "ORDINE_LARICE_listino_larice.xlsx", "larice")
        riepilogo["supplier_copies"][0]["destination"] = None
        riepilogo["supplier_copies"][0]["skipped"] = True

        with self.assertRaises(ValueError) as errore:
            self.esegui(piano, cartella, crea="ORDINE_LARICE_listino_larice.xlsx", riepilogo=riepilogo)

        self.assertIn("non dichiara nessuna copia", str(errore.exception))

    def test_una_copia_dichiarata_fuori_dalla_cartella_non_si_consegna(self) -> None:
        """The delivery is the dated folder: the zip and the listing are both built from it."""

        piano, cartella = self.prepara("larice", self.listini["larice"])
        estranea = self.root / "ORDINE_LARICE_listino_larice.xlsx"
        banco_web.listino_finto(estranea)
        riepilogo = self.copia_dichiarata(cartella, "x.xlsx", "larice")
        riepilogo["supplier_copies"][0]["destination"] = str(estranea)

        with self.assertRaises(ValueError) as errore:
            self.esegui(piano, cartella, crea=None, riepilogo=riepilogo)

        self.assertIn("fuori dalla cartella della compilazione", str(errore.exception))

    def test_le_righe_scritte_senza_verifica_si_contano(self) -> None:
        """No per-row message, but three rows that nothing verified."""

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
        """The writer's warning already names the row: counting it again would be noise."""

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
        """Writer warnings must reach the user, not stay lost on the console while the copy ships silently."""

        detta = (
            "La riga 10 del listino LARICE è descritta «ALTRO PRODOTTO» e il piano dice "
            "«PRODOTTO STANDARD»: la quantità è stata scritta lo stesso, controlla che sia "
            "la riga giusta."
        )
        scrittore = banco_web.ScrittoreFinto({"larice": self.listini["larice"]}, avvisi=(detta,))
        with mock.patch.object(self.store, "writer_configuration_issues", return_value=[]), \
                mock.patch.object(self.store, "run_writer", scrittore):
            esito = self.store.compile(self.snapshot(2, "larice"))

        # The copy is delivered: the warning isn't a rejection.
        self.assertEqual(esito["status"], "FILES_READY")
        self.assertEqual(esito["writerIssues"], [detta])
        # The message carries only the count, not the text: the page
        # already gets the full list from `writerIssues`, and repeating it
        # here would show it twice. The count stays because the compilation
        # history shows this message, not the list.
        self.assertIn("Su un listino preparato c'è una segnalazione da leggere.", esito["message"])
        self.assertNotIn(detta, esito["message"])
        cartella = self.orders_dir / esito["cartella"]
        self.assertEqual(len(list(cartella.glob("*.xlsx"))), 1)
        # And they're also written to the audit log, which is the record
        # that outlives the page.
        audit = json.loads((cartella / "compilazione.json").read_text(encoding="utf-8"))
        self.assertIn(detta, audit["avvisi"])

    def test_l_avviso_del_writer_non_diventa_il_motivo_di_una_copia_mai_creata(self) -> None:
        """"The quantity was written anyway" showing up inside "nothing was created".

        When every copy is discarded, the reason given must be the
        discarded copies. Writer warnings talk about rows that were
        written: placed inside that message, the two would contradict
        each other.
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
        # The full list carries both, which is where they belong.
        self.assertIn(detta, esito["writerIssues"])
        self.assertEqual(len(esito["writerIssues"]), 2, esito["writerIssues"])

    def test_l_avviso_del_writer_sta_davanti_a_quello_della_copia_scartata(self) -> None:
        """Two separate entries: one about the delivered copy, one about the discarded one."""

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
# 4. What the user reads when the writer stops
# ---------------------------------------------------------------------------


class IlLanciatoreConIlSoloNode(unittest.TestCase):
    """The writer needs nothing more than Node.

    `scripts/lib/xlsx_in_posizione.mjs` patches only the sheet XML with
    the standard library, rather than rebuilding the whole file with a
    .NET-in-WebAssembly library. The launcher must not look for a
    `node_modules`, nor write one into the configuration: a declared but
    missing `node_modules` would stop compilation over a folder the
    writer doesn't actually need.
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
        """None of this raw Node output must reach the user's page."""

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
