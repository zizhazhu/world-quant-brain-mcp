"""Offline behavioural checks for the October upstream integration.

Run: .venv/bin/python test_upstream_merge.py
Uses the existing MCP stub; all platform calls and browser operations are fake.
Every on-disk cache used below lives in a TemporaryDirectory.
"""
import asyncio
import base64
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, quote, urlparse

import test_simulation_mode as bootstrap

main = bootstrap.main
import forum_functions as forum

CFG = {'instrumentType': 'EQUITY', 'region': 'USA', 'delay': 1, 'universe': 'TOP3000'}


def field(fid):
    return {'id': fid, 'type': 'MATRIX', 'description': fid, 'coverage': 0.9}


class CatalogueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='wqb-catalogue-test-')
        self.addCleanup(self.tmp.cleanup)
        self.client = main.BrainApiClient.__new__(main.BrainApiClient)
        self.client.base_url = 'https://example.invalid'
        self.client.log = lambda *a, **k: None
        self.client.store = main.PersistentStore(Path(self.tmp.name), self.client.log)
        self.client.ensure_authenticated = AsyncMock()
        self.client._catalogue_task = None
        self.client._catalogue_state = {}
        self.datasets = [{'id': 'a', 'fieldCount': 2}]
        self.rows = {'a': [field('a1'), field('a2')]}
        self.calls = []
        self.client._request_json_with_retries = self.request

    async def request(self, method, url, *, params, **kwargs):
        self.calls.append((url, copy.deepcopy(params)))
        if url.endswith('/data-sets'):
            return {'count': len(self.datasets), 'results': copy.deepcopy(self.datasets)}
        dsid = params['dataset.id']
        rows = self.rows[dsid]
        count = next(ds['fieldCount'] for ds in self.datasets if ds['id'] == dsid)
        start = params['offset']
        return {'count': count, 'results': copy.deepcopy(rows[start:start + params['limit']])}

    def ds_key(self, dsid='a', cfg=CFG):
        return f"{self.client._df_config_key(cfg['instrumentType'], cfg['region'], cfg['delay'], cfg['universe'])}:{dsid}"

    def aggregate_key(self, cfg=CFG):
        return self.client._generate_cache_key('datafields', {**cfg, 'theme': 'false',
                                                            'dataset_id': None, 'data_type': ''})

    async def seed(self, ns, key, payload):
        await self.client.store.put(ns, key, payload)
        return self.client.store.path_for(ns, key).read_bytes()

    async def seed_old(self):
        old = {'results': [field('old')], 'count': 1, 'complete': True, 'declared_count': 1}
        ds = await self.seed('datafields_ds', self.ds_key(), old)
        agg = await self.seed('datafields', self.aggregate_key(), old)
        retired = await self.seed('datafields_ds', self.ds_key('retired'), old)
        return ds, agg, retired

    async def background(self, configs=None, mode='dedup'):
        started = await self.client.start_catalogue_build(configs or [dict(CFG)], mode=mode)
        self.assertTrue(started['started'])
        await self.client._catalogue_task
        return self.client._catalogue_state

    def field_requests(self):
        return [p for u, p in self.calls if u.endswith('/data-fields')]

    async def test_theme_filter_only_when_explicit(self):
        await self.client.get_datasets(force_refresh=True)
        self.assertNotIn('theme', self.calls[-1][1])
        await self.client.get_datasets(theme='true', force_refresh=True)
        self.assertEqual(self.calls[-1][1]['theme'], 'true')

    async def test_bad_listing_keeps_prior_cache(self):
        params = {'category': None, 'region': 'USA', 'delay': 1, 'universe': 'TOP3000', 'theme': 'false'}
        key = self.client._generate_cache_key('datasets', params)
        old = await self.seed('datasets', key, {'results': [{'id': 'old'}], 'count': 1})
        cases = [
            {'count': 2, 'results': self.datasets},
            {'count': 2, 'results': self.datasets * 2},
            {'count': 1, 'results': [{'fieldCount': 2}]},
            {'count': 1, 'results': [{'id': 'a'}]},
            {'results': self.datasets},
        ]
        for response in cases:
            with self.subTest(response=response), patch.object(
                    self.client, '_request_json_with_retries', AsyncMock(return_value=response)):
                with self.assertRaises(ValueError):
                    await self.client.get_datasets(force_refresh=True)
                self.assertEqual(self.client.store.path_for('datasets', key).read_bytes(), old)

    async def test_listing_duplicate_across_pages_and_changing_count(self):
        first = [{'id': f'd{i}', 'fieldCount': 0} for i in range(50)]
        for second in ({'count': 51, 'results': [first[0]]},
                       {'count': 52, 'results': [{'id': 'd50', 'fieldCount': 0}]}):
            with self.subTest(second=second), patch.object(self.client, '_request_json_with_retries',
                    AsyncMock(side_effect=[{'count': 51, 'results': first}, second])):
                with self.assertRaises(ValueError):
                    await self.client.get_datasets(force_refresh=True)

    async def test_partial_fields_preserve_old_bytes_and_report_failure(self):
        ds, agg, retired = await self.seed_old()
        self.rows['a'] = [field('a1')]
        result = await self.client.build_full_datafield_catalogue()
        self.assertFalse(result['complete'])
        self.assertFalse(result['cache_updated'])
        self.assertEqual(result['incomplete_datasets'], ['a'])
        self.assertTrue(result['per_dataset']['a']['defects'])
        self.assertEqual(len(self.field_requests()), 3)
        for ns, key, before in [('datafields_ds', self.ds_key(), ds),
                                ('datafields', self.aggregate_key(), agg),
                                ('datafields_ds', self.ds_key('retired'), retired)]:
            self.assertEqual(self.client.store.path_for(ns, key).read_bytes(), before)

    async def test_first_partial_fetch_never_persisted(self):
        self.rows['a'] = [field('a1')]
        result = await self.client.get_dataset_fields('a', expected_count=2)
        self.assertFalse(result['complete'])
        self.assertFalse(result['cache_updated'])
        self.assertIsNone(await self.client.store.get('datafields_ds', self.ds_key()))

    async def test_duplicate_and_missing_field_ids_retried(self):
        for rows in ([field('same'), field('same')], [field('a1'), {'id': None}]):
            with self.subTest(rows=rows):
                self.rows['a'] = rows
                self.calls.clear()
                result = await self.client.get_dataset_fields('a', expected_count=2)
                self.assertFalse(result['complete'])
                self.assertEqual(len(self.field_requests()), 3)
                self.assertIsNone(await self.client.store.get('datafields_ds', self.ds_key()))

    async def test_bad_cached_block_is_refetched_even_when_count_matches(self):
        for changes in ({'complete': False}, {'defects': ['prior drift']},
                        {'results': [field('same'), field('same')]}):
            with self.subTest(changes=changes):
                await self.seed('datafields_ds', self.ds_key(), {
                    'results': self.rows['a'], 'count': 2, 'declared_count': 2, 'complete': True, **changes})
                self.calls.clear()
                result = await self.client.get_dataset_fields('a', expected_count=2)
                self.assertTrue(result['complete'])
                self.assertTrue(result['cache_updated'])
                self.assertEqual(len(self.field_requests()), 1)

    async def test_retry_recovers_and_replaces_old_record(self):
        await self.seed_old()
        responses = [{'count': 2, 'results': [field('a1')]},
                     {'count': 2, 'results': self.rows['a']}]
        with patch.object(self.client, '_request_json_with_retries', AsyncMock(side_effect=responses)) as req:
            result = await self.client.get_dataset_fields('a', expected_count=2)
            self.assertTrue(result['complete'])
            self.assertEqual(req.await_count, 2)
        stored = await self.client.store.get('datafields_ds', self.ds_key())
        self.assertEqual(stored['results'], self.rows['a'])

    async def test_zero_fields_verified_but_missing_count_not_cached(self):
        with patch.object(self.client, '_request_json_with_retries', AsyncMock(return_value={'results': []})):
            result = await self.client.get_dataset_fields('a', expected_count=0)
            self.assertFalse(result['complete'])
            self.assertIsNone(await self.client.store.get('datafields_ds', self.ds_key()))
        with patch.object(self.client, '_request_json_with_retries', AsyncMock(return_value={'count': 0, 'results': []})):
            result = await self.client.get_dataset_fields('a', expected_count=0)
            self.assertTrue(result['complete'])
            self.assertIsNotNone(await self.client.store.get('datafields_ds', self.ds_key()))

    async def test_over_ten_thousand_splits_by_type(self):
        calls = []
        async def large(method, url, *, params, **kwargs):
            calls.append(params.copy())
            ftype = params.get('type')
            count = 10001 if ftype is None else {'MATRIX': 6000, 'VECTOR': 4001}.get(ftype, 0)
            start = params['offset']
            return {'count': count, 'results': [field(f'{ftype}-{i}')
                    for i in range(start, min(start + params['limit'], count))]}
        with patch.object(self.client, '_request_json_with_retries', large):
            result = await self.client.get_dataset_fields('a', expected_count=10001)
        self.assertTrue(result['complete'])
        self.assertEqual(result['count'], 10001)
        self.assertEqual({p['type'] for p in calls if 'type' in p},
                         {'MATRIX', 'VECTOR', 'GROUP', 'UNIVERSE', 'SYMBOL'})
        self.assertLess(max(p['offset'] for p in calls), 10000)

    async def test_complete_build_updates_then_prunes_in_both_paths(self):
        for background in (False, True):
            with self.subTest(background=background):
                await self.seed_old()
                deletes = []
                actual_delete = self.client.store.delete
                async def record_delete(ns, key):
                    aggregate = await self.client.store.get('datafields', self.aggregate_key())
                    self.assertTrue(aggregate['complete'])
                    self.assertEqual(aggregate['count'], 2)
                    deletes.append(key)
                    return await actual_delete(ns, key)
                with patch.object(self.client.store, 'delete', record_delete):
                    if background:
                        state = await self.background()
                        self.assertEqual(state['status'], 'complete')
                    else:
                        result = await self.client.build_full_datafield_catalogue()
                        self.assertTrue(result['cache_updated'])
                self.assertEqual(deletes, [self.ds_key('retired')])
                self.assertIsNone(await self.client.store.get('datafields_ds', self.ds_key('retired')))

    async def test_partial_build_resumes_only_failed_datasets(self):
        self.datasets.append({'id': 'b', 'fieldCount': 2})
        self.rows['b'] = [field('b1')]
        _, old, _ = await self.seed_old()
        state = await self.background()
        row = state['per_config']['USA/TOP3000/delay1']
        self.assertEqual(state['status'], 'partial')
        self.assertEqual(row['status'], 'incomplete')
        self.assertFalse(row['cache_updated'])
        self.assertTrue(row['per_dataset']['b']['defects'])
        self.assertEqual(self.client.store.path_for('datafields', self.aggregate_key()).read_bytes(), old)
        self.assertIsNotNone(await self.client.store.get('datafields_ds', self.ds_key()))
        self.rows['b'].append(field('b2'))
        self.calls.clear()
        state = await self.background()
        self.assertEqual(state['status'], 'complete')
        self.assertEqual([p['dataset.id'] for p in self.field_requests()], ['b'])

    async def test_empty_or_failed_listing_does_not_clear_catalogue(self):
        _, old, retired = await self.seed_old()
        for response in ({'count': 0, 'results': []}, {'count': 2, 'results': self.datasets}):
            with self.subTest(response=response), patch.object(self.client, '_request_json_with_retries',
                                                               AsyncMock(return_value=response)):
                result = await self.client.build_full_datafield_catalogue()
                self.assertFalse(result['complete'])
                self.assertFalse(result['cache_updated'])
                state = await self.background()
                self.assertEqual(state['status'], 'partial')
                self.assertEqual(self.client.store.path_for('datafields', self.aggregate_key()).read_bytes(), old)
                self.assertEqual(self.client.store.path_for('datafields_ds', self.ds_key('retired')).read_bytes(), retired)

    async def test_configuration_write_failure_never_prunes(self):
        _, old, retired = await self.seed_old()
        real_put = self.client.store.put
        async def failing_put(ns, *args, **kwargs):
            return 0 if ns == 'datafields' else await real_put(ns, *args, **kwargs)
        with patch.object(self.client.store, 'put', failing_put):
            result = await self.client.build_full_datafield_catalogue()
            self.assertFalse(result['cache_updated'])
            self.assertIn('persist', result['error'])
            state = await self.background()
            self.assertEqual(state['status'], 'partial')
        self.assertEqual(self.client.store.path_for('datafields', self.aggregate_key()).read_bytes(), old)
        self.assertEqual(self.client.store.path_for('datafields_ds', self.ds_key('retired')).read_bytes(), retired)

    async def test_dataset_write_failure_blocks_publication(self):
        _, old, _ = await self.seed_old()
        real_put = self.client.store.put
        async def failing_put(ns, *args, **kwargs):
            return 0 if ns == 'datafields_ds' else await real_put(ns, *args, **kwargs)
        with patch.object(self.client.store, 'put', failing_put):
            result = await self.client.build_full_datafield_catalogue()
        self.assertFalse(result['complete'])
        self.assertIn('persist', result['per_dataset']['a']['error'])
        self.assertEqual(self.client.store.path_for('datafields', self.aggregate_key()).read_bytes(), old)

    async def test_dedup_reuses_complete_only_and_labels_metrics(self):
        second = {**CFG, 'universe': 'TOP500'}
        await self.seed('datafields_ds', self.ds_key(cfg=second), {'old': True})
        state = await self.background([dict(CFG), second])
        self.assertEqual(state['per_config']['USA/TOP500/delay1']['status'], 'reused')
        self.assertEqual(len(self.field_requests()), 1)
        aggregate = await self.client.store.get('datafields', self.aggregate_key(second))
        self.assertEqual(aggregate['metrics_from_universe'], 'TOP3000')
        self.assertIsNone(await self.client.store.get('datafields_ds', self.ds_key(cfg=second)))

    async def test_failed_source_is_not_reused(self):
        self.rows['a'] = [field('a1')]
        second = {**CFG, 'universe': 'TOP500'}
        state = await self.background([dict(CFG), second])
        self.assertEqual(state['status'], 'partial')
        self.assertEqual({p['universe'] for p in self.field_requests()}, {'TOP3000', 'TOP500'})
        self.assertEqual(len(self.field_requests()), 6)
        self.assertTrue(all(r['status'] == 'incomplete' for r in state['per_config'].values()))

    async def test_all_mode_fetches_each_universe(self):
        second = {**CFG, 'universe': 'TOP500'}
        state = await self.background([dict(CFG), second], mode='all')
        self.assertEqual(state['status'], 'complete')
        self.assertEqual({p['universe'] for p in self.field_requests()}, {'TOP3000', 'TOP500'})
        result = await self.client.store.get('datafields', self.aggregate_key(second))
        self.assertNotIn('metrics_from_universe', result)
        self.assertIsNotNone(await self.client.store.get('datafields_ds', self.ds_key(cfg=second)))

    async def test_shared_field_across_datasets_is_not_incomplete(self):
        self.datasets.append({'id': 'b', 'fieldCount': 1})
        self.rows['b'] = [field('a1')]
        result = await self.client.build_full_datafield_catalogue()
        self.assertTrue(result['complete'])
        self.assertEqual(result['fields_in_two_datasets'], ['a1'])
        self.assertEqual(result['coverage'], 1.0)
        self.assertEqual(result['count'], 2)

    async def test_sync_wrapper_reports_failure_details(self):
        await self.seed_old()
        self.rows['a'] = [field('a1')]
        with patch.object(main, 'brain_client', self.client):
            result = await main.sync_platform_cache(scope='datafields_full', region='USA',
                                                    universe='TOP3000', delay=1, confirm=True)
        self.assertFalse(result['complete'])
        self.assertFalse(result['cache_updated'])
        self.assertEqual(result['incomplete_datasets'], ['a'])
        self.assertTrue(result['per_dataset']['a']['defects'])

    async def test_public_field_response_retains_incompleteness_details(self):
        self.rows['a'] = [field('a1')]
        with patch.object(main, 'brain_client', self.client):
            result = await main.get_datafields(region='USA', universe='TOP3000',
                                               dataset_id='a', filter_sharpe=False)
        self.assertFalse(result['complete'])
        self.assertFalse(result['cache_updated'])
        self.assertTrue(result['defects'])

    async def test_cleanup_failure_reports_that_catalogue_was_already_written(self):
        await self.seed_old()
        with patch.object(self.client.store, 'delete', AsyncMock(return_value=False)):
            state = await self.background()
        row = state['per_config']['USA/TOP3000/delay1']
        self.assertEqual(state['status'], 'partial')
        self.assertTrue(row['complete'])
        self.assertTrue(row['cache_updated'])
        self.assertIn('pruning', row['error'])
        self.assertIsNone(row['pruned'])
        aggregate = await self.client.store.get('datafields', self.aggregate_key())
        self.assertTrue(aggregate['complete'])
        self.assertIsNotNone(await self.client.store.get('datafields_ds', self.ds_key('retired')))

    async def test_all_regions_uses_options_only_when_requested(self):
        fake = SimpleNamespace(start_catalogue_build=AsyncMock(return_value={'started': True}),
                               get_platform_setting_options=AsyncMock(return_value={'instrument_options': [
                                   {'InstrumentType': 'EQUITY', 'Region': 'JPN', 'Delay': 1,
                                    'Universe': ['TOP500', 'TOP1000']},
                                   {'InstrumentType': 'EQUITY', 'Region': 'TWN', 'Delay': 0,
                                    'Universe': ['TOP300']},
                                   {'InstrumentType': 'OTHER', 'Region': 'USA', 'Delay': 1,
                                    'Universe': ['TOP3000']}]}))
        with patch.object(main, 'brain_client', fake):
            await main.build_datafield_catalogue(action='start')
            fake.get_platform_setting_options.assert_not_awaited()
            await main.build_datafield_catalogue(action='start', all_regions=True)
        cfgs = fake.start_catalogue_build.call_args.args[0]
        self.assertEqual([(x['region'], x['universe'], x['delay']) for x in cfgs],
                         [('JPN', 'TOP500', 1), ('JPN', 'TOP1000', 1), ('TWN', 'TOP300', 0)])

    async def test_empty_region_options_do_not_fall_back_to_in_use_configs(self):
        self.client.get_platform_setting_options = AsyncMock(return_value={'instrument_options': []})
        self.client._configs_in_use = Mock(return_value=[dict(CFG)])
        with patch.object(main, 'brain_client', self.client):
            result = await main.build_datafield_catalogue(action='start', all_regions=True)
        self.assertFalse(result['started'])
        self.client._configs_in_use.assert_not_called()
        self.assertFalse(self.calls)

    async def test_dedup_write_failure_preserves_target_blocks(self):
        second = {**CFG, 'universe': 'TOP500'}
        old = await self.seed('datafields_ds', self.ds_key(cfg=second), {'old': True})
        actual_put = self.client.store.put
        async def fail_target(ns, key, *args, **kwargs):
            if ns == 'datafields' and key == self.aggregate_key(second):
                return 0
            return await actual_put(ns, key, *args, **kwargs)
        with patch.object(self.client.store, 'put', fail_target):
            state = await self.background([dict(CFG), second])
        self.assertEqual(state['status'], 'partial')
        self.assertEqual(state['per_config']['USA/TOP500/delay1']['status'], 'incomplete')
        self.assertFalse(state['per_config']['USA/TOP500/delay1']['cache_updated'])
        self.assertEqual(self.client.store.path_for('datafields_ds', self.ds_key(cfg=second)).read_bytes(), old)

    async def test_field_count_disagrees_with_fresh_listing(self):
        old, _, _ = await self.seed_old()
        with patch.object(self.client, '_request_json_with_retries', AsyncMock(
                return_value={'count': 1, 'results': [field('a1')]})):
            result = await self.client.get_dataset_fields('a', expected_count=2)
        self.assertFalse(result['complete'])
        self.assertIn('fieldCount', ' '.join(result['defects']))
        self.assertEqual(self.client.store.path_for('datafields_ds', self.ds_key()).read_bytes(), old)


