from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
try:
    from search_result_model import SearchResultModel  # isolated workspace fixture
except ModuleNotFoundError:
    sys.path.insert(0, str(ROOT.parents[2] / 'resources' / 'naia-backend'))
    from core.search_result_model import SearchResultModel
from naia_exten.features.parquet_live_sync import ParquetLiveSyncFeature
from naia_exten.features.multi_parquet_pool import MultiParquetPoolFeature
from naia_exten.parquet_sync_journal import ConsumptionJournal

SearchResultModel._bucket_starts_cache = [0]


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT.parent)
        self.root = Path(self.temp.name)
        self.frame = pd.DataFrame({'id': [1, 2, 3, 4], 'general': ['a', 'b', 'c', 'd'],
                                   'rating': ['g', 's', 'q', 'e']})
        self.path = self.root / 'source.parquet'
        self.frame.to_parquet(self.path, index=False)
        self.feature, self.context = self.make_feature(self.frame)

    def tearDown(self):
        self.temp.cleanup()

    def make_feature(self, frame, targets=None):
        model = SearchResultModel(frame.copy())
        context = SimpleNamespace(search_results=model, search_results_snapshot=frame.copy(),
                                  search_results_master_base_snapshot=frame.copy())
        feature = ParquetLiveSyncFeature()
        feature._context = context
        feature._journal = ConsumptionJournal(self.root / 'consumed.sqlite3')
        feature.ext = SimpleNamespace(ctx=SimpleNamespace(log=lambda *_: None))
        feature._runtime_active = lambda: True
        feature._start_worker = lambda: None
        feature._set_targets(targets or [self.path], persist=False, log=False)
        return feature, context

    def pop(self, feature=None, context=None, ratings=None):
        feature = feature or self.feature
        context = context or self.context
        return feature.run_selection(SearchResultModel.pop_random_row, context.search_results, ratings or set('gsqe'))

    def test_pop_has_no_parquet_io_or_dataframe_copy(self):
        snapshots = (self.context.search_results_snapshot, self.context.search_results_master_base_snapshot)
        row = self.frame.iloc[0]
        with patch.object(pd, 'read_parquet', side_effect=AssertionError('read on pop')), \
             patch.object(pd.DataFrame, 'copy', side_effect=AssertionError('copy on pop')), \
             patch.object(pd.DataFrame, 'to_parquet', side_effect=AssertionError('write on pop')):
            self.feature._after_row_pop(row, self.context.search_results)
        self.assertIs(snapshots[0], self.context.search_results_snapshot)
        self.assertIs(snapshots[1], self.context.search_results_master_base_snapshot)
        self.assertEqual(len(self.feature._journal.pending()[str(self.path.resolve())]), 1)

    def test_batch_preserves_unselected_ratings_and_schema(self):
        schema = pq.read_schema(self.path)
        self.pop(ratings={'g'})
        self.pop(ratings={'s'})
        self.assertEqual(len(pd.read_parquet(self.path)), 4)
        with patch.object(self.feature, '_compact_parquet', wraps=self.feature._compact_parquet) as compact:
            self.feature.flush_pending()
            self.assertEqual(compact.call_count, 1)
        self.assertEqual(pd.read_parquet(self.path)['id'].tolist(), [3, 4])
        self.assertEqual(pq.read_schema(self.path), schema)
        self.assertFalse(self.feature._journal.pending())

    def test_restart_recovers_unflushed_records_and_filters_old_snapshot(self):
        self.pop(ratings={'g'})
        recovered, context = self.make_feature(self.frame)
        recovered._prepare_model(context.search_results)
        self.assertEqual(context.search_results.get_count_by_rating()['g'], 0)
        self.assertIsNone(self.pop(recovered, context, {'g'}))
        recovered.flush_pending()
        self.assertEqual(pd.read_parquet(self.path)['id'].tolist(), [2, 3, 4])

    def test_compacted_tombstone_still_filters_stale_snapshot(self):
        self.pop(ratings={'g'})
        self.feature.flush_pending()
        recovered, context = self.make_feature(self.frame)
        self.assertIsNone(self.pop(recovered, context, {'g'}))
        self.assertIsNotNone(self.pop(recovered, context, {'s'}))

    def test_failure_keeps_original_and_retries(self):
        self.pop(ratings={'g'})
        original = self.path.read_bytes()
        with patch('naia_exten.features.parquet_live_sync.os.replace', side_effect=PermissionError('busy')):
            self.feature.flush_pending()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertTrue(self.feature._journal.pending())
        self.assertFalse(self.path.with_name(self.path.name + '.exten.sync.tmp').exists())
        self.feature.flush_pending()
        self.assertFalse(self.feature._journal.pending())

    def test_switch_target_does_not_drop_old_pending_work(self):
        self.pop(ratings={'g'})
        other = self.root / 'other.parquet'
        self.frame.to_parquet(other, index=False)
        self.feature._set_targets([other], persist=False, log=False)
        self.pop(ratings={'s'})
        self.feature.flush_pending()
        self.assertEqual(pd.read_parquet(self.path)['id'].tolist(), [2, 3, 4])
        self.assertEqual(pd.read_parquet(other)['id'].tolist(), [1, 3, 4])

    def test_nested_selectors_record_once(self):
        def nested(model, ratings):
            return self.feature.run_selection(SearchResultModel.pop_random_row, model, ratings)
        self.feature.run_selection(nested, self.context.search_results, {'g'})
        self.assertEqual(len(self.feature._journal.pending()[str(self.path.resolve())]), 1)

    def test_disabled_does_not_record_new_rows(self):
        self.feature._runtime_active = lambda: False
        self.pop(ratings={'g'})
        self.assertFalse(self.feature._journal.pending())

    def test_no_id_nullable_row_survives_restart(self):
        frame = pd.DataFrame({'general': ['a', 'b'], 'artist': [None, 'x'], 'rating': ['g', 's']})
        frame.to_parquet(self.path, index=False)
        feature, context = self.make_feature(frame)
        self.pop(feature, context, {'g'})
        recovered, context = self.make_feature(frame)
        self.assertIsNone(self.pop(recovered, context, {'g'}))
        recovered.flush_pending()
        self.assertEqual(pd.read_parquet(self.path)['general'].tolist(), ['b'])

    def test_large_file_compaction_crosses_batch_boundary(self):
        frame = pd.DataFrame({'id': range(70000), 'general': ['x'] * 70000, 'rating': ['s'] * 70000})
        frame.to_parquet(self.path, index=False, row_group_size=10000)
        for row_id in [0, 65536, 69999]:
            self.feature._journal.record([self.path], f'id:{row_id}', row_id, None)
        self.feature.flush_pending()
        result = pd.read_parquet(self.path)
        self.assertEqual(len(result), 69997)
        self.assertFalse(result.id.isin([0, 65536, 69999]).any())

    def test_records_added_during_compaction_are_not_acknowledged_early(self):
        self.pop(ratings={'g'})
        compact = self.feature._compact_parquet
        def concurrent(path, ids, rows, **kwargs):
            self.feature._journal.record([self.path], 'id:2', 2, None)
            return compact(path, ids, rows, **kwargs)
        with patch.object(self.feature, '_compact_parquet', side_effect=concurrent):
            self.feature.flush_pending()
        self.assertEqual(len(self.feature._journal.pending()[str(self.path.resolve())]), 1)
        self.feature.flush_pending()
        self.assertEqual(pd.read_parquet(self.path).id.tolist(), [3, 4])

    def test_direct_multi_pool_with_rating_weights_is_journaled(self):
        frame = self.frame.copy()
        frame[MultiParquetPoolFeature.SOURCE_COL] = 0
        self.context.search_results = SearchResultModel(frame)
        multi = MultiParquetPoolFeature()
        multi._context = self.context
        multi.ext = SimpleNamespace(features={'parquet_live_sync': self.feature}, ctx=self.feature.ctx)
        multi.equal_probability_enabled = lambda: True
        row = multi.pop_equal_row(self.context.search_results, set('gsqe'), rating_weights={'g': 100})
        self.assertEqual(row['rating'], 'g')
        self.feature.flush_pending()
        self.assertEqual(pd.read_parquet(self.path).id.tolist(), [2, 3, 4])

    def test_no_id_crash_after_replace_does_not_delete_second_duplicate(self):
        frame = pd.DataFrame({'general': ['same', 'same', 'other'], 'rating': ['g', 'g', 's']})
        frame.to_parquet(self.path, index=False)
        feature, context = self.make_feature(frame)
        self.pop(feature, context, {'g'})
        with patch.object(feature._journal, 'acknowledge', side_effect=OSError('simulated exit')):
            feature.flush_pending()
        self.assertEqual(pd.read_parquet(self.path).general.tolist(), ['same', 'other'])
        recovered, _ = self.make_feature(frame)
        recovered.flush_pending()
        self.assertEqual(pd.read_parquet(self.path).general.tolist(), ['same', 'other'])
        self.assertFalse(recovered._journal.pending())

    def test_compacted_no_id_duplicate_remains_selectable(self):
        frame = pd.DataFrame({'general': ['same', 'same', 'other'], 'rating': ['g', 'g', 's']})
        frame.to_parquet(self.path, index=False)
        feature, context = self.make_feature(frame)
        self.pop(feature, context, {'g'})
        feature.flush_pending()
        recovered, context = self.make_feature(pd.read_parquet(self.path))
        self.assertIsNotNone(self.pop(recovered, context, {'g'}))
        recovered.flush_pending()
        self.assertEqual(pd.read_parquet(self.path).general.tolist(), ['other'])


if __name__ == '__main__':
    unittest.main()
