#!/usr/bin/env python3
"""Tests for the confirmation store: what the user has already answered.

This module is memory that survives the weekly recompute, so its bugs don't
show up the day they're introduced. They show up the following week, when a
confirmation applies to the wrong item or stops applying to the right one.
Tests cover both directions of error, which don't cost the same:

- losing a confirmation costs the user one click (tolerable).
- applying a wrong one buys the wrong stock, silently, every week
  (must never happen).

Test data comes from real supplier exports: the DOPLO the user reconfirms by
hand, the DIXOR that appears twice in the same list, and the Noce item codes
that all changed at once when its price list moved from the site's CSV to
their `.xls`.

No test reads `app/data/`: the app rewrites those files itself, so a suite
built on them would pass or fail depending on the last run. Real values are
kept here as constants.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

RADICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RADICE / "scripts"))
sys.path.insert(0, str(RADICE / "app"))

import conferme  # noqa: E402
from conferme import (  # noqa: E402
    MagazzinoConferme,
    MagazzinoNonUtilizzabile,
    impronta_articolo,
    impronta_prodotto,
)


# The case that motivated this module: the management-software row and the
# Noce offer the user confirmed by hand, which kept getting re-asked every week.
PRODOTTO_DOPLO = {
    "id": "product:539",
    "ean": "8009721709065",
    "name": "DOPLO PIATTI DESSER 25PZ",
    "sourceRow": 539,
    "lastUnitPrice": 1.24,
}
# The same Noce item as seen in two price lists: the site's CSV (empty item
# code) and their `.xls` (item code filled in). Same item, two different
# fingerprints — this is the case that decides what `cerca` has to do.
OFFERTA_NOCE_CSV = {
    "supplierId": "noce",
    "ean": "8009721709065",
    "supplierCode": "",
    "description": "Doplo piatto frutta 17 cm 25 pz",
}
OFFERTA_NOCE_XLS = {
    "supplierId": "noce",
    "ean": "8009721709065",
    "supplierCode": "0000000004795",
    "description": "Doplo piatto frutta 17 cm 25 pz",
}
OFFERTA_LARICE = {
    "supplierId": "larice",
    "ean": "8009721709065",
    "supplierCode": "SM4412",
    "description": "DOPLO PIATTO FRUTTA CM.17 PZ.25",
}

ADESSO = "2026-08-15T09:30:00+00:00"
DOPO = "2026-08-22T09:30:00+00:00"
ANCORA_DOPO = "2026-08-29T09:30:00+00:00"


class BaseMagazzino(unittest.TestCase):
    """Gives each test a fresh store file and guarantees it gets closed."""

    def setUp(self) -> None:
        self.cartella = Path(tempfile.mkdtemp(prefix="conferme-"))
        self.percorso = self.cartella / "conferme.db"
        self.magazzino = self.apri()

    def tearDown(self) -> None:
        for magazzino in getattr(self, "_aperti", []):
            magazzino.chiudi()

    def apri(self, percorso: Path | None = None) -> MagazzinoConferme:
        magazzino = MagazzinoConferme(percorso or self.percorso)
        self._aperti = getattr(self, "_aperti", [])
        self._aperti.append(magazzino)
        return magazzino

    def articolo(self, prodotto: dict | None = None) -> str:
        return impronta_prodotto(prodotto or PRODOTTO_DOPLO)

    def conferma(
        self,
        *,
        offerta: dict | None = None,
        fornitore: str = "noce",
        accettata: bool = True,
        motivo: str = "Confermato a mano: è lo stesso piatto.",
        quando: str = ADESSO,
        magazzino: MagazzinoConferme | None = None,
    ) -> None:
        (magazzino or self.magazzino).ricorda(
            fornitore=fornitore,
            articolo=self.articolo(),
            offerta=impronta_articolo(offerta or OFFERTA_NOCE_XLS),
            accettata=accettata,
            motivo=motivo,
            quando=quando,
        )


class IdentitaDelProdotto(unittest.TestCase):
    """`impronta_prodotto`: what goes into the fingerprint and what doesn't."""

    def test_e_fatta_di_codice_a_barre_e_nome(self) -> None:
        self.assertEqual(
            impronta_prodotto(PRODOTTO_DOPLO),
            "8009721709065|DOPLO PIATTI DESSER 25PZ",
        )

    def test_la_riga_e_il_prezzo_non_ci_entrano(self) -> None:
        """The reason this module exists.

        On real exports, 449 of 457 `product:<row>` ids carry a different item
        from one week to the next: if the row were part of the fingerprint, a
        confirmation would follow the position instead of the item. Price
        also changes weekly on the same item.
        """

        settimana_dopo = dict(PRODOTTO_DOPLO, sourceRow=17, lastUnitPrice=1.31, id="product:17")
        self.assertEqual(impronta_prodotto(PRODOTTO_DOPLO), impronta_prodotto(settimana_dopo))

    def test_un_codice_a_barre_riusato_non_eredita_la_conferma(self) -> None:
        """The name is part of the fingerprint on purpose: barcodes here are
        hand-typed. Same code, different item must give different
        fingerprints, or yesterday's confirmation attaches itself to
        something else.
        """

        altro = dict(PRODOTTO_DOPLO, name="DOPLO BICCHIERI 200CC 25PZ")
        self.assertNotEqual(impronta_prodotto(PRODOTTO_DOPLO), impronta_prodotto(altro))

    def test_due_righe_dello_stesso_articolo_hanno_la_stessa_impronta(self) -> None:
        """A real export lists the same DIXOR item twice.

        Same item: a confirmation given on one row applies to the other, and
        treating them as distinct would mean asking the same question twice.
        """

        prima = {"ean": "8019690600935", "name": "DIXOR POLVERE CLASSICO 40MISURINI", "sourceRow": 207}
        seconda = {"ean": "8019690600935", "name": "DIXOR POLVERE CLASSICO 40MISURINI", "sourceRow": 212}
        self.assertEqual(impronta_prodotto(prima), impronta_prodotto(seconda))

    def test_il_nome_si_normalizza_come_nel_resto_del_programma(self) -> None:
        sporco = {"ean": "8009721709065", "name": "  doplo   piatti-desser  25pz "}
        self.assertEqual(impronta_prodotto(sporco), impronta_prodotto(PRODOTTO_DOPLO))

    def test_senza_niente_da_identificare_non_ce_impronta(self) -> None:
        self.assertEqual(impronta_prodotto({}), "")
        self.assertEqual(impronta_prodotto({"ean": "", "name": "  "}), "")
        self.assertEqual(impronta_prodotto(None), "")

    def test_senza_codice_a_barre_resta_il_nome(self) -> None:
        """Six items in real exports have no EAN; they must not vanish."""

        self.assertEqual(impronta_prodotto({"name": "SCOTEX ASCIUGATUTTO"}), "|SCOTEX ASCIUGATUTTO")