class ModeAndTagTests(unittest.IsolatedAsyncioTestCase):
    async def test_quick_forces_visualization_and_has_note_on_all_entries(self):
        model = main.SimulationSettings(simulationMode='quick', visualization=True)
        self.assertFalse(model.visualization)
        self.assertTrue(main.SimulationSettings(visualization=True).visualization)
        with patch.object(main, 'brain_client', bootstrap.FakeSingleClient()):
            result = await main.create_simulation(alpha_expression='rank(close)', simulation_mode='quick')
            self.assertEqual(result['simulation_mode'], 'QUICK')
            self.assertIn('FULL', result['quick_mode_note'])
        fake = bootstrap.FakeMultiClient()
        with patch.object(main, 'brain_client', fake), patch.object(main, '_wait_for_multisimulation_completion',
                                                                  bootstrap._fake_wait_for_multisim):
            for fn in (main.create_multi_simulation, main.submit_multi_simulation):
                result = await fn(['rank(close)', 'rank(volume)'], simulation_mode='quick', visualization=True)
                self.assertEqual(result['simulation_mode'], 'QUICK')
                self.assertIn('FULL', result['quick_mode_note'])
                self.assertTrue(all(r['settings']['visualization'] is False for r in fake.requests[-1][2]))
            result = await main.submit_multi_simulation(['rank(close)', 'rank(volume)'])
            self.assertEqual(result['simulation_mode'], 'FULL')
            self.assertNotIn('quick_mode_note', result)
            self.assertTrue(all('simulationMode' not in r['settings'] for r in fake.requests[-1][2]))

    async def test_explicit_full_fingerprint_matches_historical_and_does_not_mutate(self):
        old = {'type': 'REGULAR', 'settings': {'region': 'USA'}, 'regular': 'rank(close)'}
        explicit = copy.deepcopy(old)
        explicit['settings']['simulationMode'] = 'FULL'
        fingerprint = main.BrainApiClient._simulation_fingerprint
        self.assertEqual(fingerprint(old), fingerprint(explicit))
        self.assertEqual(explicit['settings']['simulationMode'], 'FULL')
        explicit['settings']['simulationMode'] = 'QUICK'
        self.assertNotEqual(fingerprint(old), fingerprint(explicit))

    async def test_tags_omitted_preserved_explicit_lists_respected(self):
        client = main.BrainApiClient.__new__(main.BrainApiClient)
        client.base_url = 'https://example.invalid'
        client.log = lambda *a, **k: None
        client.ensure_authenticated = AsyncMock()
        client.get_alpha_details = AsyncMock(return_value={'tags': ['old', 'PowerPoolSelected']})
        response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'id': 'A'})
        client._request = AsyncMock(return_value=response)
        await client.set_alpha_properties('A', name='renamed')
        self.assertEqual(client._request.call_args.kwargs['json']['tags'], ['old', 'PowerPoolSelected'])
        client.get_alpha_details.assert_awaited_once_with('A', force_refresh=True)
        for tags in ([], ['new'], ['PowerPoolSelected']):
            client.get_alpha_details.reset_mock()
            await client.set_alpha_properties('A', tags=tags)
            self.assertEqual(client._request.call_args.kwargs['json']['tags'], tags)
            client.get_alpha_details.assert_not_awaited()
        for bad in (RuntimeError('offline'), {}, {'error': 'failed'}, {'tags': None}):
            client._request.reset_mock()
            client.get_alpha_details.side_effect = bad if isinstance(bad, Exception) else None
            client.get_alpha_details.return_value = bad
            with self.assertRaises((ValueError, RuntimeError)):
                await client.set_alpha_properties('A', name='renamed')
            client._request.assert_not_awaited()


