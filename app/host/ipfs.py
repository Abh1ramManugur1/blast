import requests
from django.conf import settings

class IPFSStore:
    """
    Keep objects in IPFS MFS via Kubo API at 5001
    """

    def __init__(self, *args, **kwargs):
        self.api = settings.IPFS_API_URL.rstrip('/') + '/api/v0'

    def _request(self, endpoint, path=None, params=None, **kwargs):
        # RPC API only accepts POST
        params = dict(params or {})
        if path is not None:
            params['arg'] = '/' + path.strip('/')

        response = requests.post(f'{self.api}/{endpoint}', params=params, timeout=300, **kwargs)
        if not response.ok:
            raise IOError(f'IPS {endpoint} failed with {response.status_code}: {response.text}')
        return response

    def put_object(self, path, data):
        self._request('files/write', path, files={'file':data}, params={'create': 'true', 'parents': 'true', 'truncate': 'true'})

    def get_object(self, path):
        return self._request('files/read', path).content

    def download_object(self, path, file_path):
        with self._request('files/read', path, stream=True) as response, open(file_path, 'wb') as fp:
            for chunk in response.iter_content(chunk_size=1 << 20):
                fp.write(chunk)

    def object_exists(self, path):
        try:
            self._request('files/stat', path)
            return
        except IOError as err:
            if 'does not exist' in str(err):
                return False
            raise

    def delete_object(self, path):
        self._request('files/rm', path, params={'recursive':'true', 'force':'true'})
        self._request('repo/gc', params={'quiet':'true'})