class ScritturaERilettura(BaseMagazzino):
    def test_quello_che_si_scrive_si_rilegge(self) -> None:
        self.conferma()
        trovata = self.magazzino.cerca("noce", self.articolo())
        self.assertIsNotNone(trovata)
        assert trovata is not None
        self.assertEqual(trovata["offerta"], impronta_articolo(OFFERTA_NOCE_XLS))
        self.assertTrue(trovata["accettata"])
        self.assertEqual(trovata["motivo"], "Confermato a mano: è lo stesso piatto.")
        self.assertEqual(trovata["valida_dal"], ADESSO)
        self.assertIsNone(trovata["valida_fino_a"])
        self.assertTrue(trovata["in_vigore"])

    def test_la_data_e_quella_del_chiamante(self) -> None:
        """The store has no clock: `quando` comes from the caller and is kept as-is."""

        self.conferma(quando="2026-01-02T03:04:05+00:00")
        trovata = self.magazzino.cerca("noce", self.articolo())
        assert trovata is not None
        self.assertEqual(trovata["valida_dal"], "2026-01-02T03:04:05+00:00")

    def test_dentro_il_magazzino_non_si_chiama_nessun_orologio(self) -> None:
        """A source-level check, the only way to prove an absence.

        A store that timestamps itself can't be tested reproducibly.
        """

        sorgente = (RADICE / "app" / "conferme.py").read_text(encoding="utf-8")
        for vietato in ("datetime.now", "utcnow", "date.today", "time.time", "time.monotonic"):
            self.assertNotIn(vietato, sorgente, f"il magazzino si data da solo con {vietato}")

    def test_un_no_e_una_memoria_come_le_altre(self) -> None:
        """A "not the same item" answer given once must not be re-asked every week."""

        self.conferma(accettata=False, motivo="Il 17 cm non è il dessert.")
        trovata = self.magazzino.cerca("noce", self.articolo())
        assert trovata is not None
        self.assertFalse(trovata["accettata"])

    def test_mai_visto_non_e_un_no(self) -> None:
        """`None` means "no memory of this", and leads to a different screen."""

        self.assertIsNone(self.magazzino.cerca("noce", self.articolo()))
        self.assertIsNone(self.magazzino.cerca("noce", "9999999999999|PRODOTTO MAI VISTO"))
        self.conferma(accettata=False)
        risposta = self.magazzino.cerca("noce", self.articolo())
        self.assertIsNotNone(risposta)
        assert risposta is not None
        self.assertFalse(risposta["accettata"])

    def test_il_fornitore_non_dipende_dalle_maiuscole(self) -> None:
        """`NOCE` and `noce` are the same price list.

        Writing one and searching for the other would make the confirmation
        vanish with no error, looking like "never confirmed".
        """

        self.conferma(fornitore="NOCE")
        self.assertIsNotNone(self.magazzino.cerca("noce", self.articolo()))
        self.assertIsNotNone(self.magazzino.cerca("  Noce ", self.articolo()))

    def test_impronte_vuote_rifiutate(self) -> None:
        """A confirmation attached to `"|||"` would resurface on random items."""

        buona = impronta_articolo(OFFERTA_NOCE_XLS)
        with self.assertRaises(ValueError):
            self.magazzino.ricorda(
                fornitore="noce", articolo="", offerta=buona, accettata=True, quando=ADESSO
            )
        with self.assertRaises(ValueError):
            self.magazzino.ricorda(
                fornitore="noce", articolo="|", offerta=buona, accettata=True, quando=ADESSO
            )
        with self.assertRaises(ValueError):
            self.magazzino.ricorda(
                fornitore="noce",
                articolo=self.articolo(),
                offerta=impronta_articolo({"supplierId": "noce"}),
                accettata=True,
                quando=ADESSO,
            )
        with self.assertRaises(ValueError):
            self.magazzino.ricorda(
                fornitore="", articolo=self.articolo(), offerta=buona, accettata=True, quando=ADESSO
            )
        self.assertEqual(self.magazzino.esporta(), [])

    def test_una_conferma_senza_data_non_si_scrive(self) -> None:
        """Without `quando`, a wrong order can't be audited after the fact."""

        with self.assertRaises(ValueError):
            self.magazzino.ricorda(
                fornitore="noce",
                articolo=self.articolo(),
                offerta=impronta_articolo(OFFERTA_NOCE_XLS),
                accettata=True,
                quando="   ",
            )
        self.assertEqual(self.magazzino.esporta(), [])


