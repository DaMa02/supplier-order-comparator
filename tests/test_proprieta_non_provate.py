#!/usr/bin/env python3
"""Le quattro proprietà che il codice dichiara e nessun test dimostrava.

Vengono dal revisore d'integrità dei test del 14 agosto 2026 — quello che ha
reso di più, perché le mutazioni verdi trovano i test che *sembrano* coprire e
non coprono. Erano rimaste in coda come «minori», e minori lo sono: nessuna di
loro è un difetto. Sono quattro affermazioni scritte nei commenti del programma
che, se domani smettessero di essere vere, la suite non se ne accorgerebbe.

1. **`active_rows` richiude il documento.** Il commento dice perché conta: «su
   Windows quel listino non si può più eliminare né sostituire», e
   `catalog_search` chiama quei lettori **dentro** il servizio, non in un
   sottoprocesso che muore. È un difetto già successo una volta.
2. **Il writer dichiara il nome leggibile del fornitore**, non l'identificativo
   tecnico: è il modo in cui `NUOVO_FORNITORE` è finito sotto gli occhi
   dell'utente.
3. **`copia_fedele` con quantità zero.** Il ramo è oggi irraggiungibile — chi
   costruisce il piano scarta le righe a zero colli — ma il commento dichiara
   quale regola vale se ci arrivasse, e vale la pena che sia quella scritta.
4. **Un espositore senza pezzi dichiarati vale un pezzo**, ed è il ripiego
   prudente: fa sembrare l'offerta più cara, non più conveniente. Con un numero
   negativo la prudenza deve valere uguale.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

RADICE = Path(__file__).resolve().parents[1]
for cartella in (RADICE / "app", RADICE / "scripts"):
    if str(cartella) not in sys.path:
        sys.path.insert(0, str(cartella))

import copia_fedele  # noqa: E402
import prepare_sources  # noqa: E402
from build_review_data import display_offer  # noqa: E402


class UnListinoLettoSiPuoAncoraCancellare(unittest.TestCase):
    """La proprietà vera di `active_rows`: richiude il documento dopo averlo letto.

    Le prime due prove la misurano cancellando o sostituendo il file, cioè
    come si misura su **Windows**: lì un file rimasto aperto non si può più
    né eliminare né sostituire, e sono la prova del gesto vero quando girano
    sul PC del negozio. Su macOS (e su Linux) `unlink()` e `replace()`
    riescono comunque su un file aperto, quindi **fuori da Windows queste due
    prove non possono fallire**, indipendentemente da come si comporta
    davvero `active_rows`. La quarta prova, sotto, misura la stessa proprietà
    in modo indipendente dal sistema operativo.
    """

    def setUp(self) -> None:
        temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(temporanea.cleanup)
        self.radice = Path(temporanea.name)

    def listino(self, nome: str = "listino.xlsx") -> Path:
        percorso = self.radice / nome
        libro = Workbook()
        pagina = libro.active
        pagina.title = "Listino"
        pagina.append(["EAN", "Descrizione"])
        pagina.append(["8000000000001", "SAPONE"])
        libro.save(percorso)
        libro.close()
        return percorso

    def test_dopo_la_lettura_il_file_si_elimina(self) -> None:
        percorso = self.listino()

        _foglio, righe = prepare_sources.active_rows(percorso)

        self.assertEqual(len(righe), 2)
        percorso.unlink()
        self.assertFalse(percorso.exists())

    def test_e_si_sostituisce_con_quello_della_settimana_dopo(self) -> None:
        """È il gesto vero: il listino nuovo prende il posto del vecchio mentre
        il servizio è acceso e quel documento l'ha già letto."""

        percorso = self.listino()
        nuovo = self.listino("listino_nuovo.xlsx")

        prepare_sources.active_rows(percorso)

        nuovo.replace(percorso)
        self.assertTrue(percorso.is_file())

    def test_le_righe_sono_gia_tutte_lette_non_un_generatore(self) -> None:
        """Un generatore pigro passa i test che lo scorrono subito e lascia il
        file aperto per tutta la vita del processo: è esattamente il difetto di
        prima, e si vede solo guardando che cosa viene restituito."""

        percorso = self.listino()

        _foglio, righe = prepare_sources.active_rows(percorso)

        self.assertIsInstance(righe, list)

    def test_il_documento_resta_chiuso_dopo_la_lettura(self) -> None:
        """Misura la chiusura senza dipendere da un comportamento di Windows.

        `_archive` è l'oggetto `zipfile.ZipFile` con cui openpyxl tiene aperto
        l'.xlsx (che è tecnicamente uno zip): finché il documento è aperto il
        suo `fp` — il lettore sul file — è vivo; una volta chiuso torna
        `None`. È un attributo privato, e lo si usa perché openpyxl non
        espone un modo pubblico per chiedere se un workbook è ancora aperto.
        """

        percorso = self.listino()

        foglio, _righe = prepare_sources.active_rows(percorso)

        self.assertIsNone(foglio.parent._archive.fp, "il documento è rimasto aperto")