class ForumTests(unittest.IsolatedAsyncioTestCase):
    def click_url(self, target):
        # Ruby Marshal surrounds string values with non-printable bytes.
        encoded = base64.b64encode(b'\x04\x08{\x06I"url\x06:\x06ETI"' + target.encode() + b'\x06:\x06ET').decode().rstrip('=')
        return '/hc/en-us/search/click?data=' + quote(encoded, safe='') + '--signature'

    async def test_normal_and_tracking_urls(self):
        target = 'https://support.worldquantbrain.com/hc/en-us/community/posts/12345-example'
        click = self.click_url(target)
        for url in (target, click):
            self.assertEqual(forum._extract_post_id(url), '12345')
            self.assertEqual(forum._extract_locale(url), 'en-us')
        self.assertEqual(forum._unwrap_search_click_url(click), target)
        self.assertEqual(forum._extract_post_id('12345'), '12345')
        self.assertEqual(forum._unwrap_search_click_url('/search/click?data=broken'), '/search/click?data=broken')

    async def test_search_encodes_query_and_returns_real_link(self):
        target = 'https://support.worldquantbrain.com/hc/en-us/community/posts/12345-example'
        html = ('<ul class="search-results-list"><li class="search-result-list-item">'
                f'<h2 class="search-result-title"><a href="{self.click_url(target)}">Post</a></h2>'
                '</li></ul>')
        page = SimpleNamespace(set_default_timeout=Mock(), set_default_navigation_timeout=Mock(),
                               goto=AsyncMock(return_value=SimpleNamespace(status=200)),
                               wait_for_selector=AsyncMock(), content=AsyncMock(return_value=html))
        browser = SimpleNamespace(close=AsyncMock())
        context = SimpleNamespace(new_page=AsyncMock(return_value=page))
        client = forum.forum_client
        manager = AsyncMock()
        manager.__aenter__.return_value = object()
        with patch.object(forum, 'async_playwright', return_value=manager), patch.object(
                client, '_get_browser_context', AsyncMock(return_value=(browser, context))):
            result = await client.search_forum_posts('fake@example.invalid', 'fake', 'a&b #c', max_results=1)
        url = page.goto.call_args.args[0]
        self.assertEqual(parse_qs(urlparse(url).query)['query'], ['a&b #c'])
        self.assertIn(target, str(result))
        browser.close.assert_awaited()


if __name__ == '__main__':
    unittest.main(verbosity=2)