class SostituzioneEStorico(BaseMagazzino):
    def test_la_risposta_nuova_sostituisce_quella_di_prima(self) -> None:
        self.conferma(offerta=OFFERTA_NOCE_CSV, quando=ADESSO)
        self.conferma(offerta=OFFERTA_NOCE_XLS, quando=DOPO, motivo="Riconfermato sul nuovo listino.")
        in_vigore = self.magazzino.cerca("noce", self.articolo())
        assert in_vigore is not None
        self.assertEqual(in_vigore["offerta"], impronta_articolo(OFFERTA_NOCE_XLS))
        self.assertEqual(in_vigore["valida_dal"], DOPO)
        self.assertEqual(len(self.magazzino.tutte()), 1)

    def test_lo_storico_resta_e_dice_che_cosa_valeva_prima(self) -> None:
        """What did I decide before, and when: must have an answer.

        A real order sits downstream: if a wrong confirmation already bought
        the wrong stock, the row that explains it must not be overwritten.
        """

        self.conferma(offerta=OFFERTA_NOCE_CSV, quando=ADESSO, motivo="Primo sì.")
        self.conferma(offerta=OFFERTA_NOCE_XLS, quando=DOPO, motivo="Secondo sì.")
        storia = self.magazzino.esporta()
        self.assertEqual(len(storia), 2)
        vecchia, nuova = storia[0], storia[1]
        self.assertEqual(vecchia["offerta"], impronta_articolo(OFFERTA_NOCE_CSV))
        self.assertEqual(vecchia["motivo"], "Primo sì.")
        self.assertFalse(vecchia["in_vigore"])
        self.assertTrue(nuova["in_vigore"])

    def test_la_storia_non_ha_buchi(self) -> None:
        """The previous row closes exactly when the new one takes effect.

        If the two instants didn't match, a query for what was valid at a
        given moment would have two answers or none.
        """

        self.conferma(offerta=OFFERTA_NOCE_CSV, quando=ADESSO)
        self.conferma(offerta=OFFERTA_NOCE_XLS, quando=DOPO)
        self.conferma(offerta=OFFERTA_LARICE, quando=ANCORA_DOPO, fornitore="noce")
        storia = self.magazzino.esporta()
        self.assertEqual([voce["valida_dal"] for voce in storia], [ADESSO, DOPO, ANCORA_DOPO])
        self.assertEqual([voce["valida_fino_a"] for voce in storia], [DOPO, ANCORA_DOPO, None])

    def test_riconfermare_la_stessa_identica_risposta_non_scrive_niente(self) -> None:
        """A guard against the caller, not the user.

        The page autosaves every 450 ms: if every save rewrote the
        confirmation, one decision would turn into hundreds of history rows
        describing decisions that were never made. `valida_dal` stays the
        first timestamp, since that's when the answer has held since.
        """

        self.conferma(quando=ADESSO)
        for _ in range(20):
            self.conferma(quando=DOPO)
        storia = self.magazzino.esporta()
        self.assertEqual(len(storia), 1)
        self.assertEqual(storia[0]["valida_dal"], ADESSO)

    def test_cambiare_solo_il_motivo_e_un_cambiamento(self) -> None:
        """The reason is part of the answer: it's what an audit reads."""

        self.conferma(quando=ADESSO, motivo="Sì.")
        self.conferma(quando=DOPO, motivo="Sì, verificato sul catalogo cartaceo.")
        self.assertEqual(len(self.magazzino.esporta()), 2)

    def test_cambiare_solo_la_decisione_e_un_cambiamento(self) -> None:
        self.conferma(quando=ADESSO, accettata=True, motivo="")
        self.conferma(quando=DOPO, accettata=False, motivo="")
        storia = self.magazzino.esporta()
        self.assertEqual(len(storia), 2)
        self.assertTrue(storia[0]["accettata"])
        self.assertFalse(storia[1]["accettata"])