class IlNomeCheIlWriterDichiara(unittest.TestCase):
    """Il nome leggibile del fornitore viaggia **dentro la regola di
    scrittura**, e viene dal registro.

    Serve perché il writer Node non legge il registro — la configurazione è il
    suo unico ingresso dichiarato — e senza questo si ritrovava a stampare
    l'identificativo tecnico, con l'underscore, nei messaggi e nei nomi dei file
    d'ordine: è così che `NUOVO_FORNITORE` è finito sotto gli occhi dell'utente.
    """

    def setUp(self) -> None:
        temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(temporanea.cleanup)
        self.radice = Path(temporanea.name)
        import launcher
        import registro

        self.launcher = launcher
        self.percorso_registro = self.radice / "adapters.json"
        for modulo, attributo in ((launcher, "ADAPTERS_PATH"), (registro, "REGISTRO")):
            originale = getattr(modulo, attributo)
            setattr(modulo, attributo, self.percorso_registro)
            self.addCleanup(setattr, modulo, attributo, originale)

    def listino(self) -> Path:
        percorso = self.radice / "listino.xlsx"
        libro = Workbook()
        pagina = libro.active
        pagina.title = "Listino"
        pagina.append(["EAN", "Descrizione", None])
        pagina.append(["8000000000001", "SAPONE", None])
        libro.save(percorso)
        libro.close()
        return percorso

    def scrivi_registro(self, display_name: str | None) -> dict:
        """Scrive l'adattatore nel registro. `display_name=None` lo dichiara
        senza quella chiave — è il caso del fornitore imparato che il
        registro non ha ancora imparato a chiamare per nome."""

        import json

        adattatore = {
            "id": "nuovo_v1",
            "supplier_id": "fornitore_nuovo",
            "kind": "supplier",
            "order_write": {
                "sheet": "FIRST",
                "header_row": 1,
                "data_start_row": 2,
                "order_column": "C",
                "allow_blank_header_if_confirmed": True,
                "order_header_blank_confirmed": True,
            },
        }
        if display_name is not None:
            adattatore["display_name"] = display_name
        self.percorso_registro.write_text(
            json.dumps({"schema_version": 1, "adapters": [adattatore]}, ensure_ascii=False),
            encoding="utf-8",
        )
        return adattatore

    def test_la_regola_porta_il_nome_dichiarato_dal_registro(self) -> None:
        adattatore = self.scrivi_registro("Fornitore Nuovo")

        regola, motivo = self.launcher.source_rule(
            "fornitore_nuovo", self.listino(), {}, adattatore,
        )

        self.assertIsNone(motivo)
        self.assertEqual(regola["display_name"], "Fornitore Nuovo")

    def test_senza_display_name_dichiarato_la_regola_porta_il_nome_leggibile(self) -> None:
        """Il ripiego misurato nel punto in cui conta davvero: `source_rule()`.

        La prova che stava qui prima (`assertNotIn("_", …)`) non poteva
        fallire mai: usava la stessa preparazione della prova sopra, che
        asserisce già l'uguaglianza esatta con `"Fornitore Nuovo"` — e
        quell'uguaglianza implica da sola l'assenza del trattino. Era verde
        per costruzione ogni volta che la prima lo era.

        Il ripiego vero — che scatta quando il registro **non dichiara**
        un `display_name` — è già provato altrove, a livello del solo nome:
        `tests/test_web_app.py`, classe `IlRegistroEUnaVeritaSolaTests`
        (`test_senza_dichiarazione_l_underscore_non_arriva_all_utente`), e
        `tests/test_registro_impronte.py`, classe
        `IlNomeDelFornitoreInOgniFrase`
        (`test_chi_non_e_dichiarato_non_esce_con_gli_underscore` e
        `test_le_frasi_del_lanciatore_usano_la_stessa_regola`). Quello che
        mancava è lo stesso caso attraverso `source_rule()`: è l'unico punto
        in cui il nome esce dentro la regola consegnata al writer Node, ed è
        lì che `NUOVO_FORNITORE` era finito sotto gli occhi dell'utente.
        """

        adattatore = self.scrivi_registro(None)

        regola, motivo = self.launcher.source_rule(
            "fornitore_nuovo", self.listino(), {}, adattatore,
        )

        self.assertIsNone(motivo)
        self.assertEqual(regola["display_name"], "FORNITORE NUOVO")


