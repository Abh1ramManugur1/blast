import requests
from django.conf import settings

class IPFSStore:
    """
    Keep objects in IPFS MFS via Kubo API at 5001
    """

    def __init__(self, *args, **kwargs):
        self.api = settings.IPFS_API_URL.rstrip('/') + '/api/v0'
        self.cluster_api = settings.IPFS_CLUSTER_API_URL.rstrip('/')

    def _request(self, endpoint, path=None, params=None, **kwargs):
        # RPC API only accepts POST
        params = dict(params or {})
        if path is not None:
            params['arg'] = '/' + path.strip('/')

        response = requests.post(f'{self.api}/{endpoint}', params=params, timeout=300, **kwargs)
        if not response.ok:
            raise IOError(f'IPFS {endpoint} failed with {response.status_code}: {response.text}')
        return response

    def _cluster(self, method, endpoint):
        response = requests.request(method, f'{self.cluster_api}/{endpoint}', timeout=300)
        if not response.ok:
            raise IOError(f'IPFS Cluster {endpoint} failed with {response.status_code}: {response.text}')
        return response

    def _cid(self, path):
        return self._request('files/stat', path).json()['Hash']

    def put_object(self, path, data):
        self._request('files/write', path, files={'file':data}, params={'create': 'true', 'parents': 'true', 'truncate': 'true'})

        # pin content on cluster's peers, replicate
        cid = self._cid(path)
        self._cluster('POST', f'pins/{cid}')
        return cid

    def get_object(self, path):
        return self._request('files/read', path).content

    def download_object(self, path, file_path):
        with self._request('files/read', path, stream=True) as response, open(file_path, 'wb') as fp:
            for chunk in response.iter_content(chunk_size=1 << 20):
                fp.write(chunk)

    def object_exists(self, path):
        try:
            self._request('files/stat', path)
            return True
        except IOError as err: # no existing wrapper to read IOError message, used string parsing
            if 'does not exist' in str(err):
                return False
            raise

    def delete_object(self, path):
        cid = self._cid(path)
        self._request('files/rm', path, params={'recursive':'true', 'force':'true'})
        # self._request('repo/gc', params={'quiet':'true'})
        self._cluster('DELETE', f'pins/{cid}')

        self._cluster('POST', 'ipfs/gc')
        