class Dimenticare(BaseMagazzino):
    def test_toglie_la_conferma_in_vigore(self) -> None:
        self.conferma()
        self.assertTrue(self.magazzino.dimentica("noce", self.articolo(), quando=DOPO))
        self.assertIsNone(self.magazzino.cerca("noce", self.articolo()))
        self.assertEqual(self.magazzino.tutte(), [])

    def test_la_riga_tolta_resta_nell_audit(self) -> None:
        """A wrong confirmation is removed, not erased.

        If it already produced a wrong order, that row is the only thing
        explaining it: deleting it would fix the future while erasing the past.
        """

        self.conferma(motivo="Sì, sbagliando.")
        self.magazzino.dimentica("noce", self.articolo(), quando=DOPO)
        storia = self.magazzino.esporta()
        self.assertEqual(len(storia), 1)
        self.assertEqual(storia[0]["motivo"], "Sì, sbagliando.")
        self.assertEqual(storia[0]["valida_fino_a"], DOPO)
        self.assertFalse(storia[0]["in_vigore"])

    def test_dimenticare_quello_che_non_ce_dice_di_no(self) -> None:
        self.assertFalse(self.magazzino.dimentica("noce", self.articolo()))
        self.conferma()
        self.assertTrue(self.magazzino.dimentica("noce", self.articolo()))
        self.assertFalse(self.magazzino.dimentica("noce", self.articolo()))

    def test_dopo_aver_dimenticato_si_puo_riconfermare(self) -> None:
        """The pair is free again: the invariant must not lock the door shut."""

        self.conferma(quando=ADESSO)
        self.magazzino.dimentica("noce", self.articolo(), quando=DOPO)
        self.conferma(quando=ANCORA_DOPO, motivo="Ci riprovo.")
        in_vigore = self.magazzino.cerca("noce", self.articolo())
        assert in_vigore is not None
        self.assertEqual(in_vigore["valida_dal"], ANCORA_DOPO)
        self.assertEqual(len(self.magazzino.esporta()), 2)


class DueFornitori(BaseMagazzino):
    def test_lo_stesso_articolo_ha_una_risposta_per_fornitore(self) -> None:
        """Yes to one supplier and no to another, on the same item, is normal."""

        self.conferma(fornitore="noce", offerta=OFFERTA_NOCE_XLS, accettata=True)
        self.conferma(fornitore="larice", offerta=OFFERTA_LARICE, accettata=False, motivo="Loro hanno il 22 cm.")
        mega = self.magazzino.cerca("noce", self.articolo())
        larice = self.magazzino.cerca("larice", self.articolo())
        assert mega is not None and larice is not None
        self.assertTrue(mega["accettata"])
        self.assertFalse(larice["accettata"])
        self.assertEqual(len(self.magazzino.tutte()), 2)

    def test_dimenticare_uno_non_tocca_l_altro(self) -> None:
        self.conferma(fornitore="noce")
        self.conferma(fornitore="larice", offerta=OFFERTA_LARICE)
        self.magazzino.dimentica("noce", self.articolo(), quando=DOPO)
        self.assertIsNone(self.magazzino.cerca("noce", self.articolo()))
        self.assertIsNotNone(self.magazzino.cerca("larice", self.articolo()))

    def test_sostituire_uno_non_tocca_l_altro(self) -> None:
        self.conferma(fornitore="noce", offerta=OFFERTA_NOCE_CSV, quando=ADESSO)
        self.conferma(fornitore="larice", offerta=OFFERTA_LARICE, quando=ADESSO)
        self.conferma(fornitore="noce", offerta=OFFERTA_NOCE_XLS, quando=DOPO)
        larice = self.magazzino.cerca("larice", self.articolo())
        assert larice is not None
        self.assertEqual(larice["valida_dal"], ADESSO)
        self.assertEqual(len(self.magazzino.tutte()), 2)


class OffertaCambiata(BaseMagazzino):
    """The design decision behind `cerca`, and the real case that protects it."""

    def test_cerca_risponde_anche_se_l_articolo_del_fornitore_e_cambiato(self) -> None:
        """A real Noce case: 51 identical items — same barcode, same
        description — changed fingerprint because the price list moved from
        the site's CSV to their `.xls` and the item code went from empty to
        filled in. If `cerca` required today's fingerprint to match and
        reported "expired" otherwise, memory would have emptied out that week
        for items that hadn't actually changed. That's the bug this store closes.
        """

        self.conferma(offerta=OFFERTA_NOCE_CSV)
        trovata = self.magazzino.cerca("noce", self.articolo())
        self.assertIsNotNone(trovata)
        assert trovata is not None
        self.assertEqual(trovata["offerta"], impronta_articolo(OFFERTA_NOCE_CSV))
        self.assertNotEqual(trovata["offerta"], impronta_articolo(OFFERTA_NOCE_XLS))

    def test_il_chiamante_ha_in_mano_tutto_per_accorgersene(self) -> None:
        """A confirmation is never applied blindly: the caller compares.

        `cerca` returns the exact fingerprint that was confirmed, so the
        comparison against today's is a one-line check, and the difference
        can be shown to the user instead of starting over.
        """

        self.conferma(offerta=OFFERTA_NOCE_CSV, motivo="Confermato sul CSV.")
        trovata = self.magazzino.cerca("noce", self.articolo())
        assert trovata is not None
        oggi = impronta_articolo(OFFERTA_NOCE_XLS)
        combacia = trovata["offerta"] == oggi
        self.assertFalse(combacia)
        self.assertEqual(trovata["motivo"], "Confermato sul CSV.")
        self.assertEqual(trovata["valida_dal"], ADESSO)


