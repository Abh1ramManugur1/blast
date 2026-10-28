import os
import tempfile
import uuid
from unittest import mock

from django.test import SimpleTestCase
from django.test import override_settings
from django.test import tag

from rest_framework import status

from host.ipfs import IPFSStore

API = 'http://ipfs:5001/api/v0'

def fake_response(status_code=200, json_data=None, content=b'', text='', chunks=None):
    """Stand-in for requests.Response with only the attributes IPFSStore uses."""
    response = mock.MagicMock()
    response.status_code = status_code
    response.ok = 200 <= status_code < 400
    response.text = text
    response.content = content
    response.json.return_value = json_data or {}
    response.iter_content.return_value = chunks or []
    response.__enter__.return_value = response
    return response


@override_settings(IPFS_API_URL='http://ipfs:5001/', IPFS_CLUSTER_API_URL='http://cluster0:9094/')
@mock.patch('host.ipfs.requests.request')
@mock.patch('host.ipfs.requests.post')
class IPFSUnitTests(SimpleTestCase):
    """
    Unit tests for the IPFSStore interface. All HTTP calls are mocked, so no IPFS peer is needed.
    """

    def setUp(self):
        self.store = IPFSStore()

    # __init__

    def test_init_normalizes_urls(self, post, request):
        self.assertEqual(self.store.api, API)
        self.assertEqual(self.store.cluster_api, 'http://cluster0:9094')

    # _request

    def test_request_posts_to_endpoint_with_normalized_path(self, post, request):
        post.return_value = fake_response()
        response = self.store._request('files/stat', '/test/a.txt/')
        self.assertIs(response, post.return_value)
        post.assert_called_once_with(f'{API}/files/stat', params={'arg': '/test/a.txt'}, timeout=300)

    def test_request_merges_params_without_mutating_caller(self, post, request):
        post.return_value = fake_response()
        params = {'long': 'true'}
        self.store._request('files/ls', 'test', params=params)
        post.assert_called_once_with(f'{API}/files/ls', params={'long': 'true', 'arg': '/test'}, timeout=300)
        self.assertEqual(params, {'long': 'true'})

    def test_request_without_path_sends_no_arg(self, post, request):
        post.return_value = fake_response()
        self.store._request('repo/gc')
        post.assert_called_once_with(f'{API}/repo/gc', params={}, timeout=300)

    def test_request_forwards_extra_kwargs(self, post, request):
        post.return_value = fake_response()
        self.store._request('files/read', 'a.txt', stream=True)
        post.assert_called_once_with(f'{API}/files/read', params={'arg': '/a.txt'}, timeout=300, stream=True)

    def test_request_raises_on_error_status(self, post, request):
        post.return_value = fake_response(500, text='file does not exist')
        with self.assertRaisesRegex(IOError, 'files/stat failed with 500: file does not exist'):
            self.store._request('files/stat', 'missing.txt')

    # _cluster

    def test_cluster_sends_method_and_url(self, post, request):
        request.return_value = fake_response()
        response = self.store._cluster('DELETE', 'pins/Qm1')
        self.assertIs(response, request.return_value)
        request.assert_called_once_with('DELETE', f'{'http://cluster0:9094'}/pins/Qm1', timeout=300)

    def test_cluster_raises_on_error_status(self, post, request):
        request.return_value = fake_response(404, text='not found')
        with self.assertRaisesRegex(IOError, 'pins/Qm1 failed with 404: not found'):
            self.store._cluster('GET', 'pins/Qm1')

    # _cid

    def test_cid_reads_hash_from_stat(self, post, request):
        post.return_value = fake_response(json_data={'Hash': 'Qm1'})
        self.assertEqual(self.store._cid('a.txt'), 'Qm1')
        post.assert_called_once_with(f'{API}/files/stat', params={'arg': '/a.txt'}, timeout=300)

    # put_object

    def test_put_writes_then_pins(self, post, request):
        post.side_effect = [fake_response(), fake_response(json_data={'Hash': 'Qm1'})]
        request.return_value = fake_response()
        self.assertEqual(self.store.put_object('test/a.txt', b'data'), 'Qm1')
        self.assertEqual(post.call_args_list, [
            mock.call(f'{API}/files/write',
                      params={'create': 'true', 'parents': 'true', 'truncate': 'true', 'arg': '/test/a.txt'},
                      timeout=300, files={'file': b'data'}),
            mock.call(f'{API}/files/stat', params={'arg': '/test/a.txt'}, timeout=300),
        ])
        request.assert_called_once_with('POST', f'{'http://cluster0:9094'}/pins/Qm1', timeout=300)

    def test_put_does_not_pin_when_write_fails(self, post, request):
        post.return_value = fake_response(500, text='write error')
        with self.assertRaises(IOError):
            self.store.put_object('test/a.txt', b'data')
        request.assert_not_called()

    def test_put_raises_when_pin_fails(self, post, request):
        post.side_effect = [fake_response(), fake_response(json_data={'Hash': 'Qm1'})]
        request.return_value = fake_response(500, text='cluster error')
        with self.assertRaises(IOError):
            self.store.put_object('test/a.txt', b'data')

    # get_object

    def test_get_returns_content(self, post, request):
        post.return_value = fake_response(content=b'data')
        self.assertEqual(self.store.get_object('a.txt'), b'data')
        post.assert_called_once_with(f'{API}/files/read', params={'arg': '/a.txt'}, timeout=300)

    # download_object

    def test_download_streams_chunks_to_file(self, post, request):
        post.return_value = fake_response(chunks=[b'ab', b'cd', b'e'])
        with tempfile.TemporaryDirectory() as tmp_dir:
            file_path = os.path.join(tmp_dir, 'a.txt')
            self.store.download_object('a.txt', file_path)
            with open(file_path, 'rb') as fp:
                self.assertEqual(fp.read(), b'abcde')
        post.assert_called_once_with(f'{API}/files/read', params={'arg': '/a.txt'}, timeout=300, stream=True)
        post.return_value.iter_content.assert_called_once_with(chunk_size=1 << 20)

    def test_download_error_creates_no_file(self, post, request):
        post.return_value = fake_response(500, text='file does not exist')
        with tempfile.TemporaryDirectory() as tmp_dir:
            file_path = os.path.join(tmp_dir, 'a.txt')
            with self.assertRaises(IOError):
                self.store.download_object('a.txt', file_path)
            self.assertFalse(os.path.exists(file_path))

    # object_exists

    def test_exists_true(self, post, request):
        post.return_value = fake_response()
        self.assertIs(self.store.object_exists('a.txt'), True)

    def test_exists_false_when_missing(self, post, request):
        post.return_value = fake_response(500, text='file does not exist')
        self.assertIs(self.store.object_exists('a.txt'), False)

    def test_exists_reraises_other_errors(self, post, request):
        post.return_value = fake_response(500, text='permission denied')
        with self.assertRaises(IOError):
            self.store.object_exists('a.txt')

    def test_exists_reraises_connection_errors(self, post, request):
        post.side_effect = ConnectionError('connection refused')
        with self.assertRaises(IOError):
            self.store.object_exists('a.txt')

    # delete_object

    def test_delete_removes_unpins_and_collects_garbage(self, post, request):
        post.side_effect = [fake_response(json_data={'Hash': 'Qm1'}), fake_response()]
        request.return_value = fake_response()
        self.store.delete_object('test/a.txt')
        self.assertEqual(post.call_args_list, [
            mock.call(f'{API}/files/stat', params={'arg': '/test/a.txt'}, timeout=300),
            mock.call(f'{API}/files/rm', params={'recursive': 'true', 'force': 'true', 'arg': '/test/a.txt'},
                      timeout=300),
        ])
        self.assertEqual(request.call_args_list, [
            mock.call('DELETE', f'{'http://cluster0:9094'}/pins/Qm1', timeout=300),
            mock.call('POST', f'{'http://cluster0:9094'}/ipfs/gc', timeout=300),
        ])

    def test_delete_missing_changes_nothing(self, post, request):
        post.return_value = fake_response(500, text='file does not exist')
        with self.assertRaises(IOError):
            self.store.delete_object('missing.txt')
        post.assert_called_once()  # only the stat lookup
        request.assert_not_called()


@tag('ipfs')
class IPFSIntegrationTest(SimpleTestCase):
    """
    Integration tests against a live Kubo node and IPFS Cluster.
    """

    def setUp(self):
        self.store = IPFSStore()

        self.path = f'test/{uuid.uuid4().hex}/hello.txt'
        self.data = b'hello ' + uuid.uuid4().bytes
        self.cid = self.store.put_object(self.path, self.data)

    def tearDown(self):
        try:
            if self.store.object_exists(self.path):
                self.store.delete_object(self.path)
        except IOError:
            pass

    def test_put_outputs_cid(self):
        self.assertTrue(self.cid)
        self.assertEqual(self.cid, self.store._cid(self.path))

    def test_pin_get(self):
        # uploaded file is part of cluster's global pin set
        response = self.store._cluster('GET', f'allocations/{self.cid}')
        self.assertTrue(response.status_code == status.HTTP_200_OK)

    def test_object_exists(self):
        self.assertTrue(self.store.object_exists(self.path))

    def test_missing_obj_existence(self):
        self.assertFalse(self.store.object_exists(f'test/{uuid.uuid4().hex}/missing.txt'))

    def test_get_object(self):
        data = self.store.get_object(self.path)
        self.assertEqual(data, self.data)

    def test_get_by_cid(self):
        # Content is retrievable by its address, independent of its MFS path
        response = self.store._request('cat', params={'arg': self.cid})
        self.assertEqual(response.content, self.data)

    def test_download_object(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            file_path = os.path.join(tmp_dir, 'hello.txt')
            self.store.download_object(self.path, file_path)
            with open(file_path, 'rb') as fp:
                self.assertEqual(fp.read(), self.data)

    def test_overwrite_changes_cid(self):
        # put_object never unpins the replaced CID, so unpin it after the test
        self.addCleanup(self.store._cluster, 'DELETE', f'pins/{self.cid}')
        new_cid = self.store.put_object(self.path, b'changed ' + uuid.uuid4().bytes)
        self.assertNotEqual(new_cid, self.cid)
        # The path resolves to the new CID; the old version stays retrievable by CID
        self.assertEqual(self.store._cid(self.path), new_cid)
        response = self.store._request('cat', params={'arg': self.cid})
        self.assertEqual(response.content, self.data)

    def test_overwrite_with_shorter_data_truncates(self):
        long_cid = self.store.put_object(self.path, self.data * 512)
        self.addCleanup(self.store._cluster, 'DELETE', f'pins/{long_cid}')
        self.store.put_object(self.path, self.data)
        self.assertEqual(self.store.get_object(self.path), self.data)

    def test_list_directory(self):
        # Upload a second file next to the first
        directory = os.path.dirname(self.path)
        other_path = f'{directory}/other.txt'
        other_cid = self.store.put_object(other_path, b'other ' + uuid.uuid4().bytes)
        self.addCleanup(self.store.delete_object, other_path)
        # List the directory
        response = self.store._request('files/ls', directory, params={'long': 'true'})
        entries = {entry['Name']: entry['Hash'] for entry in response.json()['Entries']}
        self.assertEqual(entries, {'hello.txt': self.cid, 'other.txt': other_cid})

    def test_import_external_content(self):
        # Add content through the Kubo API directly, outside put_object
        data = b'external ' + uuid.uuid4().bytes
        response = self.store._request('add', files={'file': data}, params={'pin': 'false'})
        cid = response.json()['Hash']
        self.store._cluster('POST', f'pins/{cid}')
        # Link it into MFS
        path = f'{os.path.dirname(self.path)}/external.txt'
        self.store._request('files/cp', params={'arg': [f'/ipfs/{cid}', f'/{path}']})
        self.addCleanup(self.store.delete_object, path)
        # The store reads it like any other object
        self.assertTrue(self.store.object_exists(path))
        self.assertEqual(self.store.get_object(path), data)
        self.assertEqual(self.store._cid(path), cid)

    def test_delete_object(self):
        self.store.delete_object(self.path)
        self.assertFalse(self.store.object_exists(self.path))
        # The content is no longer pinned on the cluster
        with self.assertRaises(IOError):
            self.store._cluster('GET', f'allocations/{self.cid}')

    def test_delete_missing_file(self):
        with self.assertRaises(IOError):
            self.store.delete_object(f'test/{uuid.uuid4().hex}/missing.txt')

    def test_delete_keeps_siblings(self):
        other_path = f'{os.path.dirname(self.path)}/other.txt'
        other_data = b'other ' + uuid.uuid4().bytes
        other_cid = self.store.put_object(other_path, other_data)
        self.addCleanup(self.store.delete_object, other_path)
        self.store.delete_object(self.path)
        self.assertEqual(self.store.get_object(other_path), other_data)
        response = self.store._cluster('GET', f'allocations/{other_cid}')
        self.assertTrue(response.status_code == status.HTTP_200_OK)

    def test_object_exists_raises_when_peer_down(self):
        with override_settings(IPFS_API_URL='http://127.0.0.1:1'):
            with self.assertRaises(IOError):
                IPFSStore().object_exists(self.path)