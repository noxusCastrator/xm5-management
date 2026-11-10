"""Search semantics and bounded SQL round trips, using a synthetic library."""
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import unittest

from xm5lib.application import Application


class SearchQueries(unittest.TestCase):
    def test_keyword_search_batches_locations_and_preserves_unicode_and_paging(self):
        with tempfile.TemporaryDirectory(prefix='xm5-search-') as temporary:
            app = Application(Path(temporary))
            self.addCleanup(app.close)
            with app._connection() as connection:
                for i in range(250):
                    data = {'metadata': {'title': 'Straße', 'artist': '歌手', 'album': 'Album'},
                            'note': 'Favorite note' if i == 2 else ''}
                    key = f'{i:04d}'
                    connection.execute('INSERT INTO resources VALUES(?,?,?,?,?)',
                                       ('track', key, 1, json.dumps(data), 0))
                    connection.execute('INSERT INTO locations VALUES(?,?,?,?,?)',
                                       (f'/music/{key}.flac', key, 1, '/music', 'fixture'))
            original = app._connection
            statements = []
            @contextmanager
            def traced():
                with original() as connection:
                    connection.set_trace_callback(statements.append)
                    yield connection
            app._connection = traced
            result = app.query('tracks.search', {'text': 'STRASSE', 'offset': 10, 'limit': 3})
            self.assertEqual(result['total'], 250)
            self.assertEqual([t['id'] for t in result['tracks']], ['0010', '0011', '0012'])
            self.assertTrue(all(t['locations'][0]['present'] for t in result['tracks']))
            self.assertLessEqual(len(statements), 8, 'Keyword search must not query each track separately')
            self.assertEqual(app.query('tracks.search', {'text': 'favorite NOTE'})['total'], 1)
            self.assertEqual(app.query('tracks.search', {'text': '/music/0002'})['total'], 1)
            self.assertEqual(app.query('tracks.search', {'text': '歌手'})['total'], 250)