class ElencoEdEsportazione(BaseMagazzino):
    def test_tutte_mostra_solo_quello_che_vale_adesso(self) -> None:
        self.conferma(offerta=OFFERTA_NOCE_CSV, quando=ADESSO)
        self.conferma(offerta=OFFERTA_NOCE_XLS, quando=DOPO)
        self.conferma(fornitore="larice", offerta=OFFERTA_LARICE, quando=DOPO)
        self.magazzino.dimentica("larice", self.articolo(), quando=ANCORA_DOPO)
        in_vigore = self.magazzino.tutte()
        self.assertEqual(len(in_vigore), 1)
        self.assertEqual(in_vigore[0]["fornitore"], "noce")
        self.assertEqual(in_vigore[0]["offerta"], impronta_articolo(OFFERTA_NOCE_XLS))
        self.assertEqual(len(self.magazzino.esporta()), 3)

    def test_esporta_si_scrive_in_json(self) -> None:
        """A `.db` file isn't human-readable; the user must be able to inspect it."""

        self.conferma(offerta=OFFERTA_NOCE_CSV, quando=ADESSO)
        self.conferma(offerta=OFFERTA_NOCE_XLS, quando=DOPO)
        documento = json.dumps(self.magazzino.esporta(), ensure_ascii=False, indent=2)
        riletto = json.loads(documento)
        self.assertEqual(riletto, self.magazzino.esporta())
        self.assertIn("DOPLO PIATTI DESSER 25PZ", documento)
        self.assertIsInstance(riletto[0]["accettata"], bool)

    def test_esporta_e_ripetibile(self) -> None:
        """Two exports of the same content must be comparable."""

        self.conferma(fornitore="larice", offerta=OFFERTA_LARICE, quando=DOPO)
        self.conferma(fornitore="noce", offerta=OFFERTA_NOCE_XLS, quando=ADESSO)
        self.assertEqual(self.magazzino.esporta(), self.magazzino.esporta())
        self.assertEqual(
            [voce["fornitore"] for voce in self.magazzino.esporta()], ["larice", "noce"]
        )

    def test_due_risposte_nello_stesso_istante_restano_in_ordine(self) -> None:
        """The case where the timestamp alone isn't enough to order history.

        Not theoretical: since callers timestamp confirmations to the second
        (as the project's adapter registry already does), two answers given in
        quick succession can carry the same `quando`, so the closed row and the
        new one share the same `valida_dal`. An audit still needs "before" and
        "after" to stay distinguishable.
        """

        self.conferma(quando=ADESSO, motivo="Primo sì.")
        self.conferma(quando=ADESSO, motivo="Secondo sì, corretto subito.")
        storia = self.magazzino.esporta()
        self.assertEqual([voce["motivo"] for voce in storia], ["Primo sì.", "Secondo sì, corretto subito."])
        self.assertEqual([voce["in_vigore"] for voce in storia], [False, True])
        self.assertEqual([voce["valida_dal"] for voce in storia], [ADESSO, ADESSO])

    def test_magazzino_vuoto(self) -> None:
        self.assertEqual(self.magazzino.tutte(), [])
        self.assertEqual(self.magazzino.esporta(), [])


class Persistenza(BaseMagazzino):
    def test_le_conferme_sopravvivono_alla_chiusura(self) -> None:
        """The whole point of the store: memory must still hold next week."""

        self.conferma(offerta=OFFERTA_NOCE_CSV, quando=ADESSO)
        self.conferma(offerta=OFFERTA_NOCE_XLS, quando=DOPO)
        self.magazzino.chiudi()

        riaperto = self.apri()
        trovata = riaperto.cerca("noce", self.articolo())
        assert trovata is not None
        self.assertEqual(trovata["offerta"], impronta_articolo(OFFERTA_NOCE_XLS))
        self.assertEqual(trovata["valida_dal"], DOPO)
        self.assertEqual(len(riaperto.esporta()), 2)

    def test_riaprire_non_azzera_e_non_duplica_lo_schema(self) -> None:
        self.conferma()
        for _ in range(3):
            self.apri().chiudi()
        riaperto = self.apri()
        self.assertEqual(len(riaperto.esporta()), 1)
        self.assertIsNotNone(riaperto.cerca("noce", self.articolo()))

    def test_il_giornale_e_wal(self) -> None:
        """The service is multi-threaded: without WAL, a read would block a write."""

        self.assertEqual(self.magazzino.giornale, "wal")

    def test_usare_il_magazzino_chiuso_lo_dice(self) -> None:
        self.conferma()
        self.magazzino.chiudi()
        self.magazzino.chiudi()  # closing twice is harmless
        with self.assertRaises(MagazzinoNonUtilizzabile):
            self.magazzino.cerca("noce", self.articolo())
        with self.assertRaises(MagazzinoNonUtilizzabile):
            self.magazzino.tutte()


