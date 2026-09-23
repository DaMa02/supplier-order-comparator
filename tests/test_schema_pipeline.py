from __future__ import annotations

import hashlib
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
TESTS = Path(__file__).resolve().parent
ADAPTERS = SKILL_ROOT / "references" / "adapters.json"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from prepare_sources import read_noce
from openpyxl.utils import column_index_from_string

import inspect_sources
import registro

from prepare_manifest_sources import (
    column_number,
    mapped_rows,
    read_mapped_csv_supplier,
    read_mapped_xlsx_supplier,
)

# Lo scrittore di file .xls sta gia' nel collaudo del lettore: costruisce un
# contenitore OLE2 vero con dentro record BIFF8 veri.  Riusarlo evita di avere
# due scrittori diversi che possono divergere.
import test_xls_reader as scrittore_xls


def save_workbook(path: Path, rows: list[list[object]], sheet_name: str = "Sheet1") -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_name
    for row in rows:
        sheet.append(row)
    workbook.save(path)
    workbook.close()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def impronta_file(path: Path) -> tuple[int, int, str]:
    """Dimensione, data di modifica e contenuto di un file.

    Se cambia anche uno solo dei tre, il file è stato toccato: riscriverlo con
    gli stessi identici byte ne cambia comunque la data.
    """
    stato = path.stat()
    return stato.st_size, stato.st_mtime_ns, sha256(path)


def esegui_script(name: str, *arguments: object) -> subprocess.CompletedProcess[bytes]:
    """Esegue uno script della pipeline come lo esegue l'utente."""
    return subprocess.run(
        [sys.executable, str(SCRIPTS / name), *(str(value) for value in arguments)],
        cwd=SKILL_ROOT,
        capture_output=True,
        check=False,
    )


def esegui_script_riuscito(name: str, *arguments: object) -> subprocess.CompletedProcess[bytes]:
    """Come esegui_script, ma un fallimento diventa subito leggibile."""
    result = esegui_script(name, *arguments)
    if result.returncode != 0:
        raise AssertionError(
            f"{name} non è riuscito.\n"
            f"stdout:\n{result.stdout.decode('utf-8', errors='replace')}\n"
            f"stderr:\n{result.stderr.decode('utf-8', errors='replace')}"
        )
    return result