class LaCopiaConQuantitaZero(unittest.TestCase):
    """Il ramo `attesa == 0`: oggi non ci arriva nessuno, e la regola che vale
    se ci arrivasse è dichiarata nel codice. Se cambiasse, adesso si vede."""

    def setUp(self) -> None:
        temporanea = tempfile.TemporaryDirectory()
        self.addCleanup(temporanea.cleanup)
        self.radice = Path(temporanea.name)

    def coppia(self, valore_copia) -> tuple[Path, Path]:
        originale = self.radice / "originale.xlsx"
        copia = self.radice / "copia.xlsx"
        for percorso, valore in ((originale, None), (copia, valore_copia)):
            libro = Workbook()
            pagina = libro.active
            pagina.title = "Listino"
            pagina.append(["EAN", "Descrizione", "ORDINE"])
            pagina.append(["8000000000001", "SAPONE", valore])
            libro.save(percorso)
            libro.close()
        return originale, copia

    def test_il_piano_chiede_zero_e_la_copia_mostra_zero(self) -> None:
        originale, copia = self.coppia(0)

        esito = copia_fedele.confronta_copia(
            originale, copia, colonna_ordine="C", prima_riga=2, quantita={2: 0},
        )

        self.assertEqual(esito.righe_mancanti, [])
        self.assertEqual(esito.quante_rifiutate, 0)

    def test_il_piano_chiede_zero_e_la_copia_mostra_altro(self) -> None:
        """La copia deve mostrare quello che il piano chiede: si ferma, invece
        di partire con una cella che non corrisponde."""

        originale, copia = self.coppia(5)

        esito = copia_fedele.confronta_copia(
            originale, copia, colonna_ordine="C", prima_riga=2, quantita={2: 0},
        )

        self.assertEqual(esito.righe_mancanti, [2])


class UnEspositoreSenzaPezziDichiarati(unittest.TestCase):
    """Il ripiego prudente: senza pezzi dichiarati l'espositore vale **un**
    pezzo, così l'offerta sembra più cara e non più conveniente. Un numero
    negativo è un dato altrettanto inutilizzabile, e deve ricevere la stessa
    prudenza — non un fattore negativo, che ribalterebbe il confronto."""

    BASE = {
        "supplier": "larice",
        "net_price_per_display": 24.0,
        "confidence": "ALTA",
        "auto_confirmed": True,
    }

    def test_senza_pezzi_dichiarati_vale_un_pezzo(self) -> None:
        offerta = display_offer({**self.BASE})

        self.assertEqual(offerta["quantityFactor"], 1)
        self.assertEqual(offerta["unitPriceNet"], 24.0)

    def test_con_pezzi_dichiarati_negativi_vale_un_pezzo_lo_stesso(self) -> None:
        offerta = display_offer({**self.BASE, "declared_units": -12})

        self.assertEqual(offerta["quantityFactor"], 1)
        self.assertEqual(offerta["unitPriceNet"], 24.0)

    def test_con_pezzi_dichiarati_veri_il_prezzo_e_quello_del_pezzo(self) -> None:
        offerta = display_offer({**self.BASE, "declared_units": 12})

        self.assertEqual(offerta["quantityFactor"], 12)
        self.assertEqual(offerta["unitPriceNet"], 2.0)
        self.assertEqual(offerta["orderUnitPriceNet"], 24.0)


if __name__ == "__main__":
    unittest.main()