class FileGuasto(unittest.TestCase):
    """A file that won't open must not crash the app."""

    def setUp(self) -> None:
        self.cartella = Path(tempfile.mkdtemp(prefix="conferme-guasto-"))

    def test_un_file_corrotto_da_un_errore_dichiarato(self) -> None:
        percorso = self.cartella / "conferme.db"
        percorso.write_bytes(b"questo non e' un database, sono 40 byte di spazzatura!!")
        with self.assertRaises(MagazzinoNonUtilizzabile) as caso:
            MagazzinoConferme(percorso)
        self.assertIn(str(percorso), str(caso.exception))

    def test_dopo_un_file_corrotto_il_programma_continua(self) -> None:
        """How the service actually uses this: catch it and carry on without memory."""

        rotto = self.cartella / "rotto.db"
        rotto.write_bytes(b"\x00\x01\x02 non sono SQLite \xff\xfe")
        magazzino = None
        try:
            magazzino = MagazzinoConferme(rotto)
        except MagazzinoNonUtilizzabile:
            magazzino = None
        self.assertIsNone(magazzino)

        buono = MagazzinoConferme(self.cartella / "buono.db")
        try:
            buono.ricorda(
                fornitore="noce",
                articolo=impronta_prodotto(PRODOTTO_DOPLO),
                offerta=impronta_articolo(OFFERTA_NOCE_XLS),
                accettata=True,
                quando=ADESSO,
            )
            self.assertIsNotNone(buono.cerca("noce", impronta_prodotto(PRODOTTO_DOPLO)))
        finally:
            buono.chiudi()

    def test_un_percorso_illeggibile_da_un_errore_dichiarato(self) -> None:
        """The path is a directory: happens with a misconfigured path."""

        cartella = self.cartella / "sono-una-cartella"
        cartella.mkdir()
        with self.assertRaises(MagazzinoNonUtilizzabile):
            MagazzinoConferme(cartella)

    def test_un_file_di_una_versione_futura_non_si_tocca(self) -> None:
        """Rolling the app back must not rewrite a file newer than itself."""

        percorso = self.cartella / "futuro.db"
        conn = sqlite3.connect(str(percorso))
        conn.execute(f"PRAGMA user_version={conferme.VERSIONE_SCHEMA + 7}")
        conn.close()
        with self.assertRaises(MagazzinoNonUtilizzabile):
            MagazzinoConferme(percorso)

    def test_la_cartella_che_manca_si_crea(self) -> None:
        percorso = self.cartella / "mai" / "vista" / "conferme.db"
        magazzino = MagazzinoConferme(percorso)
        try:
            self.assertTrue(percorso.exists())
        finally:
            magazzino.chiudi()


class Concorrenza(unittest.TestCase):
    """Two writers: the service has the page's polling and the pipeline's thread."""

    def setUp(self) -> None:
        self.cartella = Path(tempfile.mkdtemp(prefix="conferme-concorrenza-"))
        self.percorso = self.cartella / "conferme.db"
        self.articolo = impronta_prodotto(PRODOTTO_DOPLO)

    def _lancia(self, lavori: list) -> list[BaseException]:
        errori: list[BaseException] = []
        partenza = threading.Barrier(len(lavori))

        def esegui(lavoro) -> None:
            partenza.wait()
            try:
                lavoro()
            except BaseException as errore:  # noqa: BLE001 - va raccolto, non ingoiato
                errori.append(errore)

        fili = [threading.Thread(target=esegui, args=(lavoro,)) for lavoro in lavori]
        for filo in fili:
            filo.start()
        for filo in fili:
            filo.join(timeout=60)
        for filo in fili:
            self.assertFalse(filo.is_alive(), "un filo non è mai finito: scrittura bloccata")
        return errori

    def test_due_connessioni_scrivono_lo_stesso_articolo(self) -> None:
        """The contested pair: exactly one answer ends up in effect, and the
        full history survives.

        The case that breaks a poorly written SQLite write path: two
        transactions that start in read mode and try to upgrade to a write can
        end up deadlocked on each other with neither able to retry.
        """

        primo = MagazzinoConferme(self.percorso)
        secondo = MagazzinoConferme(self.percorso)
        self.addCleanup(primo.chiudi)
        self.addCleanup(secondo.chiudi)

        def scrittura(magazzino: MagazzinoConferme, base: int):
            def lavoro() -> None:
                for passo in range(6):
                    magazzino.ricorda(
                        fornitore="noce",
                        articolo=self.articolo,
                        offerta=f"noce|8009721709065|COD{base + passo}|DOPLO PIATTO FRUTTA",
                        accettata=passo % 2 == 0,
                        motivo=f"giro {base + passo}",
                        quando=f"2026-08-15T10:{base + passo:02d}:00+00:00",
                    )
            return lavoro

        errori = self._lancia([scrittura(primo, 0), scrittura(secondo, 30)])
        self.assertEqual(errori, [], f"scrittura concorrente fallita: {errori}")

        in_vigore = primo.tutte()
        self.assertEqual(len(in_vigore), 1)
        storia = primo.esporta()
        self.assertEqual(len(storia), 12)
        # The chain reads in write order, not clock order: two threads don't
        # agree on the time.
        per_scrittura = sorted(storia, key=lambda voce: voce["id"])
        for prima, dopo in zip(per_scrittura, per_scrittura[1:]):
            self.assertEqual(prima["valida_fino_a"], dopo["valida_dal"])
        self.assertIsNone(per_scrittura[-1]["valida_fino_a"])

    def test_due_connessioni_scrivono_articoli_diversi(self) -> None:
        primo = MagazzinoConferme(self.percorso)
        secondo = MagazzinoConferme(self.percorso)
        self.addCleanup(primo.chiudi)
        self.addCleanup(secondo.chiudi)

        def scrittura(magazzino: MagazzinoConferme, fornitore: str):
            def lavoro() -> None:
                for passo in range(15):
                    magazzino.ricorda(
                        fornitore=fornitore,
                        articolo=f"800130056{passo:04d}|ARTICOLO {passo}",
                        offerta=f"{fornitore}|800130056{passo:04d}||RIGA {passo}",
                        accettata=True,
                        quando=ADESSO,
                    )
            return lavoro

        errori = self._lancia([scrittura(primo, "noce"), scrittura(secondo, "larice")])
        self.assertEqual(errori, [], f"scrittura concorrente fallita: {errori}")
        self.assertEqual(len(primo.tutte()), 30)

    def test_lo_stesso_magazzino_da_piu_fili(self) -> None:
        """A single shared object, the way the service actually uses it."""

        magazzino = MagazzinoConferme(self.percorso)
        self.addCleanup(magazzino.chiudi)

        def scrittura(base: int):
            def lavoro() -> None:
                for passo in range(12):
                    magazzino.ricorda(
                        fornitore="noce",
                        articolo=f"8001{base + passo:05d}|ARTICOLO {base + passo}",
                        offerta=f"noce|8001{base + passo:05d}||RIGA",
                        accettata=True,
                        quando=ADESSO,
                    )
                    magazzino.cerca("noce", f"8001{base + passo:05d}|ARTICOLO {base + passo}")
                    magazzino.tutte()
            return lavoro

        errori = self._lancia([scrittura(0), scrittura(100), scrittura(200)])
        self.assertEqual(errori, [], f"uso concorrente fallito: {errori}")
        self.assertEqual(len(magazzino.tutte()), 36)

    def test_il_database_impedisce_due_risposte_in_vigore(self) -> None:
        """The invariant isn't left to application code: a unique index holds it.

        If a transaction here ever gets it wrong, the file won't end up with
        two simultaneous answers for the same pair: the write just fails.
        """

        magazzino = MagazzinoConferme(self.percorso)
        self.addCleanup(magazzino.chiudi)
        magazzino.ricorda(
            fornitore="noce",
            articolo=self.articolo,
            offerta=impronta_articolo(OFFERTA_NOCE_XLS),
            accettata=True,
            quando=ADESSO,
        )
        intruso = sqlite3.connect(str(self.percorso))
        self.addCleanup(intruso.close)
        with self.assertRaises(sqlite3.IntegrityError):
            intruso.execute(
                "INSERT INTO conferme (fornitore, articolo, offerta, accettata, motivo, "
                "valida_dal, valida_fino_a) VALUES (?, ?, ?, ?, ?, ?, NULL)",
                ("noce", self.articolo, "noce|altro||ALTRO", 1, "", DOPO),
            )


