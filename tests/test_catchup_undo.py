import os
import tempfile
import unittest

from evolved.catchup import collect_history, run_catchup
from evolved.data import load_rules
from evolved.engine import EngineConfig, EvolvedEngine
from evolved.journal import Journal


BASE_TS = 1789000000  # fixed synthetic baseline (seconds)


def _row(seq, card, ease=3, kind=1):
    rid = BASE_TS * 1000 + seq
    return {"revlog_id": rid, "card_id": card, "ease": ease,
            "revlog_type": kind, "ts": BASE_TS + seq}


class _Col:
    def __init__(self, rows):
        self.db = self
        self._rows = list(rows)

    def all(self, query, *params):
        rows = [(r["revlog_id"], r["card_id"], r["ease"], r["revlog_type"])
                for r in self._rows]
        rows.sort(reverse=True)
        limit = params[-1] if params else len(rows)
        since = params[0] if ">" in query and len(params) > 1 else 0
        out = [r for r in rows if r[0] > since]
        return out[:limit]

    def scalar(self, query, *params):
        return None


def _engine(path, game="game-cu-1"):
    journal = Journal(path)
    cfg = EngineConfig(game_uuid=game, device_id="dev-a", activated_at=1000,
                       rules=load_rules())
    return EvolvedEngine(cfg, journal), journal


class TestCatchup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_full_scan_catches_late_arrival_and_dedups(self):
        eng, journal = _engine(os.path.join(self.tmp.name, "g.sqlite3"))
        self.addCleanup(journal.close)
        rows = [
            _row(30, 3),
            _row(10, 1),
            # Late arrival older than the max id, plus a rating-1 row.
            _row(20, 2),
            _row(40, 4, ease=1),
        ]
        first = run_catchup(_Col(rows), eng, journal, full=True)
        self.assertEqual(first["made"], 3)
        again = run_catchup(_Col(rows), eng, journal, full=True)
        self.assertEqual(again["made"], 0)

    def test_scan_skips_directly_credited_rows(self):
        # A direct award followed by a scan must NOT re-ingest the same
        # review (regression: missing review_key on scan rows defeated the
        # processed-set dedup and littered duplicate catch-up ops).
        eng, journal = _engine(os.path.join(self.tmp.name, "d.sqlite3"))
        self.addCleanup(journal.close)
        credited = eng.credit_direct(revlog_id=BASE_TS * 1000 + 11, card_id=21,
                                     ease=3, revlog_type=1,
                                     review_ts=BASE_TS + 11, skill="mining",
                                     resource="Rune essence")
        self.assertTrue(credited["awarded"])
        out = run_catchup(_Col([_row(11, 21), _row(12, 22)]), eng, journal,
                          full=True)
        self.assertEqual(out["made"], 1)
        self.assertEqual(journal.operation_count(), 2)

    def test_hydrate_survives_restart(self):
        path = os.path.join(self.tmp.name, "h.sqlite3")
        eng, journal = _engine(path)
        col = _Col([_row(11, 21)])
        self.assertEqual(run_catchup(col, eng, journal, full=True)["made"], 1)
        journal.close()
        eng2, journal2 = _engine(path)
        self.addCleanup(journal2.close)
        self.assertEqual(eng2.hydrate(), 1)
        self.assertEqual(run_catchup(col, eng2, journal2, full=True)["made"], 0)

    def test_reconcile_retract_and_restore(self):
        eng, journal = _engine(os.path.join(self.tmp.name, "u.sqlite3"))
        self.addCleanup(journal.close)
        credited = eng.credit_direct(revlog_id=50, card_id=60, ease=3, revlog_type=1,
                                     review_ts=1500, skill="mining",
                                     resource="Rune essence")
        key = credited["review_key"]
        live = {50}
        out = eng.reconcile_undo(lambda rid: rid in live)
        self.assertEqual(out["retracted"], [])
        live.clear()  # Anki Undo deleted the revlog row
        out = eng.reconcile_undo(lambda rid: rid in live)
        self.assertEqual(out["retracted"], [key])
        # Duplicate reconcile is safe (idempotent downstream).
        out = eng.reconcile_undo(lambda rid: rid in live)
        self.assertEqual(out["retracted"], [])
        live.add(50)  # Redo restored the row
        out = eng.reconcile_undo(lambda rid: rid in live)
        self.assertEqual(out["restored"], [key])


if __name__ == "__main__":
    unittest.main()