class SchemaPipelineTests(unittest.TestCase):
    maxDiff = None

    def run_script(self, name: str, *arguments: object) -> subprocess.CompletedProcess[bytes]:
        return esegui_script(name, *arguments)

    def assert_success(self, result: subprocess.CompletedProcess[bytes]) -> None:
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")
        self.assertEqual(result.returncode, 0, msg=f"stdout:\n{stdout}\nstderr:\n{stderr}")

    def test_inspector_identifies_known_betulla_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "listino_betulla.xlsx"
            output = root / "profiles.json"
            save_workbook(
                source,
                [
                    ["EAN", "CodArt", "ORDINE", "Descr.Commerciale", "PzCt", "Cessione", "Pedana", "Iva", "TOTALI"],
                    ["8000000000001", "C-001", None, "Prodotto Alfa", 6, 1.25, 60, 22, 0],
                    ["8000000000002", "C-002", None, "Prodotto Beta", 12, 2.50, 30, 22, 0],
                ],
                "Listino",
            )

            result = self.run_script("inspect_sources.py", source, "--output", output)

            self.assert_success(result)
            document = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(document["profiled_count"], 1)
            self.assertEqual(document["errors"], [])
            profile = document["profiles"][0]
            self.assertEqual(profile["deterministic_hint"]["state"], "SCHEMA_NOTO")
            self.assertEqual(profile["deterministic_hint"]["adapter_id"], "betulla_v1")
            self.assertEqual(profile["details"]["sheet_count"], 1)
            self.assertEqual(profile["details"]["sheets"][0]["active_range"]["max_row"], 3)
            self.assertEqual(profile["details"]["sheets"][0]["active_range"]["max_column"], 9)
            self.assertEqual(profile["sha256"], sha256(source))

    def test_noce_promotional_availability_remains_orderable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "noce.csv"
            with source.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(
                    stream,
                    fieldnames=["catalog_page", "ean", "product", "packaging", "availability", "variation", "price", "unit"],
                )
                writer.writeheader()
                writer.writerow({
                    "catalog_page": "1",
                    "ean": "8000000000001",
                    "product": "Prodotto con omaggio",
                    "packaging": "6 x 10 PZ",
                    "availability": "Disponibile ACQUISTA 3 CT IN OMAGGIO 1 CT",
                    "variation": "",
                    "price": "1,25",
                    "unit": "x 6",
                })
                writer.writerow({
                    "catalog_page": "1",
                    "ean": "8000000000002",
                    "product": "Prodotto esaurito",
                    "packaging": "6 x 10 PZ",
                    "availability": "Non disponibile",
                    "variation": "",
                    "price": "1,25",
                    "unit": "x 6",
                })

            records = read_noce(source)

            self.assertTrue(records[0]["usable"])
            self.assertFalse(records[1]["usable"])

    def test_manifest_validator_accepts_complete_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            master = root / "master.xlsx"
            supplier = root / "supplier.xlsx"
            manifest_path = root / "manifest.json"
            adapters_path = root / "adapters.json"
            report_path = root / "report.json"
            save_workbook(master, [["placeholder"]])
            save_workbook(supplier, [["placeholder"]])
            adapters_path.write_text(
                json.dumps({"adapters": [{"id": "master_known"}, {"id": "supplier_known"}]}),
                encoding="utf-8",
            )
            manifest = {
                "files": [
                    {
                        "path": str(master),
                        "file_name": master.name,
                        "sha256": sha256(master),
                        "ai_preflight": {
                            "state": "SCHEMA_NOTO",
                            "role": "master",
                            "adapter_id": "master_known",
                            "rationale": "Firma del gestionale nota.",
                        },
                        "user_confirmation": {"required": False, "status": "NOT_REQUIRED"},
                    },
                    {
                        "path": str(supplier),
                        "file_name": supplier.name,
                        "sha256": sha256(supplier),
                        "ai_preflight": {
                            "state": "SCHEMA_NOTO",
                            "role": "supplier",
                            "supplier_id": "fornitore_noto",
                            "adapter_id": "supplier_known",
                            "rationale": "Firma del fornitore nota.",
                        },
                        "user_confirmation": {"required": False, "status": "NOT_REQUIRED"},
                    },
                ]
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            result = self.run_script(
                "validate_input_manifest.py",
                "--manifest",
                manifest_path,
                "--adapters",
                adapters_path,
                "--output",
                report_path,
            )

            self.assert_success(result)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertTrue(report["valid"])
            self.assertEqual(report["master"], [master.name])
            self.assertEqual(report["suppliers"], ["fornitore_noto"])
            self.assertEqual(report["errors"], [])

    def test_manifest_validator_rejects_missing_mapping_and_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            master = root / "master.xlsx"
            supplier = root / "supplier_new.xlsx"
            manifest_path = root / "manifest_invalid.json"
            adapters_path = root / "adapters.json"
            report_path = root / "report_invalid.json"
            save_workbook(master, [["placeholder"]])
            save_workbook(supplier, [["placeholder"]])
            adapters_path.write_text(json.dumps({"adapters": [{"id": "master_known"}]}), encoding="utf-8")
            manifest = {
                "files": [
                    {
                        "path": str(master),
                        "file_name": master.name,
                        "ai_preflight": {
                            "state": "SCHEMA_NOTO",
                            "role": "master",
                            "adapter_id": "master_known",
                            "rationale": "Firma del gestionale nota.",
                        },
                        "user_confirmation": {"required": False, "status": "NOT_REQUIRED"},
                    },
                    {
                        "path": str(supplier),
                        "file_name": supplier.name,
                        "ai_preflight": {
                            "state": "NUOVO_FORNITORE",
                            "role": "supplier",
                            "supplier_id": "fornitore_nuovo",
                            "adapter_id": None,
                            "rationale": "Schema nuovo da mappare.",
                        },
                        "user_confirmation": {"required": True, "status": "PENDING"},
                    },
                ]
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            result = self.run_script(
                "validate_input_manifest.py",
                "--manifest",
                manifest_path,
                "--adapters",
                adapters_path,
                "--output",
                report_path,
            )

            self.assertEqual(result.returncode, 2)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertFalse(report["valid"])
            codes = {error["code"] for error in report["errors"]}
            self.assertIn("MAPPATURA_MANCANTE", codes)
            self.assertIn("CONFERMA_UTENTE_MANCANTE", codes)

    def test_manifest_validator_rejects_incomplete_supplier_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            master = root / "master.xlsx"
            supplier = root / "supplier_changed.xlsx"
            manifest_path = root / "manifest_incomplete.json"
            adapters_path = root / "adapters.json"
            report_path = root / "report_incomplete.json"
            save_workbook(master, [["placeholder"]])
            save_workbook(supplier, [["Descrizione"], ["Prodotto Alfa"]])
            adapters_path.write_text(json.dumps({"adapters": [{"id": "master_known"}]}), encoding="utf-8")
            manifest = {
                "files": [
                    {
                        "path": str(master),
                        "file_name": master.name,
                        "ai_preflight": {
                            "state": "SCHEMA_NOTO",
                            "role": "master",
                            "adapter_id": "master_known",
                            "rationale": "Firma del gestionale nota.",
                        },
                        "user_confirmation": {"required": False, "status": "NOT_REQUIRED"},
                    },
                    {
                        "path": str(supplier),
                        "file_name": supplier.name,
                        "ai_preflight": {
                            "state": "SCHEMA_VARIATO",
                            "role": "supplier",
                            "supplier_id": "fornitore_variato",
                            "rationale": "Il nome della colonna descrizione e cambiato.",
                            "field_mapping": {
                                "sheet": "Sheet1",
                                "data_start_row": 2,
                                "columns": {"description": "Descrizione"},
                            },
                        },
                        "user_confirmation": {"required": True, "status": "CONFIRMED"},
                    },
                ]
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            result = self.run_script(
                "validate_input_manifest.py",
                "--manifest",
                manifest_path,
                "--adapters",
                adapters_path,
                "--output",
                report_path,
            )

            self.assertEqual(result.returncode, 2)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            error = next(item for item in report["errors"] if item["code"] == "MAPPATURA_INCOMPLETA")
            self.assertIn("columns.unit_price_net oppure columns.unit_price_pre_discount", error["missing"])
            self.assertIn("fattore d'ordine in colonna oppure default esplicito", error["missing"])
            self.assertIn("order_column", error["missing"])

    def test_prepare_manifest_sources_parses_reordered_renamed_supplier(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            master = root / "gestionale.xlsx"
            supplier = root / "fornitore_fittizio.xlsx"
            manifest_path = root / "manifest.json"
            validation_path = root / "manifest_report.json"
            output_dir = root / "normalized"

            save_workbook(
                master,
                [
                    ["Tipo", "Codice", None, "Descrizione", "UM", "Colli", "Quantita", "Prezzo", "Sconto", "IVA", "Totale"],
                    ["C", "8000000000001", None, "Prodotto Alfa", "PZ", 99, 99, 2.50, None, 22, None],
                ],
                "Foglio1",
            )
            save_workbook(
                supplier,
                [
                    ["Listino promozionale del fornitore fittizio"],
                    [],
                    ["Disponibilita merce", "Prezzo Netto EUR", "Nome Articolo", "Pezzi Scatola", "Codice Interno", "Barcode EAN", "Aliquota IVA", "Qta ordine"],
                    ["SI", 1.25, "Prodotto Alfa", 6, "FIT-001", "8000000000001", 22, None],
                ],
                "Offerte agosto",
            )

            field_mapping = {
                "sheet": "Offerte agosto",
                "header_row": 3,
                "data_start_row": 4,
                "columns": {
                    "availability": "Disponibilita merce",
                    "unit_price_net": "Prezzo Netto EUR",
                    "description": "Nome Articolo",
                    "pieces_per_carton": "Pezzi Scatola",
                    "supplier_code": "Codice Interno",
                    "ean": "Barcode EAN",
                    "vat": "Aliquota IVA",
                },
                "available_values": ["SI"],
                "order_column": "H",
            }
            manifest = {
                "files": [
                    {
                        "path": str(master),
                        "file_name": master.name,
                        "sha256": sha256(master),
                        "ai_preflight": {
                            "state": "SCHEMA_NOTO",
                            "role": "master",
                            "adapter_id": "gestionale_v1",
                            "rationale": "Export gestionale conforme.",
                        },
                        "user_confirmation": {"required": False, "status": "NOT_REQUIRED"},
                    },
                    {
                        "path": str(supplier),
                        "file_name": supplier.name,
                        "sha256": sha256(supplier),
                        "ai_preflight": {
                            "state": "NUOVO_FORNITORE",
                            "role": "supplier",
                            "supplier_id": "fittizio",
                            "adapter_id": None,
                            "rationale": "Colonne rinominate e riordinate; mapping dichiarativo confermato.",
                            "field_mapping": field_mapping,
                        },
                        "user_confirmation": {"required": True, "status": "CONFIRMED"},
                    },
                ]
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            validation = self.run_script(
                "validate_input_manifest.py",
                "--manifest",
                manifest_path,
                "--adapters",
                ADAPTERS,
                "--output",
                validation_path,
            )
            self.assert_success(validation)

            result = self.run_script(
                "prepare_manifest_sources.py",
                "--manifest",
                manifest_path,
                "--adapters",
                ADAPTERS,
                "--output",
                output_dir,
            )

            self.assert_success(result)
            normalized = json.loads((output_dir / "normalized_sources.json").read_text(encoding="utf-8"))
            self.assertEqual(len(normalized["gestionale"]), 1)
            self.assertEqual(len(normalized["fittizio"]), 1)
            record = normalized["fittizio"][0]
            self.assertEqual(record["source_row"], 4)
            self.assertEqual(record["ean"], "8000000000001")
            self.assertEqual(record["supplier_code"], "FIT-001")
            self.assertEqual(record["description"], "Prodotto Alfa")
            self.assertEqual(record["pieces_per_carton"], "6.0000")
            self.assertEqual(record["unit_price_net"], "1.2500")
            self.assertEqual(record["availability"], "SI")
            self.assertEqual(record["order_column"], "H")
            self.assertTrue(record["usable"])

            matching = json.loads((output_dir / "matching_result.json").read_text(encoding="utf-8"))
            self.assertEqual(matching[0]["suppliers"]["fittizio"]["status"], "EAN_ESATTO")
            self.assertEqual(len(matching[0]["suppliers"]["fittizio"]["usable_candidates"]), 1)
            semantic_queue = json.loads((output_dir / "semantic_queue.json").read_text(encoding="utf-8"))
            self.assertEqual(semantic_queue, [])
            audit = json.loads((output_dir / "audit.json").read_text(encoding="utf-8"))
            self.assertEqual(audit["sources"]["fittizio"]["rows"], 1)
            self.assertEqual(audit["exact_unique_usable"]["fittizio"], 1)
            self.assertEqual(audit["inputs"][1]["state"], "NUOVO_FORNITORE")
            self.assertEqual(audit["inputs"][1]["records"], 1)

    def test_prepare_manifest_sources_separates_declared_standalone_display(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            master = root / "gestionale.xlsx"
            supplier = root / "cipresso.xlsx"
            manifest_path = root / "manifest.json"
            output_dir = root / "normalized"

            save_workbook(
                master,
                [
                    ["Tipo", "Codice", None, "Descrizione", "UM", "Colli", "Quantita", "Prezzo", "Sconto", "IVA", "Totale"],
                    ["C", "8000000000001", None, "Prodotto normale", "PZ", 99, 99, 2.50, None, 22, None],
                ],
                "Foglio1",
            )
            save_workbook(
                supplier,
                [
                    ["COD.ART.", "DES.ARTICOLO", "UM", "QT", "LISTINO", "COD.EAN", "ORDINE"],
                    ["E000001", "LABRINO EXPO BURROMIE 108 BIANCO+36 ROSA", "PZ", 1, 174.0, "4009440173219", None],
                    ["019654", "Prodotto normale", "PZ", 6, 4.90, "8000000000001", None],
                ],
                "Listino al 10-08-2026",
            )
            manifest = {
                "files": [
                    {
                        "path": str(master),
                        "file_name": master.name,
                        "sha256": sha256(master),
                        "ai_preflight": {
                            "state": "SCHEMA_NOTO",
                            "role": "master",
                            "adapter_id": "gestionale_v1",
                            "rationale": "Export gestionale conforme.",
                        },
                        "user_confirmation": {"required": False, "status": "NOT_REQUIRED"},
                    },
                    {
                        "path": str(supplier),
                        "file_name": supplier.name,
                        "sha256": sha256(supplier),
                        "ai_preflight": {
                            "state": "NUOVO_FORNITORE",
                            "role": "supplier",
                            "supplier_id": "cipresso",
                            "rationale": "Mappatura Cipresso verificata.",
                            "field_mapping": {
                                "sheet": "FIRST",
                                "header_row": 1,
                                "data_start_row": 2,
                                "columns": {
                                    "supplier_code": "COD.ART.",
                                    "description": "DES.ARTICOLO",
                                    "unit": "UM",
                                    "pieces_per_carton": "QT",
                                    "unit_price_net": "LISTINO",
                                    "ean": "COD.EAN",
                                },
                                "assume_available": True,
                                "vat_unavailable": True,
                                "order_column": "G",
                                "display_detection": {
                                    "description_regex": "\\bEXPO\\b",
                                    "supplier_code_regex": "^E\\d{6}$",
                                    "order_factor_equals": 1,
                                    "declared_units_regex": "\\b\\d+\\s+[^+\\s]+\\s*\\+\\s*\\d+\\s+[^\\s]+\\b",
                                    "confidence": "MEDIA",
                                },
                            },
                        },
                        "user_confirmation": {"required": False, "status": "NOT_REQUIRED"},
                    },
                ]
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            result = self.run_script(
                "prepare_manifest_sources.py",
                "--manifest",
                manifest_path,
                "--adapters",
                ADAPTERS,
                "--output",
                output_dir,
            )

            self.assert_success(result)
            normalized = json.loads((output_dir / "normalized_sources.json").read_text(encoding="utf-8"))
            self.assertEqual(len(normalized["cipresso"]), 1)
            self.assertEqual(normalized["cipresso"][0]["description"], "Prodotto normale")
            displays = json.loads((output_dir / "display_offers.json").read_text(encoding="utf-8"))
            self.assertEqual(len(displays["cipresso"]), 1)
            self.assertEqual(displays["cipresso"][0]["source_row"], 2)
            self.assertEqual(displays["cipresso"][0]["declared_units"], "144.0000")
            self.assertEqual(displays["cipresso"][0]["confidence"], "MEDIA")
            audit = json.loads((output_dir / "audit.json").read_text(encoding="utf-8"))
            self.assertEqual(audit["displays"]["cipresso"]["standalone_display_count"], 1)
            self.assertEqual(audit["exact_unique_usable"]["cipresso"], 1)


# ---------------------------------------------------------------------------
# Noce in formato Excel 97-2003 (.xls)
# ---------------------------------------------------------------------------

# Le intestazioni del listino vero, dalla colonna B alla colonna Q.
INTESTAZIONI_NOCE = (
    "codice_a_barre", "codice", "descrizione_articolo", "pezzi_x_cartone",
    "cartoni_x_stra", "strati_x_pal", "prezzo", "quantita", "offerta",
    "Importo", "descrizione_reparto", "cat", "ragione_sociale", "variato",
    "descrizione_offerta", "Iva",
)

# Il listino vero, misurato: 5,5 MB, 17.143 prodotti dalla riga 6 alla 17148,
# poi 931 righe che contengono soltanto la formula della colonna Importo.
LISTINO_VERO = Path(
    os.environ.get(
        "LISTINO_XLS_DI_PROVA",
        str(SKILL_ROOT / "listini-storici" / "formattato_104233.xls"),
    )
)


def celle_listino_noce(
    righe: list[dict[str, object]],
    righe_in_coda: int = 0,
    intestazioni: tuple[str, ...] = INTESTAZIONI_NOCE,
) -> bytes:
    """Costruisce i record BIFF di un foglio con la forma del listino Noce.

    La colonna A resta vuota, l'intestazione sta alla riga 5 e il primo
    prodotto alla riga 6, come nel file vero.  Le righe in coda contengono
    soltanto la formula della colonna Importo: sono quelle che Excel si porta
    dietro dopo l'ultimo prodotto e che non devono diventare articoli.
    """
    contenuto = scrittore_xls.label(3, 3, "i prezzi offerta sono in grassetto")
    for scarto, testo in enumerate(intestazioni):
        contenuto += scrittore_xls.label(4, 1 + scarto, testo, 1)

    for numero_riga, riga in enumerate(righe, start=5):
        ean = str(riga.get("ean") or "")
        contenuto += (
            scrittore_xls.label(numero_riga, 1, ean)
            if ean
            else scrittore_xls.blank(numero_riga, 1)
        )
        codice = riga["codice"]
        contenuto += (
            scrittore_xls.label(numero_riga, 2, codice)
            if isinstance(codice, str)
            else scrittore_xls.numero(numero_riga, 2, codice)
        )
        contenuto += scrittore_xls.label(numero_riga, 3, str(riga["descrizione"]))
        contenuto += scrittore_xls.numero(numero_riga, 4, riga.get("pezzi", 6))
        contenuto += scrittore_xls.numero(numero_riga, 5, 10)
        contenuto += scrittore_xls.numero(numero_riga, 6, 8)
        contenuto += scrittore_xls.numero(
            numero_riga, 7, riga["prezzo"], 1 if riga.get("prezzo_in_grassetto") else 0
        )
        contenuto += scrittore_xls.numero(numero_riga, 8, 0)
        contenuto += scrittore_xls.label(numero_riga, 9, str(riga.get("offerta", "NO")))
        contenuto += scrittore_xls.formula(numero_riga, 10, scrittore_xls.formula_numero(0.0))
        contenuto += scrittore_xls.label(numero_riga, 11, "CASALINGHI")
        contenuto += scrittore_xls.label(numero_riga, 12, str(riga["cat"]))
        contenuto += scrittore_xls.label(numero_riga, 13, "FORNITORE SPA")
        contenuto += scrittore_xls.label(numero_riga, 14, "NO")
        contenuto += scrittore_xls.blank(numero_riga, 15)
        contenuto += scrittore_xls.label(numero_riga, 16, "22")

    for scarto in range(righe_in_coda):
        contenuto += scrittore_xls.formula(
            5 + len(righe) + scarto, 10, scrittore_xls.formula_numero(0.0)
        )
    return contenuto


def scrivi_listino_noce(
    percorso: Path,
    righe: list[dict[str, object]],
    righe_in_coda: int = 0,
    intestazioni: tuple[str, ...] = INTESTAZIONI_NOCE,
) -> Path:
    """Scrive un .xls vero: contenitore OLE2, record BIFF8, prezzi in grassetto."""
    percorso.write_bytes(
        scrittore_xls.costruisci_xls(
            [("Foglio1", celle_listino_noce(righe, righe_in_coda, intestazioni)), ("Foglio2", b"")],
            # Due caratteri: normale e grassetto.  Il formato 1 usa il secondo,
            # ed e' cosi' che si scrive un prezzo in offerta.
            pesi_font=(400, 700),
            font_di_xf=(0, 1),
        )
    )
    return percorso


def voce_manifest(percorso: Path, decisione: dict[str, object]) -> dict[str, object]:
    return {
        "path": str(percorso),
        "file_name": percorso.name,
        "sha256": sha256(percorso),
        "ai_preflight": decisione,
        "user_confirmation": {"required": False, "status": "NOT_REQUIRED"},
    }


def scrivi_gestionale(percorso: Path, ean: str, descrizione: str) -> Path:
    save_workbook(
        percorso,
        [
            ["Tipo", "Codice", None, "Descrizione", "UM", "Colli", "Quantita", "Prezzo", "Sconto", "IVA", "Totale"],
            ["C", ean, None, descrizione, "PZ", 2, 2, 25.00, None, 22, None],
        ],
        "Foglio1",
    )
    return percorso


# L'anno prossimo, cosi' la scadenza credibile resta credibile anche fra
# qualche anno e il collaudo non scade insieme al prodotto.
ANNO_PROSSIMO = date.today().year + 1

RIGHE_DI_PROVA: list[dict[str, object]] = [
    # riga 6: articolo normale, e' anche quello che il gestionale cerca
    {"ean": "8009123304516", "codice": "0000000450011", "descrizione": "ABETO CASSERUOLA CUCINAMI ALL.32 H12",
     "pezzi": 1, "prezzo": 21.75, "cat": "NO FOOD"},
    # riga 7: scadenza credibile nella descrizione e codice con gli zeri iniziali
    {"ean": "8000000000002", "codice": "0000000449070",
     "descrizione": f"DETERSIVO PIATTI ML.500<br> Scadenza 30/08/{ANNO_PROSSIMO}",
     "pezzi": 12, "prezzo": 0.38, "cat": "NO FOOD"},
    # riga 8: la scadenza assurda, come quella al 2057 del listino vero
    {"ean": "8000000000003", "codice": "0000000461823",
     "descrizione": "SGRASSATORE UNIVERSALE ML.750<br> Scadenza 24/05/2057",
     "pezzi": 6, "prezzo": 1.20, "cat": "NO FOOD"},
    # riga 9: una data che sul calendario non esiste
    {"ean": "8000000000004", "codice": "0000000461824",
     "descrizione": "SCATOLA REGALO GRANDE<br> Scadenza 31/02/2027",
     "pezzi": 4, "prezzo": 2.10, "cat": "NO FOOD"},
    # riga 10: senza codice a barre
    {"ean": "", "codice": "0000000135976", "descrizione": "SAPONE SENZA CODICE A BARRE",
     "pezzi": 24, "prezzo": 0.55, "cat": "NO FOOD"},
    # riga 11: codice a barre corto, cioe' interno del fornitore
    {"ean": "87177756", "codice": "0000000449069", "descrizione": "PRODOTTO CON CODICE INTERNO",
     "pezzi": 12, "prezzo": 0.49, "cat": "NO FOOD"},
    # riga 12: il codice articolo scritto come numero invece che come testo
    {"ean": "8000000000007", "codice": 449070, "descrizione": "CODICE SCRITTO COME NUMERO",
     "pezzi": 6, "prezzo": 3.30, "cat": "NO FOOD"},
    # riga 13: offerta segnalata solo dal prezzo in grassetto
    {"ean": "8000000000008", "codice": "0000000000123", "descrizione": "OFFERTA SEGNALATA IN GRASSETTO",
     "pezzi": 6, "prezzo": 2.50, "cat": "NO FOOD", "prezzo_in_grassetto": True},
    # riga 14: offerta dichiarata nella colonna offerta
    {"ean": "8000000000009", "codice": "0000000000124", "descrizione": "OFFERTA DICHIARATA IN COLONNA",
     "pezzi": 6, "prezzo": 2.60, "cat": "NO FOOD", "offerta": "SI"},
    # righe 15 e 16: alimentari, l'utente non li tratta
    {"ean": "8000000000010", "codice": "0000000000125", "descrizione": "PASTA DI SEMOLA GR.500",
     "pezzi": 20, "prezzo": 0.89, "cat": "FOOD"},
    {"ean": "8000000000011", "codice": "0000000000126",
     "descrizione": "BISCOTTI FROLLINI GR.300<br> Scadenza 01/03/2027",
     "pezzi": 12, "prezzo": 1.45, "cat": "FOOD"},
]
RIGHE_IN_CODA = 6
PRIMA_RIGA_DATI = 6
ULTIMA_RIGA_DATI = PRIMA_RIGA_DATI + len(RIGHE_DI_PROVA) - 1  # 16
RIGHE_NO_FOOD = sum(1 for riga in RIGHE_DI_PROVA if riga["cat"] != "FOOD")
RIGHE_FOOD = len(RIGHE_DI_PROVA) - RIGHE_NO_FOOD


class NoceXlsTests(unittest.TestCase):
    """L'adattatore noce_xls_v1 dal riconoscimento del file al confronto.

    Il listino di prova viene letto una volta sola: tutte le prove guardano lo
    stesso risultato, come farebbe l'utente con una sola esecuzione.
    """

    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.radice = Path(tempfile.mkdtemp(prefix="collaudo_noce_"))
        listino = scrivi_listino_noce(cls.radice / "formattato_104233.xls", RIGHE_DI_PROVA, RIGHE_IN_CODA)
        gestionale = scrivi_gestionale(cls.radice / "gestionale.xlsx", "8009123304516", "Casseruola alluminio")
        manifest = cls.radice / "manifest.json"
        manifest.write_text(json.dumps({"files": [
            voce_manifest(gestionale, {
                "state": "SCHEMA_NOTO", "role": "master", "adapter_id": "gestionale_v1",
                "rationale": "Export gestionale conforme.",
            }),
            voce_manifest(listino, {
                "state": "SCHEMA_NOTO", "role": "supplier", "supplier_id": "noce",
                "adapter_id": "noce_xls_v1",
                "rationale": "Firma delle intestazioni Noce nel foglio Foglio1.",
            }),
        ]}), encoding="utf-8")

        # Prima il cancello: un manifest che nomina noce_xls_v1 deve passare
        # la validazione, altrimenti l'adattatore non entra nella pipeline.
        convalida = cls.radice / "manifest_report.json"
        esegui_script_riuscito(
            "validate_input_manifest.py",
            "--manifest", manifest, "--adapters", ADAPTERS, "--output", convalida,
        )
        cls.convalida = json.loads(convalida.read_text(encoding="utf-8"))

        dati = cls.radice / "dati"
        esegui_script_riuscito(
            "prepare_manifest_sources.py",
            "--manifest", manifest, "--adapters", ADAPTERS, "--output", dati,
        )
        cls.normalized = json.loads((dati / "normalized_sources.json").read_text(encoding="utf-8"))
        cls.matching = json.loads((dati / "matching_result.json").read_text(encoding="utf-8"))
        cls.audit = json.loads((dati / "audit.json").read_text(encoding="utf-8"))
        cls.lettura = cls.audit["inputs"][1]["reading"]
        cls.righe = {record["source_row"]: record for record in cls.normalized["noce"]}
        cls.addClassCleanup(shutil.rmtree, cls.radice, True)

    # -- riconoscimento del formato ----------------------------------------

    def test_lo_xls_e_riconosciuto_dai_byte_e_non_dall_estensione(self) -> None:
        """Un listino .xls rinominato .xlsx resta un .xls e viene letto lo stesso."""
        with tempfile.TemporaryDirectory() as temporaneo:
            radice = Path(temporaneo)
            travestito = scrivi_listino_noce(radice / "listino_noce.xlsx", RIGHE_DI_PROVA, RIGHE_IN_CODA)
            profili = radice / "profiles.json"

            esegui_script_riuscito("inspect_sources.py", travestito, "--output", profili)

            documento = json.loads(profili.read_text(encoding="utf-8"))
            self.assertEqual(documento["errors"], [])
            profilo = documento["profiles"][0]
            self.assertEqual(profilo["declared_suffix"], ".xlsx")
            self.assertEqual(profilo["content_format"], "xls")
            self.assertEqual(profilo["details"]["format"], "xls")
            hint = profilo["deterministic_hint"]
            self.assertEqual(hint["state"], "SCHEMA_NOTO")
            self.assertEqual(hint["adapter_id"], "noce_xls_v1")
            self.assertIn("riga 5", " ".join(hint["evidence"]))
            self.assertIn("riga 6", " ".join(hint["evidence"]))

    def test_un_xlsx_rinominato_xls_non_fa_esplodere_niente(self) -> None:
        """La strada opposta: dentro c'è un .xlsx e va letto con openpyxl."""
        with tempfile.TemporaryDirectory() as temporaneo:
            radice = Path(temporaneo)
            travestito = radice / "listino_betulla.xls"
            save_workbook(
                travestito,
                [
                    ["EAN", "CodArt", "ORDINE", "Descr.Commerciale", "PzCt", "Cessione", "Pedana", "Iva", "TOTALI"],
                    ["8000000000001", "C-001", None, "Prodotto Alfa", 6, 1.25, 60, 22, 0],
                ],
                "Listino",
            )
            profili = radice / "profiles.json"

            esegui_script_riuscito("inspect_sources.py", travestito, "--output", profili)

            documento = json.loads(profili.read_text(encoding="utf-8"))
            self.assertEqual(documento["errors"], [])
            profilo = documento["profiles"][0]
            self.assertEqual(profilo["declared_suffix"], ".xls")
            self.assertEqual(profilo["content_format"], "xlsx")
            self.assertEqual(profilo["details"]["format"], "xlsx")
            self.assertEqual(profilo["deterministic_hint"]["adapter_id"], "betulla_v1")

    # -- ingresso nella pipeline -------------------------------------------

    def test_il_listino_xls_entra_nel_confronto(self) -> None:
        """Il manifest passa la validazione e il prodotto arriva al confronto."""
        self.assertTrue(self.convalida["valid"], self.convalida["errors"])
        self.assertEqual(self.convalida["suppliers"], ["noce"])
        self.assertEqual(self.audit["inputs"][1]["content_format"], "xls")
        confronto = self.matching[0]["suppliers"]["noce"]
        self.assertEqual(confronto["status"], "EAN_ESATTO")
        self.assertEqual(confronto["usable_candidates"][0]["source_row"], 6)
        self.assertEqual(self.audit["exact_unique_usable"]["noce"], 1)

    def test_un_foglio_sbagliato_viene_spiegato_in_italiano(self) -> None:
        """Se la mappatura punta al foglio sbagliato l'utente deve capire perché."""
        adattatore = next(
            voce for voce in json.loads(ADAPTERS.read_text(encoding="utf-8"))["adapters"]
            if voce["id"] == "noce_xls_v1"
        )
        manifest = self.radice / "manifest_foglio_sbagliato.json"
        manifest.write_text(json.dumps({"files": [
            voce_manifest(self.radice / "gestionale.xlsx", {
                "state": "SCHEMA_NOTO", "role": "master", "adapter_id": "gestionale_v1",
                "rationale": "Export gestionale conforme.",
            }),
            voce_manifest(self.radice / "formattato_104233.xls", {
                "state": "SCHEMA_VARIATO", "role": "supplier", "supplier_id": "noce",
                "adapter_id": "noce_xls_v1",
                "rationale": "Prova con il foglio sbagliato.",
                "field_mapping": dict(adattatore["field_mapping"], sheet="Foglio2"),
            }),
        ]}), encoding="utf-8")

        esito = esegui_script(
            "prepare_manifest_sources.py",
            "--manifest", manifest, "--adapters", ADAPTERS,
            "--output", self.radice / "dati_foglio_sbagliato",
        )

        self.assertNotEqual(esito.returncode, 0)
        messaggio = esito.stderr.decode("utf-8", errors="replace")
        self.assertIn("Foglio2", messaggio)
        self.assertIn("non arriva alla riga 5", messaggio)
        self.assertNotIn("list index out of range", messaggio)

    # -- le regole dichiarate ----------------------------------------------

    def test_le_righe_food_sono_scartate_e_contate(self) -> None:
        """L'utente non tratta l'alimentare: le righe FOOD non entrano, e si sa quante."""
        self.assertEqual(len(self.normalized["noce"]), RIGHE_NO_FOOD)
        self.assertEqual(self.lettura["rows_excluded"], {"food": RIGHE_FOOD})
        self.assertEqual(self.lettura["rows_kept"], RIGHE_NO_FOOD)
        categorie = {record["category"] for record in self.normalized["noce"]}
        self.assertEqual(categorie, {"NO FOOD"})
        descrizioni = {record["description"] for record in self.normalized["noce"]}
        self.assertNotIn("PASTA DI SEMOLA GR.500", descrizioni)
        self.assertNotIn("BISCOTTI FROLLINI GR.300", descrizioni)

    def test_il_confine_dei_dati_non_lascia_prodotti_fantasma(self) -> None:
        """Dopo l'ultimo prodotto restano solo formule: non sono articoli."""
        self.assertEqual(self.lettura["sheet_rows"], ULTIMA_RIGA_DATI + RIGHE_IN_CODA)
        self.assertEqual(self.lettura["data_start_row"], PRIMA_RIGA_DATI)
        self.assertEqual(self.lettura["data_end_row"], ULTIMA_RIGA_DATI)
        self.assertEqual(self.lettura["rows_ignored_after_data_end"], RIGHE_IN_CODA)
        self.assertLessEqual(max(self.righe), ULTIMA_RIGA_DATI)
        self.assertTrue(all(record["description"] for record in self.normalized["noce"]))

    def test_la_scadenza_esce_dalla_descrizione_che_resta_pulita(self) -> None:
        """La data si legge, la descrizione si ripulisce dal frammento HTML."""
        record = self.righe[7]
        self.assertEqual(record["description"], "DETERSIVO PIATTI ML.500")
        self.assertEqual(record["expiry_date"], f"{ANNO_PROSSIMO}-08-30")
        self.assertTrue(record["expiry_plausible"])
        self.assertEqual(record["expiry_raw"], f"<br> Scadenza 30/08/{ANNO_PROSSIMO}")
        senza_scadenza = self.righe[6]
        self.assertIsNone(senza_scadenza["expiry_date"])
        self.assertNotIn("<br>", " ".join(r["description"] for r in self.normalized["noce"]))
        self.assertEqual(self.lettura["expiry_dates_read"], 2)

    def test_una_data_assurda_si_segnala_e_non_ferma_la_lettura(self) -> None:
        """Il 2057 e il 31 febbraio si leggono, si dichiarano e non si credono."""
        assurda = self.righe[8]
        self.assertEqual(assurda["description"], "SGRASSATORE UNIVERSALE ML.750")
        self.assertEqual(assurda["expiry_date"], "2057-05-24")
        self.assertFalse(assurda["expiry_plausible"])
        self.assertTrue(assurda["usable"])

        impossibile = self.righe[9]
        self.assertEqual(impossibile["description"], "SCATOLA REGALO GRANDE")
        self.assertIsNone(impossibile["expiry_date"])
        self.assertFalse(impossibile["expiry_plausible"])
        self.assertTrue(impossibile["usable"])

        self.assertEqual(self.lettura["expiry_dates_to_check"], 2)
        avvisi = {avviso["source_row"]: avviso["warning"] for avviso in self.audit["warnings"]}
        self.assertEqual(sorted(avvisi), [8, 9])
        self.assertIn("fuori dal credibile", avvisi[8])
        self.assertIn("scritta male", avvisi[9])
        # la lettura e' andata avanti fino in fondo
        self.assertEqual(len(self.normalized["noce"]), RIGHE_NO_FOOD)

    def test_gli_zeri_iniziali_del_codice_articolo_restano(self) -> None:
        """Il codice fornitore è un testo: 0000000449070 non è 449070."""
        self.assertEqual(self.righe[7]["supplier_code"], "0000000449070")
        self.assertIsInstance(self.righe[7]["supplier_code"], str)
        # e una cella scritta come numero diventa testo, senza la coda ".0"
        self.assertEqual(self.righe[12]["supplier_code"], "449070")
        self.assertIsInstance(self.righe[12]["supplier_code"], str)

    def test_un_ean_corto_o_vuoto_non_fa_scartare_la_riga(self) -> None:
        """Codici a barre interni o assenti restano articoli ordinabili."""
        vuoto = self.righe[10]
        self.assertEqual(vuoto["ean"], "")
        self.assertEqual(vuoto["description"], "SAPONE SENZA CODICE A BARRE")
        self.assertTrue(vuoto["usable"])
        corto = self.righe[11]
        self.assertEqual(corto["ean"], "87177756")
        self.assertTrue(corto["usable"])

    def test_il_prezzo_e_al_pezzo_e_l_ordine_in_cartoni(self) -> None:
        """I due campi che il resto del programma già usa, senza conversioni inventate."""
        record = self.righe[7]
        self.assertEqual(record["unit_price_net"], "0.3800")
        self.assertEqual(record["pieces_per_carton"], "12.0000")
        self.assertIsNone(record["order_multiplier"])
        self.assertEqual(record["order_column"], "I")
        # Il totale di riga e' quello della colonna Importo: cartoni * prezzo al
        # pezzo * pezzi per cartone.  Con 3 cartoni: 3 * 0,38 * 12 = 13,68.
        self.assertAlmostEqual(3 * float(record["unit_price_net"]) * float(record["pieces_per_carton"]), 13.68, places=4)

    def test_il_prezzo_in_grassetto_segnala_l_offerta(self) -> None:
        """«I prezzi offerta sono in grassetto»: quel segnale non si butta via."""
        grassetto = self.righe[13]
        self.assertTrue(grassetto["offer_price_bold"])
        self.assertTrue(grassetto["offer"])
        self.assertEqual(grassetto["offer_flag"], "NO")
        dichiarata = self.righe[14]
        self.assertFalse(dichiarata["offer_price_bold"])
        self.assertTrue(dichiarata["offer"])
        normale = self.righe[6]
        self.assertFalse(normale["offer_price_bold"])
        self.assertFalse(normale["offer"])


@unittest.skipUnless(
    LISTINO_VERO.is_file(),
    f"Manca il listino Noce vero: {LISTINO_VERO}. "
    "Si può indicare un altro percorso con la variabile d'ambiente LISTINO_XLS_DI_PROVA.",
)
class ListinoNoceVeroTests(unittest.TestCase):
    """La stessa pipeline sul file vero da 5,5 MB, con i numeri già misurati."""

    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.radice = Path(tempfile.mkdtemp(prefix="collaudo_noce_vero_"))
        cls.prima = impronta_file(LISTINO_VERO)
        gestionale = scrivi_gestionale(cls.radice / "gestionale.xlsx", "8009123304516", "Casseruola alluminio")
        manifest = cls.radice / "manifest.json"
        manifest.write_text(json.dumps({"files": [
            voce_manifest(gestionale, {
                "state": "SCHEMA_NOTO", "role": "master", "adapter_id": "gestionale_v1",
                "rationale": "Export gestionale conforme.",
            }),
            voce_manifest(LISTINO_VERO, {
                "state": "SCHEMA_NOTO", "role": "supplier", "supplier_id": "noce",
                "adapter_id": "noce_xls_v1",
                "rationale": "Firma delle intestazioni Noce nel foglio Foglio1.",
            }),
        ]}), encoding="utf-8")
        dati = cls.radice / "dati"
        esegui_script_riuscito(
            "prepare_manifest_sources.py",
            "--manifest", manifest, "--adapters", ADAPTERS, "--output", dati,
        )
        cls.normalized = json.loads((dati / "normalized_sources.json").read_text(encoding="utf-8"))
        cls.audit = json.loads((dati / "audit.json").read_text(encoding="utf-8"))
        cls.lettura = cls.audit["inputs"][1]["reading"]
        cls.addClassCleanup(shutil.rmtree, cls.radice, True)

    def test_i_numeri_del_listino_vero(self) -> None:
        """17.143 righe di dati: 8.292 alimentari fuori, 8.851 dentro."""
        self.assertEqual(self.lettura["sheet_rows"], 18079)
        self.assertEqual(self.lettura["data_start_row"], 6)
        self.assertEqual(self.lettura["data_end_row"], 17148)
        self.assertEqual(self.lettura["rows_ignored_after_data_end"], 931)
        self.assertEqual(self.lettura["rows_excluded"], {"food": 8292})
        self.assertEqual(self.lettura["rows_kept"], 8851)
        self.assertEqual(len(self.normalized["noce"]), 8851)

    def test_codici_scadenze_e_codici_a_barre_del_listino_vero(self) -> None:
        """I campi delicati, contati sul file vero e non stimati."""
        righe = self.normalized["noce"]
        self.assertTrue(all(isinstance(record["supplier_code"], str) for record in righe))
        self.assertTrue(all(len(record["supplier_code"]) == 13 for record in righe))
        self.assertTrue(all(record["supplier_code"].startswith("0") for record in righe))
        self.assertEqual(sum(1 for record in righe if not record["ean"]), 27)
        self.assertEqual(sum(1 for record in righe if 0 < len(record["ean"]) < 13), 105)
        self.assertEqual(self.lettura["expiry_dates_read"], 375)
        self.assertFalse(any("<br>" in record["description"] for record in righe))
        self.assertTrue(all(record["usable"] for record in righe))

    def test_il_listino_vero_non_viene_toccato(self) -> None:
        """Principio numero uno: gli input originali restano come sono arrivati.

        Si guarda anche la data di modifica, non solo il contenuto: riscrivere
        un file con gli stessi byte lo lascia identico ma non è leggerlo.
        """
        self.assertEqual(impronta_file(LISTINO_VERO), self.prima)


# ---------------------------------------------------------------------------
# Fase 9b — la mappatura delle colonne e il grassetto
# ---------------------------------------------------------------------------


def scrivi_listino_noce_xlsx(
    percorso: Path,
    righe: list[dict[str, object]],
    intestazioni: tuple[str, ...] = INTESTAZIONI_NOCE,
) -> Path:
    """Lo stesso listino, ma in .xlsx: e' cosi' che arriva se qualcuno lo risalva.

    Serve a provare che il grassetto del prezzo — il segnale dell'offerta — non
    dipende dal formato in cui il documento e' arrivato.
    """
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Foglio1"
    sheet.cell(row=4, column=4, value="i prezzi offerta sono in grassetto")
    for scarto, testo in enumerate(intestazioni):
        sheet.cell(row=5, column=2 + scarto, value=testo)
    for numero_riga, riga in enumerate(righe, start=6):
        valori = [
            str(riga.get("ean") or ""), riga["codice"], riga["descrizione"], riga.get("pezzi", 6),
            10, 8, riga["prezzo"], 0, str(riga.get("offerta", "NO")), 0,
            "CASALINGHI", riga["cat"], "FORNITORE SPA", "NO", None, "22",
        ]
        for scarto, valore in enumerate(valori):
            sheet.cell(row=numero_riga, column=2 + scarto, value=valore)
        if riga.get("prezzo_in_grassetto"):
            sheet.cell(row=numero_riga, column=8).font = Font(bold=True)
    workbook.save(percorso)
    workbook.close()
    return percorso


class MappaturaDelleColonneTests(unittest.TestCase):
    """Una colonna dichiarata per nome non deve mai diventare un'altra colonna."""

    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.mappatura = next(
            voce for voce in json.loads(ADAPTERS.read_text(encoding="utf-8"))["adapters"]
            if voce["id"] == "noce_xls_v1"
        )["field_mapping"]

    def test_il_nome_dichiarato_si_cerca_nell_intestazione(self) -> None:
        intestazione = ["codice_a_barre", "codice", "descrizione_articolo", "cat"]
        self.assertEqual(column_number("cat", intestazione), 4)
        self.assertEqual(column_number("Codice", intestazione), 2)

    def test_un_nome_assente_non_ripiega_sulle_lettere(self) -> None:
        """«cat» e' anche un riferimento di colonna Excel valido: CAT = 2394.

        Col ripiego, un listino che rinomina quella colonna verrebbe letto
        puntando a una colonna vuota, e nessuno se ne accorgerebbe.
        """
        intestazione = ["codice_a_barre", "codice", "descrizione_articolo", "categoria"]
        with self.assertRaises(ValueError) as errore:
            column_number("cat", intestazione)
        messaggio = str(errore.exception)
        self.assertIn("«cat»", messaggio)
        self.assertIn("categoria", messaggio)
        # 2074 è la colonna CAT secondo la stessa funzione che il progetto usa
        # per le lettere: se comparisse nel messaggio vorrebbe dire che il
        # ripiego è tornato.
        self.assertEqual(column_index_from_string("CAT"), 2074)
        self.assertNotIn("2074", messaggio)

    def test_senza_intestazione_le_lettere_restano_ammesse(self) -> None:
        """Dove non c'è una riga di intestazione da leggere, la lettera è l'unico modo."""
        self.assertEqual(column_number("C"), 3)
        self.assertEqual(column_number("AA"), 27)
        self.assertEqual(column_number(7), 7)

    def test_una_colonna_rinominata_ferma_la_lettura_invece_di_far_entrare_il_food(self) -> None:
        """La prova che conta: 2 righe alimentari su 11 non devono entrare in silenzio."""
        with tempfile.TemporaryDirectory() as temporaneo:
            intestazioni = tuple(
                "categoria_merceologica" if nome == "cat" else nome
                for nome in INTESTAZIONI_NOCE
            )
            listino = scrivi_listino_noce(
                Path(temporaneo) / "formattato_104233.xls", RIGHE_DI_PROVA, RIGHE_IN_CODA, intestazioni,
            )

            with self.assertRaises(ValueError) as errore:
                read_mapped_xlsx_supplier(listino, "noce", self.mappatura)

            messaggio = str(errore.exception)
            self.assertIn("«cat»", messaggio)
            self.assertIn("categoria_merceologica", messaggio)

    def test_il_grassetto_del_prezzo_si_legge_anche_da_un_xlsx(self) -> None:
        """Risalvare il listino in .xlsx non deve far sparire il segnale dell'offerta."""
        with tempfile.TemporaryDirectory() as temporaneo:
            listino = scrivi_listino_noce_xlsx(
                Path(temporaneo) / "listino_noce.xlsx", RIGHE_DI_PROVA,
            )

            records, _avvisi = read_mapped_xlsx_supplier(listino, "noce", self.mappatura)

            righe = {record["description"]: record for record in records}
            in_grassetto = righe["OFFERTA SEGNALATA IN GRASSETTO"]
            self.assertTrue(in_grassetto["offer_price_bold"])
            self.assertTrue(in_grassetto["offer"])
            self.assertEqual(in_grassetto["offer_flag"], "NO")
            normale = righe["ABETO CASSERUOLA CUCINAMI ALL.32 H12"]
            self.assertFalse(normale["offer_price_bold"])
            self.assertFalse(normale["offer"])
            # La colonna «offerta» continua a valere da sola, senza grassetto.
            self.assertTrue(righe["OFFERTA DICHIARATA IN COLONNA"]["offer"])
            self.assertFalse(righe["OFFERTA DICHIARATA IN COLONNA"]["offer_price_bold"])

    def test_il_valore_delle_celle_non_cambia_leggendo_anche_il_grassetto(self) -> None:
        """Il ramo .xlsx è stato riscritto: i valori devono restare quelli."""
        with tempfile.TemporaryDirectory() as temporaneo:
            listino = scrivi_listino_noce_xlsx(
                Path(temporaneo) / "listino_noce.xlsx", RIGHE_DI_PROVA,
            )

            records, _avvisi = read_mapped_xlsx_supplier(listino, "noce", self.mappatura)

            self.assertEqual(len(records), RIGHE_NO_FOOD)
            self.assertFalse(any(record.get("category") == "FOOD" for record in records))
            prima = records[0]
            self.assertEqual(prima["ean"], "8009123304516")
            self.assertEqual(prima["supplier_code"], "0000000450011")
            self.assertEqual(prima["unit_price_net"], "21.7500")
            self.assertEqual(prima["pieces_per_carton"], "1.0000")

    def test_una_cella_vuota_non_e_un_prezzo_in_offerta(self) -> None:
        """Le celle mai scritte non hanno il carattere: valgono «non in grassetto»."""
        senza_prezzo = [*RIGHE_DI_PROVA, {
            "ean": "8000000000099", "codice": "0000000000999",
            "descrizione": "ARTICOLO SENZA PREZZO", "pezzi": 6, "prezzo": None, "cat": "NO FOOD",
        }]
        with tempfile.TemporaryDirectory() as temporaneo:
            listino = scrivi_listino_noce_xlsx(Path(temporaneo) / "listino.xlsx", senza_prezzo)

            records, _avvisi = read_mapped_xlsx_supplier(listino, "noce", self.mappatura)

            riga = next(record for record in records if record["description"] == "ARTICOLO SENZA PREZZO")
            self.assertFalse(riga["offer_price_bold"])
            self.assertFalse(riga["offer"])
            self.assertFalse(riga["usable"])

    def test_il_grassetto_si_legge_solo_quando_la_mappatura_lo_chiede(self) -> None:
        """Leggerlo da un .xlsx costa dal 5 al 45%: non si paga senza motivo."""
        with tempfile.TemporaryDirectory() as temporaneo:
            listino = scrivi_listino_noce_xlsx(Path(temporaneo) / "listino.xlsx", RIGHE_DI_PROVA)
            senza = {chiave: valore for chiave, valore in self.mappatura.items() if chiave != "offer_from_bold"}

            _inizio, _colonne, righe_con, grassetto_con = mapped_rows(listino, self.mappatura)
            _inizio, _colonne, righe_senza, grassetto_senza = mapped_rows(listino, senza)

            self.assertEqual(righe_con, righe_senza)
            self.assertTrue(any(any(riga) for riga in grassetto_con))
            self.assertEqual(grassetto_senza, [])

    def test_una_riga_di_intestazione_impossibile_viene_detta(self) -> None:
        with tempfile.TemporaryDirectory() as temporaneo:
            listino = scrivi_listino_noce_xlsx(Path(temporaneo) / "listino.xlsx", RIGHE_DI_PROVA)
            storta = {**self.mappatura, "header_row": -1}

            with self.assertRaises(ValueError) as errore:
                mapped_rows(listino, storta)

            self.assertIn("riga di intestazione -1", str(errore.exception))

    def test_un_csv_senza_intestazione_si_mappa_dichiarandolo(self) -> None:
        """Senza una via d'uscita, un CSV senza intestazione non sarebbe più leggibile:
        la prima riga di prodotti verrebbe presa per la riga dei nomi."""
        with tempfile.TemporaryDirectory() as temporaneo:
            percorso = Path(temporaneo) / "listino_senza_intestazione.csv"
            percorso.write_text(
                "8000000000002;ARTICOLO UNO;12;2,50\n8000000000003;ARTICOLO DUE;6;1,20\n",
                encoding="utf-8",
            )
            mappatura = {
                "header_row": 0,
                "delimiter": ";",
                "italian_numbers": True,
                "columns": {"ean": "A", "description": "B", "pieces_per_carton": "C", "unit_price_net": "D"},
            }

            records, _avvisi = read_mapped_csv_supplier(percorso, "nuovo", mappatura)

            self.assertEqual([record["description"] for record in records], ["ARTICOLO UNO", "ARTICOLO DUE"])
            self.assertEqual(records[0]["unit_price_net"], "2.5000")
            self.assertEqual(records[0]["ean"], "8000000000002")

    def test_su_un_foglio_largo_l_elenco_delle_intestazioni_si_ferma(self) -> None:
        intestazione = [f"colonna_{numero}" for numero in range(1, 26)]

        with self.assertRaises(ValueError) as errore:
            column_number("cat", intestazione)

        self.assertIn("e altre 5", str(errore.exception))
        self.assertNotIn("colonna_21", str(errore.exception))


# ---------------------------------------------------------------------------
# Fase 4 — l'inspector smette di conoscere i fornitori
# ---------------------------------------------------------------------------

# Intestazioni che non somigliano a niente di censito: nessuno di questi token
# compare nell'elenco di parole di `header_candidates`.  Servono a provare che
# un fornitore nuovo arriva al registro lo stesso.
INTESTAZIONI_MAI_VISTE = ["Rif. Interno", "Barre", "Denominazione", "Confezione", "Imponibile"]

INTESTAZIONI_BETULLA = ["EAN", "CodArt", "ORDINE", "Descr.Commerciale", "PzCt",
                      "Cessione", "Pedana", "Iva", "TOTALI"]


class MotoreDelRegistroTests(unittest.TestCase):
    """Il riconoscimento viene dal registro, non da costanti scritte qui dentro.

    Il programma finito gira da solo: quando un fornitore cambia il listino
    l'unica cosa che si potrà toccare è `references/adapters.json`. Queste
    prove servono a dimostrare che è davvero così — che una regola aggiunta al
    registro cambia l'esito, e che una regola tolta dal registro fa sparire un
    fornitore che il codice "conosceva".
    """

    maxDiff = None

    def setUp(self) -> None:
        self.radice = Path(tempfile.mkdtemp(prefix="collaudo_motore_"))
        self.addCleanup(shutil.rmtree, self.radice, True)

    def usa_registro(self, voci: list[dict[str, object]], nome: str = "adapters.json") -> Path:
        """Fa leggere all'inspector un registro di prova al posto di quello vero.

        Il registro vero lo legge una persona prima del commit: un collaudo non
        deve poterlo toccare nemmeno per sbaglio.
        """
        percorso = self.radice / nome
        percorso.write_bytes(json.dumps(
            {"schema_version": 1, "adapters": voci}, ensure_ascii=False, indent=2,
        ).encode("utf-8"))
        originale = registro.REGISTRO
        registro.REGISTRO = percorso
        self.addCleanup(setattr, registro, "REGISTRO", originale)
        return percorso

    def adattatori_senza(self, identificativo: str) -> list[dict[str, object]]:
        voci = json.loads(ADAPTERS.read_text(encoding="utf-8"))["adapters"]
        return [voce for voce in voci if voce["id"] != identificativo]

    def hint(self, percorso: Path) -> dict[str, object]:
        return inspect_sources.profile_file(percorso)["deterministic_hint"]

    # -- la regola sta nel registro ----------------------------------------

    def test_un_fornitore_mai_visto_si_riconosce_dichiarandolo_nel_registro(self) -> None:
        """La prova che dice se la Fase 4 è servita a qualcosa.

        Nessuna di queste intestazioni è mai stata dentro `inspect_sources`: se
        il riconoscimento arrivasse ancora dal codice, dichiarare l'adattatore
        non basterebbe e servirebbe un programmatore a ogni fornitore nuovo.
        """
        percorso = self.radice / "listino_di_un_fornitore_nuovo.xlsx"
        save_workbook(percorso, [
            INTESTAZIONI_MAI_VISTE,
            ["A1", "8000000000001", "PRODOTTO ALFA", 6, 1.25],
            ["A2", "8000000000002", "PRODOTTO BETA", 12, 2.50],
        ], "Listino")

        sconosciuto = self.hint(percorso)
        foglio = inspect_sources.profile_file(percorso)["details"]["sheets"][0]

        self.usa_registro([{
            "id": "fornitore_nuovo_v1", "kind": "supplier", "supplier_id": "fornitore_nuovo",
            "header_signature": {
                "kind": "headers", "sheet": "FIRST", "header_row": 1, "data_start_row": 2,
                "required": ["barre", "confezione", "denominazione", "imponibile", "rifinterno"],
                "known": ["barre", "confezione", "denominazione", "imponibile", "rifinterno"],
            },
        }])
        dichiarato = self.hint(percorso)

        self.assertEqual(sconosciuto["state"], "AMBIGUO")
        self.assertIsNone(sconosciuto["adapter_id"])
        self.assertEqual(dichiarato["state"], "SCHEMA_NOTO")
        self.assertEqual(dichiarato["adapter_id"], "fornitore_nuovo_v1")
        # E il motivo per cui ci è arrivato: `header_candidates` non avrebbe
        # tenuto nemmeno una riga, perché nessuna di quelle parole è nel suo
        # elenco. Senza `header_rows` il registro non avrebbe niente da leggere.
        self.assertEqual(foglio["header_candidates"], [])
        self.assertEqual(foglio["header_rows"][0]["row"], 1)
        self.assertEqual(foglio["header_rows"][0]["values"], INTESTAZIONI_MAI_VISTE)

    def test_togliere_la_firma_dal_registro_fa_sparire_un_fornitore_noto(self) -> None:
        """BETULLA era scritto dentro `known_xlsx_hint`: se ci fosse ancora,
        questa prova resterebbe verde con l'adattatore tolto dal registro.
        È la differenza fra una regola dichiarata e una cablata."""
        percorso = self.radice / "listino_betulla.xlsx"
        save_workbook(percorso, [
            INTESTAZIONI_BETULLA,
            ["8000000000001", "C-001", None, "Prodotto Alfa", 6, 1.25, 60, 22, 0],
        ], "Listino")

        con = self.hint(percorso)
        self.usa_registro(self.adattatori_senza("betulla_v1"))
        senza = self.hint(percorso)

        self.assertEqual(con["adapter_id"], "betulla_v1")
        self.assertEqual(senza["state"], "AMBIGUO")
        self.assertIsNone(senza["adapter_id"])
        self.assertEqual(senza["evidence"], ["Nessuna firma nota sufficiente"])

    def test_anche_il_csv_chiede_al_registro(self) -> None:
        """La firma del CSV Noce era un insieme scritto dentro `profile_csv`:
        adesso è una voce del registro come tutte le altre."""
        percorso = self.radice / "noce.csv"
        with percorso.open("w", encoding="utf-8-sig", newline="") as stream:
            scrittore = csv.writer(stream)
            scrittore.writerow(["catalog_page", "ean", "product", "packaging",
                                "availability", "variation", "price", "unit"])
            scrittore.writerow(["1", "8000000000001", "PRODOTTO", "6 x 10 PZ", "Disponibile", "", "1,25", "x 6"])

        noto = self.hint(percorso)
        self.usa_registro(self.adattatori_senza("noce_csv_v1"))
        ignoto = self.hint(percorso)

        self.assertEqual(noto["state"], "SCHEMA_NOTO")
        self.assertEqual(noto["adapter_id"], "noce_csv_v1")
        # Un CSV non ha fogli: l'impronta lo dice invece di inventarne uno.
        self.assertIsNone(noto["signature"]["sheet"])
        self.assertEqual(noto["signature"]["header_row"], 1)
        self.assertEqual(ignoto["state"], "AMBIGUO")
        self.assertIsNone(ignoto["adapter_id"])

    def test_normalized_non_e_una_seconda_implementazione(self) -> None:
        """Due normalizzazioni che si allontanano di un carattere vorrebbero
        dire un listino riconosciuto dall'inspector e non ritrovato nel
        registro, senza che nessuno capisca perché."""
        originale = registro.normalizza
        registro.normalizza = lambda valore: "una_sola_implementazione"
        self.addCleanup(setattr, registro, "normalizza", originale)

        self.assertEqual(inspect_sources.normalized("Cod.Art."), "una_sola_implementazione")

    # -- quello che il manifest deve portare --------------------------------

    def test_il_profilo_del_foglio_guadagna_header_rows_senza_perdere_niente(self) -> None:
        """Chi legge il manifest oggi continua a trovare quello che trovava."""
        listino = scrivi_listino_noce(
            self.radice / "formattato_104233.xls", RIGHE_DI_PROVA, RIGHE_IN_CODA)

        foglio = inspect_sources.profile_file(listino)["details"]["sheets"][0]

        for chiave in ("header_candidates", "columns", "samples", "active_range"):
            with self.subTest(chiave=chiave):
                self.assertIn(chiave, foglio)
        righe = foglio["header_rows"]
        # Le righe si consegnano come sono, senza sceglierne nessuna: la riga 4
        # è la nota sul grassetto e intestazione non è, ma a dirlo è il
        # registro confrontando le obbligatorie, non questo elenco.
        self.assertEqual([riga["row"] for riga in righe][:3], [4, 5, 6])
        self.assertIn("i prezzi offerta sono in grassetto", righe[0]["values"])
        self.assertIn("codice_a_barre", righe[1]["values"])

    def test_un_intestazione_oltre_la_ventesima_riga_non_sparisce(self) -> None:
        """Nessun documento che si riconosceva ieri deve smettere oggi.

        Prima di questa fase il riconoscimento guardava le righe scelte da
        `header_candidates`, che arrivano fino alla cinquantesima. Consegnare
        al registro solo le prime venti farebbe sparire un fornitore con un
        preambolo lungo, e nessuno se ne accorgerebbe: il listino diventerebbe
        semplicemente «da interpretare».
        """
        percorso = self.radice / "listino_con_preambolo.xlsx"
        preambolo = [[f"Condizioni di vendita, comma {numero}"] for numero in range(1, 26)]
        save_workbook(percorso, preambolo + [
            INTESTAZIONI_BETULLA,
            ["8000000000001", "C-001", None, "Prodotto Alfa", 6, 1.25, 60, 22, 0],
        ], "Listino")

        foglio = inspect_sources.profile_file(percorso)["details"]["sheets"][0]
        hint = self.hint(percorso)

        self.assertEqual(hint["state"], "SCHEMA_NOTO")
        self.assertEqual(hint["adapter_id"], "betulla_v1")
        self.assertEqual(hint["signature"]["header_row"], 26)
        numeri = [riga["row"] for riga in foglio["header_rows"]]
        # Le prime venti ci sono tutte, e la ventiseiesima è arrivata perché la
        # riconosceva già `header_candidates`.
        self.assertEqual(numeri[:inspect_sources.RIGHE_PER_IL_REGISTRO], list(range(1, 21)))
        self.assertIn(26, numeri)

    def test_il_hint_conserva_le_chiavi_del_server_e_ne_aggiunge_quattro(self) -> None:
        """`app/server.py` e `app/launcher.py` leggono state, adapter_id,
        confidence ed evidence: toglierne una spegnerebbe la pagina senza
        nessun errore da leggere."""
        listino = scrivi_listino_noce(
            self.radice / "formattato_104233.xls", RIGHE_DI_PROVA, RIGHE_IN_CODA)
        profili = self.radice / "profiles.json"

        esegui_script_riuscito("inspect_sources.py", listino, "--output", profili)

        hint = json.loads(profili.read_text(encoding="utf-8"))["profiles"][0]["deterministic_hint"]
        self.assertEqual(sorted(hint), ["adapter_id", "checks", "confidence", "evidence",
                                        "missing_headers", "signature", "state", "unknown_headers"])
        self.assertEqual(hint["state"], "SCHEMA_NOTO")
        self.assertEqual(hint["adapter_id"], "noce_xls_v1")
        self.assertEqual(hint["confidence"], 0.99)
        self.assertEqual(hint["signature"]["sheet"], "Foglio1")
        self.assertEqual(hint["signature"]["header_row"], 5)
        self.assertEqual(hint["signature"]["data_start_row"], 6)
        self.assertEqual(len(hint["signature"]["hash"]), 64)
        self.assertEqual([verifica["name"] for verifica in hint["checks"]],
                         ["colonne_attese", "riga_intestazione", "foglio", "righe_dati",
                          "tipi_plausibili", "posizioni_intestazioni"])
        self.assertTrue(all(verifica["ok"] for verifica in hint["checks"]))
        self.assertEqual(hint["missing_headers"], [])

    def test_una_colonna_mappata_che_sparisce_si_legge_nel_manifest(self) -> None:
        """L'utente non guarda i log: se il listino cambia deve trovarlo scritto
        nel manifest, con il nome della verifica che non è passata."""
        intestazioni = tuple(nome for nome in INTESTAZIONI_NOCE if nome != "Iva")
        listino = scrivi_listino_noce(
            self.radice / "formattato_104233.xls", RIGHE_DI_PROVA, RIGHE_IN_CODA, intestazioni)
        profili = self.radice / "profiles.json"

        esegui_script_riuscito("inspect_sources.py", listino, "--output", profili)

        hint = json.loads(profili.read_text(encoding="utf-8"))["profiles"][0]["deterministic_hint"]
        self.assertEqual(hint["state"], "SCHEMA_VARIATO")
        self.assertEqual(hint["adapter_id"], "noce_xls_v1")
        # Da R4 la firma di noce dichiara anche le posizioni: una colonna
        # sparita fa fallire due verifiche, e tutte e due stanno nel manifest.
        self.assertEqual([verifica["name"] for verifica in hint["checks"] if not verifica["ok"]],
                         ["colonne_attese", "posizioni_intestazioni"])
        self.assertIn("iva", " ".join(hint["evidence"]))

    def test_un_listino_travestito_da_un_altro_fornitore_resta_quello_che_e(self) -> None:
        """Noce manda «formattato_104233.xls»: il nome non è mai una prova,
        nemmeno quando sembra dire tutto. Qui il file si chiama come il listino
        BETULLA, finisce per .xlsx, e dentro è un .xls di Noce."""
        travestito = scrivi_listino_noce(
            self.radice / "LISTINO BETULLA VALIDO FINO AL 28-07-26.xlsx", RIGHE_DI_PROVA, RIGHE_IN_CODA)
        profili = self.radice / "profiles.json"

        esegui_script_riuscito("inspect_sources.py", travestito, "--output", profili)

        profilo = json.loads(profili.read_text(encoding="utf-8"))["profiles"][0]
        self.assertEqual(profilo["declared_suffix"], ".xlsx")
        self.assertEqual(profilo["content_format"], "xls")
        self.assertEqual(profilo["deterministic_hint"]["adapter_id"], "noce_xls_v1")
        self.assertEqual(profilo["deterministic_hint"]["state"], "SCHEMA_NOTO")


# Un listino con un blocco promozionale davanti, come QUERCIA: le righe 3-4 sono
# valorizzazioni di omaggi e non prezzi d'acquisto, la riga 5 dice «LISTINO» e
# i prodotti veri cominciano alla 6.  La settimana dopo il blocco e' piu'
# lungo, e il numero di riga della volta scorsa taglierebbe nel punto
# sbagliato: e' l'unica cosa che questi collaudi guardano.
INTESTAZIONI_CON_BLOCCO = ["Articolo", "EAN", "Descrizione", "Imballo", "Prezzo", "Ordine"]
RIGHE_PROMOZIONALI = [
    ["OMA1", "8000000000901", "OMAGGIO PANNO MULTIUSO", 30, 1.05, None],
    ["OMA2", "8000000000902", "OMAGGIO STRAP CERETTA", 12, 0.61, None],
]
RIGHE_DI_LISTINO = [
    ["FAT443", "8009150351279", "AXO CANDEGGINA 1 LT CLASSICA", 18, 0.94, None],
    ["FAT512", "8009640002209", "AXO CANDEGGINA 2,8 LT", 6, 1.45, None],
    ["FAT7019", "8009234403450", "AXO CANDEGGINA 3 LT NEW", 6, 2.18, None],
]

MAPPATURA_CON_MARCATORE: dict[str, object] = {
    "sheet": "Sheet1",
    "header_row": 1,
    # Il numero c'e' e resta: e' la riga a cui il marcatore si e' risolto la
    # volta che l'utente ha confermato lo schema, e serve a chi scrive la copia
    # dell'ordine.  Chi legge non deve guardarlo mai, ed e' quello che i
    # collaudi qui sotto verificano.
    "data_start_row": 5,
    "data_start_marker": {"column": "A", "equals": "LISTINO", "offset": 1},
    "columns": {"supplier_code": "Articolo", "ean": "EAN", "description": "Descrizione",
                "pieces_per_carton": "Imballo", "unit_price_net": "Prezzo"},
    "order_column": "F",
    "assume_available": True,
    "vat_unavailable": True,
}


def scrivi_listino_con_blocco(percorso: Path, promozionali: list[list[object]],
                              separatore: str = "LISTINO") -> Path:
    """Intestazione, un blocco promozionale di lunghezza variabile, il separatore, i prodotti."""

    righe: list[list[object]] = [list(INTESTAZIONI_CON_BLOCCO)]
    righe.extend([list(riga) for riga in promozionali])
    righe.append([separatore, None, None, None, None, None])
    righe.extend([list(riga) for riga in RIGHE_DI_LISTINO])
    save_workbook(percorso, righe)
    return percorso


class InizioDeiDatiDichiaratoTests(unittest.TestCase):
    """«I dati cominciano dopo la riga che dice X» invece di «alla riga 69».

    Il numero non sopravvive a una settimana: su QUERCIA le righe 7-67 sono un
    blocco promozionale che cambia lunghezza, e il listino vero comincia dopo
    l'unico `A68 = 'LISTINO'` del file.  Un `data_start_row` congelato porta
    dentro righe fantasma, o ne perde di vere, **senza dire niente**.
    """

    maxDiff = None

    def setUp(self) -> None:
        self.temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporanea.cleanup)
        self.radice = Path(self.temporanea.name)

    def descrizioni(self, records: list[dict[str, object]]) -> list[str]:
        return [str(record["description"]) for record in records]

    def test_il_marcatore_taglia_dove_comincia_il_listino(self) -> None:
        listino = scrivi_listino_con_blocco(self.radice / "listino.xlsx", RIGHE_PROMOZIONALI)

        records, _avvisi = read_mapped_xlsx_supplier(listino, "quercia", MAPPATURA_CON_MARCATORE)

        self.assertEqual(len(records), 3)
        self.assertEqual(self.descrizioni(records)[0], "AXO CANDEGGINA 1 LT CLASSICA")
        self.assertEqual(records[0]["source_row"], 5)

    def test_il_blocco_che_si_allunga_non_sposta_il_taglio(self) -> None:
        """La prova che il numero non basta: due righe promozionali in piu'.

        Con il solo `data_start_row` della settimana scorsa entrerebbero nel
        listino delle valorizzazioni di omaggi come se fossero prezzi
        d'acquisto, e nessuno lo vedrebbe.
        """

        piu_lungo = [*RIGHE_PROMOZIONALI,
                     ["OMA3", "8000000000903", "OMAGGIO LEOPELLE FANGO", 6, 1.55, None],
                     ["OMA4", "8000000000904", "OMAGGIO BAYZON TRAPPOLA", 12, 2.68, None]]
        listino = scrivi_listino_con_blocco(self.radice / "listino.xlsx", piu_lungo)

        records, _avvisi = read_mapped_xlsx_supplier(listino, "quercia", MAPPATURA_CON_MARCATORE)

        self.assertEqual(len(records), 3)
        self.assertNotIn("OMAGGIO BAYZON TRAPPOLA", self.descrizioni(records))
        self.assertEqual(records[0]["source_row"], 7)

        # E senza marcatore, sullo stesso file, il difetto si vede: il numero
        # della volta scorsa fa entrare gli omaggi.
        senza = {chiave: valore for chiave, valore in MAPPATURA_CON_MARCATORE.items()
                 if chiave != "data_start_marker"}
        col_numero, _avvisi = read_mapped_xlsx_supplier(listino, "quercia", senza)
        self.assertIn("OMAGGIO BAYZON TRAPPOLA", self.descrizioni(col_numero))
        self.assertGreater(len(col_numero), 3)

    def test_un_marcatore_che_non_c_e_ferma_la_lettura_e_dice_che_cosa_cercava(self) -> None:
        listino = scrivi_listino_con_blocco(self.radice / "listino.xlsx", RIGHE_PROMOZIONALI,
                                            separatore="PREZZI")

        with self.assertRaises(ValueError) as errore:
            read_mapped_xlsx_supplier(listino, "quercia", MAPPATURA_CON_MARCATORE)

        messaggio = str(errore.exception)
        self.assertIn("«LISTINO»", messaggio)
        self.assertIn("colonna A", messaggio)
        self.assertIn("listino.xlsx", messaggio)

    def test_un_marcatore_che_compare_due_volte_ferma_la_lettura(self) -> None:
        """Due tagli possibili non sono un taglio: sceglierne uno sarebbe indovinare."""

        righe = [list(INTESTAZIONI_CON_BLOCCO),
                 ["LISTINO", None, None, None, None, None],
                 *[list(riga) for riga in RIGHE_PROMOZIONALI],
                 ["LISTINO", None, None, None, None, None],
                 *[list(riga) for riga in RIGHE_DI_LISTINO]]
        listino = self.radice / "listino.xlsx"
        save_workbook(listino, righe)

        with self.assertRaises(ValueError) as errore:
            read_mapped_xlsx_supplier(listino, "quercia", MAPPATURA_CON_MARCATORE)

        messaggio = str(errore.exception)
        self.assertIn("2 righe", messaggio)
        self.assertIn("righe 2, 5", messaggio)

    def test_il_marcatore_non_ripiega_mai_sul_numero_di_riga(self) -> None:
        """⚠ La difesa vera: `data_start_row` e' scritto e valido, e non si usa.

        Ripiegare in silenzio sul numero della settimana scorsa e' esattamente
        il difetto che il marcatore chiude: la lettura si ferma.
        """

        listino = scrivi_listino_con_blocco(self.radice / "listino.xlsx", RIGHE_PROMOZIONALI,
                                            separatore="PREZZI")

        with self.assertRaises(ValueError):
            read_mapped_xlsx_supplier(listino, "quercia", MAPPATURA_CON_MARCATORE)

        # Lo stesso documento, con il solo numero, si legge benissimo: la
        # fermata non viene da un file illeggibile.
        senza = {chiave: valore for chiave, valore in MAPPATURA_CON_MARCATORE.items()
                 if chiave != "data_start_marker"}
        records, _avvisi = read_mapped_xlsx_supplier(listino, "quercia", senza)
        self.assertEqual(len(records), 3)

    def test_contains_serve_quando_la_scritta_porta_la_data_della_settimana(self) -> None:
        """`equals` non basta sempre: «CANVASS 33-34 07-21ago» cambia ogni settimana."""

        listino = scrivi_listino_con_blocco(self.radice / "listino.xlsx", RIGHE_PROMOZIONALI,
                                            separatore="LISTINO VALIDO DAL 07/08 AL 21/08")
        mappatura = {**MAPPATURA_CON_MARCATORE,
                     "data_start_marker": {"column": "A", "contains": "LISTINO VALIDO", "offset": 1}}

        records, _avvisi = read_mapped_xlsx_supplier(listino, "quercia", mappatura)

        self.assertEqual(len(records), 3)
        self.assertEqual(records[0]["source_row"], 5)
        self.assertEqual(self.descrizioni(records)[0], "AXO CANDEGGINA 1 LT CLASSICA")

    def test_il_marcatore_vale_anche_per_un_csv(self) -> None:
        percorso = self.radice / "listino.csv"
        percorso.write_text(
            "Articolo;EAN;Descrizione;Imballo;Prezzo\n"
            "OMA1;8000000000901;OMAGGIO PANNO;30;1,05\n"
            "LISTINO;;;;\n"
            "FAT443;8009150351279;AXO CANDEGGINA 1 LT;18;0,94\n"
            "FAT512;8009640002209;AXO CANDEGGINA 2,8 LT;6;1,45\n",
            encoding="utf-8", newline="\n",
        )
        mappatura = {
            "header_row": 1, "data_start_row": 2, "delimiter": ";", "italian_numbers": True,
            "data_start_marker": {"column": 1, "equals": "LISTINO", "offset": 1},
            "columns": {"supplier_code": "Articolo", "ean": "EAN", "description": "Descrizione",
                        "pieces_per_carton": "Imballo", "unit_price_net": "Prezzo"},
            "assume_available": True, "vat_unavailable": True,
        }

        records, _avvisi = read_mapped_csv_supplier(percorso, "quercia", mappatura)

        self.assertEqual(self.descrizioni(records), ["AXO CANDEGGINA 1 LT", "AXO CANDEGGINA 2,8 LT"])

    def test_un_marcatore_scritto_male_si_ferma_prima_di_leggere(self) -> None:
        listino = scrivi_listino_con_blocco(self.radice / "listino.xlsx", RIGHE_PROMOZIONALI)
        for marcatore, atteso in (
            ({"column": "A"}, "equals"),
            ({"column": "A", "equals": "LISTINO", "contains": "LIST"}, "uno solo dei due"),
            ({"column": "??", "equals": "LISTINO"}, "column"),
            ({"column": "A", "equals": "LISTINO", "offset": -1}, "offset"),
        ):
            with self.subTest(marcatore=marcatore):
                mappatura = {**MAPPATURA_CON_MARCATORE, "data_start_marker": marcatore}
                with self.assertRaises(ValueError) as errore:
                    read_mapped_xlsx_supplier(listino, "quercia", mappatura)
                self.assertIn(atteso, str(errore.exception))

    def test_il_validatore_accetta_il_marcatore_al_posto_del_numero(self) -> None:
        """Una mappatura scritta a mano può dichiarare solo la regola."""

        from validate_input_manifest import incomplete_mapping

        mappatura = {chiave: valore for chiave, valore in MAPPATURA_CON_MARCATORE.items()
                     if chiave != "data_start_row"}
        self.assertEqual(incomplete_mapping(mappatura, "supplier", Path("listino.xlsx")), [])

        senza_niente = {chiave: valore for chiave, valore in mappatura.items()
                        if chiave != "data_start_marker"}
        self.assertIn("data_start_row oppure data_start_marker",
                      incomplete_mapping(senza_niente, "supplier", Path("listino.xlsx")))

    def test_un_marcatore_malfatto_non_passa_per_una_dichiarazione_buona(self) -> None:
        """Senza questo, bastava scrivere `data_start_marker: {}` per saltare il controllo."""

        from validate_input_manifest import incomplete_mapping

        mappatura = {chiave: valore for chiave, valore in MAPPATURA_CON_MARCATORE.items()
                     if chiave != "data_start_row"}
        mappatura["data_start_marker"] = {"column": "A"}
        mancanti = incomplete_mapping(mappatura, "supplier", Path("listino.xlsx"))
        self.assertTrue(any("equals" in voce for voce in mancanti), mancanti)



class SenzaListiniLoDiceSubitoTests(unittest.TestCase):
    """«Nessun listino fornitore» era un avviso, e la fase dopo moriva.

    La validazione dichiarava il manifest valido; `prepare_manifest_sources`
    sollevava «Nessun fornitore incluso nel manifest» e usciva 1; in pagina
    arrivava la frase generica del passo fallito — «il problema è in uno dei
    documenti caricati» — a chi aveva caricato un documento solo, e giusto. La
    cosa da fare la sapeva già la fase prima.
    """

    def test_manca_il_listino_e_la_validazione_lo_dice(self) -> None:
        with tempfile.TemporaryDirectory() as temporaneo:
            radice = Path(temporaneo)
            gestionale = radice / "gestionale.xlsx"
            gestionale.write_text("non conta", encoding="utf-8")
            manifest = radice / "input_manifest.json"
            manifest.write_text(json.dumps({"files": [
                {"file_name": gestionale.name, "path": str(gestionale), "ai_preflight": {
                    "state": "SCHEMA_NOTO", "role": "master",
                    "adapter_id": "gestionale_v1", "rationale": "prova"}},
            ]}, ensure_ascii=False), encoding="utf-8")
            uscita = radice / "manifest_validation.json"

            esito_processo = esegui_script("validate_input_manifest.py", "--manifest", manifest,
                                           "--adapters", ADAPTERS, "--output", uscita)
            esito = json.loads(uscita.read_text(encoding="utf-8"))

        self.assertEqual(esito_processo.returncode, 2)
        self.assertFalse(esito["valid"])
        self.assertEqual([voce["code"] for voce in esito["errors"]], ["NESSUN_FORNITORE"])
        # E la frase dice che cosa fare, non che cosa manca a un file JSON.
        self.assertIn("carica i listini di questa settimana", esito["errors"][0]["message"])
        self.assertEqual(esito["warnings"], [])


class RegistroImparatoNelValidatoreTests(unittest.TestCase):
    """Il controllo della fase 3 deve vedere il registro VERO, non mezzo.

    Il difetto che queste prove chiudono, misurato il 22 agosto 2026: il
    validatore leggeva i soli adattatori spediti con un `json.load` suo, e
    bocciava con `ADATTATORE_NON_VALIDO` ogni documento riconosciuto grazie a
    un adattatore imparato su questo computer — cioe' tutti i fornitori
    insegnati dalla mappatura guidata, e da quando esiste il suffisso
    `__locale` anche ogni fornitore spedito a cui si e' spostata la colonna
    d'ordine. Un errore del manifest e' una fermata: il confronto della
    settimana non si faceva, e dalla pagina non c'era niente da premere.
    """

    maxDiff = None

    def registro_di_prova(self, radice: Path, imparati: list[dict] | None) -> Path:
        """Una copia dello spedito, con accanto l'imparato: come nell'installazione vera."""

        spedito = json.loads(ADAPTERS.read_text(encoding="utf-8"))
        percorso = radice / "adapters.json"
        percorso.write_text(json.dumps(spedito, ensure_ascii=False), encoding="utf-8")
        if imparati is not None:
            (radice / "adattatori_imparati.json").write_text(
                json.dumps({"adapters": imparati}, ensure_ascii=False), encoding="utf-8"
            )
        return percorso

    def voce_spedita(self, identificativo: str) -> dict:
        spedito = json.loads(ADAPTERS.read_text(encoding="utf-8"))
        return next(voce for voce in spedito["adapters"] if voce["id"] == identificativo)

    def esito(self, radice: Path, adattatore: str, imparati: list[dict] | None) -> dict:
        """Il verdetto del validatore su un manifest che dichiara quell'adattatore."""

        registro_path = self.registro_di_prova(radice, imparati)
        gestionale = radice / "gestionale.xlsx"
        listino = radice / "listino.xlsx"
        for documento in (gestionale, listino):
            documento.write_text("non conta: il validatore guarda che il file esista", encoding="utf-8")
        manifest = radice / "input_manifest.json"
        manifest.write_text(json.dumps({"files": [
            {"file_name": gestionale.name, "path": str(gestionale), "ai_preflight": {
                "state": "SCHEMA_NOTO", "role": "master",
                "adapter_id": "gestionale_v1", "rationale": "prova"}},
            {"file_name": listino.name, "path": str(listino), "ai_preflight": {
                "state": "SCHEMA_NOTO", "role": "supplier", "supplier_id": "prova",
                "adapter_id": adattatore, "rationale": "prova"}},
        ]}, ensure_ascii=False), encoding="utf-8")
        uscita = radice / "manifest_validation.json"
        esegui_script("validate_input_manifest.py", "--manifest", manifest,
                      "--adapters", registro_path, "--output", uscita)
        return json.loads(uscita.read_text(encoding="utf-8"))

    def test_un_fornitore_imparato_qui_passa_la_validazione(self) -> None:
        """La mappatura guidata insegna «quercia_v1», che nello spedito non c'e'."""

        with tempfile.TemporaryDirectory() as temporaneo:
            radice = Path(temporaneo)
            imparato = {**self.voce_spedita("betulla_v1"), "id": "quercia_v1", "supplier_id": "quercia"}
            esito = self.esito(radice, "quercia_v1", [imparato])
            self.assertTrue(esito["valid"], esito["errors"])
            self.assertEqual(esito["errors"], [])

    def test_una_colonna_spostata_non_ferma_il_confronto_della_settimana_dopo(self) -> None:
        """Spostare la colonna d'ordine di BETULLA scrive «betulla_v1__locale»."""

        with tempfile.TemporaryDirectory() as temporaneo:
            radice = Path(temporaneo)
            imparato = {**self.voce_spedita("betulla_v1"), "id": "betulla_v1__locale"}
            esito = self.esito(radice, "betulla_v1__locale", [imparato])
            self.assertTrue(esito["valid"], esito["errors"])

    def test_l_imparato_sparito_lascia_valere_lo_spedito(self) -> None:
        """Un «__locale» senza la sua voce e' Betulla: il documento resta leggibile."""

        with tempfile.TemporaryDirectory() as temporaneo:
            esito = self.esito(Path(temporaneo), "betulla_v1__locale", None)
            self.assertTrue(esito["valid"], esito["errors"])

    def test_un_adattatore_che_non_esiste_resta_un_errore(self) -> None:
        """Il cancello non si e' aperto: un nome inventato si ferma ancora qui."""

        with tempfile.TemporaryDirectory() as temporaneo:
            esito = self.esito(Path(temporaneo), "pippo_v1", None)
            self.assertFalse(esito["valid"])
            self.assertEqual([voce["code"] for voce in esito["errors"]], ["ADATTATORE_NON_VALIDO"])


if __name__ == "__main__":
    unittest.main()