# --- Barcode equalities -----------------------------------------------------
#
# A real, measured case: "LUXA SAPONE LIQ. EROG.250ML" sits in the
# management software under code 4009428623194, which only one supplier uses
# (at 1.28 EUR/pc); three other suppliers list the same item under
# 8729721830575, at different prices. No scoring can infer this: the variant
# (ORIGINAL vs SETA) isn't in the management-software name at all, and one
# supplier's shortlist had the correct row ranked second, not first. What
# follows tests the memory that closes that gap, and the cost of getting it
# wrong: a bad equality doesn't spoil one item at one supplier, it spoils it
# at every supplier, every week.

DOVE_GESTIONALE = "4009428623194"
DOVE_FORNITORI = "8729721830575"


class UguaglianzeFraCodiciTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cartella = tempfile.TemporaryDirectory()
        self.addCleanup(self.cartella.cleanup)
        self.magazzino = MagazzinoConferme(Path(self.cartella.name) / "conferme.db")
        self.addCleanup(self.magazzino.chiudi)

    def test_due_codici_dichiarati_uguali_si_ritrovano_da_tutti_e_due_i_lati(self) -> None:
        self.assertIs(
            self.magazzino.unisci(DOVE_GESTIONALE, DOVE_FORNITORI, quando=ADESSO), True
        )

        self.assertEqual(
            self.magazzino.classe(DOVE_GESTIONALE), [DOVE_GESTIONALE, DOVE_FORNITORI]
        )
        self.assertEqual(
            self.magazzino.classe(DOVE_FORNITORI), [DOVE_GESTIONALE, DOVE_FORNITORI]
        )

    def test_l_uguaglianza_non_ha_un_verso(self) -> None:
        """Declaring it in reverse is the same declaration, and removes the same way.

        Without a fixed order, the unique index wouldn't stop the second
        write, and `separa` would look for a pair stored the other way round:
        a wrong equality that can't be removed.
        """

        self.magazzino.unisci(DOVE_GESTIONALE, DOVE_FORNITORI, quando=ADESSO)

        self.assertIs(self.magazzino.unisci(DOVE_FORNITORI, DOVE_GESTIONALE, quando=DOPO), False)
        self.assertEqual(len(self.magazzino.uguaglianze()), 1)
        self.assertIs(self.magazzino.separa(DOVE_FORNITORI, DOVE_GESTIONALE, quando=DOPO), True)
        self.assertEqual(self.magazzino.uguaglianze(), [])

    def test_le_dichiarazioni_si_incatenano(self) -> None:
        """A≡B and B≡C form a group of three: looking up A also finds C."""

        self.magazzino.unisci("111", "222", quando=ADESSO)
        self.magazzino.unisci("222", "333", quando=DOPO)

        self.assertEqual(self.magazzino.classi(), [["111", "222", "333"]])
        self.assertEqual(self.magazzino.classe("333"), ["111", "222", "333"])

    def test_togliendo_l_anello_di_mezzo_restano_due_gruppi_separati(self) -> None:
        """Only what was declared gets stored, never what can be inferred from it.

        If `unisci` also stored the implied A≡C, removing A≡B would leave an
        equality standing that nobody could trace back to a declaration.
        """

        self.magazzino.unisci("111", "222", quando=ADESSO)
        self.magazzino.unisci("222", "333", quando=ADESSO)
        self.magazzino.separa("222", "333", quando=DOPO)

        self.assertEqual(self.magazzino.classi(), [["111", "222"]])
        self.assertEqual(self.magazzino.classe("333"), ["333"])

    def test_un_codice_senza_uguaglianze_e_se_stesso(self) -> None:
        """A caller querying the index by EAN must not have one fewer case to handle."""

        self.assertEqual(self.magazzino.classe(DOVE_GESTIONALE), [DOVE_GESTIONALE])
        self.assertEqual(self.magazzino.classe(""), [])

    def test_lo_stesso_codice_scritto_in_modi_diversi_e_lo_stesso_codice(self) -> None:
        """EANs in the management software are hand-typed, and the spreadsheet
        reads them back as numbers: without this normalization, an equality
        declared one day wouldn't be found the next, with no error raised."""

        self.magazzino.unisci(4009428623194.0, f"'{DOVE_FORNITORI}", quando=ADESSO)

        self.assertEqual(self.magazzino.classe(f"  {DOVE_GESTIONALE} "), [DOVE_GESTIONALE, DOVE_FORNITORI])
        self.assertEqual(conferme.codice_confrontabile("8729721830575.0"), DOVE_FORNITORI)

    def test_un_codice_solo_o_vuoto_non_e_una_dichiarazione(self) -> None:
        with self.assertRaises(ValueError):
            self.magazzino.unisci(DOVE_GESTIONALE, DOVE_GESTIONALE, quando=ADESSO)
        with self.assertRaises(ValueError):
            self.magazzino.unisci(DOVE_GESTIONALE, "", quando=ADESSO)
        with self.assertRaises(ValueError):
            self.magazzino.unisci(DOVE_GESTIONALE, DOVE_FORNITORI, quando="")

    def test_niente_si_cancella_mai(self) -> None:
        """If it already produced a wrong order, the row is the only trace
        that explains it: `separa` closes it, it doesn't discard it."""

        self.magazzino.unisci(
            DOVE_GESTIONALE, DOVE_FORNITORI,
            articolo=f"{DOVE_GESTIONALE}|LUXA SAPONE LIQ EROG 250ML",
            offerta=f"noce|{DOVE_FORNITORI}||LUXA SAPONE EROGATORE ORIGINAL ML 250",
            motivo="scelta a mano dal listino NOCE, riga 4794",
            quando=ADESSO,
        )
        self.magazzino.separa(DOVE_GESTIONALE, DOVE_FORNITORI, quando=DOPO)

        storia = self.magazzino.esporta_uguaglianze()
        self.assertEqual(len(storia), 1)
        self.assertIs(storia[0]["in_vigore"], False)
        self.assertEqual(storia[0]["valida_dal"], ADESSO)
        self.assertEqual(storia[0]["valida_fino_a"], DOPO)
        self.assertIn("riga 4794", storia[0]["motivo"])
        self.assertIn("LUXA SAPONE EROGATORE", storia[0]["offerta"])

    def test_il_database_impedisce_due_dichiarazioni_contemporanee(self) -> None:
        """The unique index holds the invariant, not application code."""

        self.magazzino.unisci(DOVE_GESTIONALE, DOVE_FORNITORI, quando=ADESSO)
        intruso = sqlite3.connect(str(self.magazzino.percorso))
        self.addCleanup(intruso.close)
        with self.assertRaises(sqlite3.IntegrityError):
            intruso.execute(
                "INSERT INTO uguaglianze (codice_a, codice_b, articolo, offerta, motivo, "
                "valida_dal, valida_fino_a) VALUES (?, ?, '', '', '', ?, NULL)",
                (DOVE_GESTIONALE, DOVE_FORNITORI, DOPO),
            )

    def test_le_classi_sono_stabili(self) -> None:
        """This is the artifact the pipeline consumes downstream: two reads of
        the same content must produce the same document, or comparing this
        week's run against last week's is meaningless."""

        self.magazzino.unisci("333", "111", quando=ADESSO)
        self.magazzino.unisci("222", "444", quando=ADESSO)

        self.assertEqual(self.magazzino.classi(), [["111", "333"], ["222", "444"]])
        self.assertEqual(self.magazzino.classi(), self.magazzino.classi())


if __name__ == "__main__":
    unittest.main()
