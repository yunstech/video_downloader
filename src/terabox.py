import os
import uuid
import logging
import base64
import random
from urllib.parse import quote
import cloudscraper
import requests

logger = logging.getLogger(__name__)

TERABOX_DOMAINS = (
    "terabox.com", "1024terabox.com", "freeterabox.com", "nephobox.com",
    "terabox.app", "teraboxapp.com", "4funbox.com", "mirrobox.com",
    "momerybox.com", "teraboxlink.com",
)

class TeraboxFile:
    def __init__(self):
        self.r = requests.Session()
        self.sc = cloudscraper.create_scraper()
        self.headers = {
            'user-agent': 'Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36'
        }
        self.result = {'status': 'failed', 'sign': '', 'timestamp': '', 'shareid': '', 'uk': '', 'list': []}

    def search(self, url: str):
        req = self.r.get(url, allow_redirects=True)
        import re
        m = re.search(r'surl=([^&]+)', str(req.url))
        if not m:
            self.result['status'] = 'failed'
            return
        self.short_url = m.group(1)
        self.getMainFile()
        self.getSign()

    def getSign(self):
        api = 'https://terabox.hnn.workers.dev/api/get-info'
        post_url = f'{api}?shorturl={self.short_url}&pwd='
        headers_post = {
            'accept-language': 'en-US,en;q=0.9,id;q=0.8',
            'referer': 'https://terabox.hnn.workers.dev/',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-origin',
            'user-agent': self.headers['user-agent'],
        }
        try:
            pos = self.sc.get(post_url, headers=headers_post, allow_redirects=True).json()
            if pos.get('ok'):
                self.result['sign'] = pos['sign']
                self.result['timestamp'] = pos['timestamp']
                self.result['status'] = 'success'
            else:
                self.result['status'] = 'failed'
        except Exception:
            self.result['status'] = 'failed'

    def getMainFile(self):
        url = f'https://www.terabox.com/api/shorturlinfo?app_id=250528&shorturl=1{self.short_url}&root=1'
        req = self.r.get(url, headers=self.headers, cookies={'cookie': ''}).json()
        all_file = self.packData(req, self.short_url)
        if len(all_file):
            self.result['shareid'] = req['shareid']
            self.result['uk'] = req['uk']
            self.result['list'] = all_file

    def getChildFile(self, short_url, path='', root='0'):
        params = {'app_id': '250528', 'shorturl': short_url, 'root': root, 'dir': path}
        url = 'https://www.terabox.com/share/list?' + '&'.join([f'{a}={b}' for a, b in params.items()])
        req = self.r.get(url, headers=self.headers, cookies={'cookie': ''}).json()
        return self.packData(req, short_url)

    def packData(self, req, short_url):
        all_file = [{
            'is_dir': item['isdir'],
            'path': item['path'],
            'fs_id': item['fs_id'],
            'name': item['server_filename'],
            'type': self.checkFileType(item['server_filename']) if not bool(int(item.get('isdir'))) else 'other',
            'size': item.get('size') if not bool(int(item.get('isdir'))) else '',
            'image': item.get('thumbs', {}).get('url3', '') if not bool(int(item.get('isdir'))) else '',
            'list': self.getChildFile(short_url, item['path'], '0') if item.get('isdir') else [],
        } for item in req.get('list', [])]
        return all_file

    def checkFileType(self, name):
        name = name.lower()
        if any(ext in name for ext in ['.mp4', '.mov', '.m4v', '.mkv', '.asf', '.avi', '.wmv', '.m2ts', '.3g2']):
            return 'video'
        elif any(ext in name for ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg']):
            return 'image'
        elif any(ext in name for ext in ['.pdf', '.docx', '.zip', '.rar', '.7z']):
            return 'file'
        else:
            return 'other'

class TeraboxLink:
    def __init__(self, shareid, uk, sign, timestamp, fs_id):
        self.domain = 'https://terabox.hnn.workers.dev/'
        self.api = f'{self.domain}api'
        self.sc = cloudscraper.create_scraper()
        self.headers = {
            'accept-language': 'en-US,en;q=0.9,id;q=0.8',
            'referer': self.domain,
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-origin',
            'user-agent': 'Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Mobile Safari/537.36',
        }
        self.result = {'status': 'failed', 'download_link': {}}
        self.params = {
            'shareid': str(shareid),
            'uk': str(uk),
            'sign': str(sign),
            'timestamp': str(timestamp),
            'fs_id': str(fs_id),
        }
        self.base_urls = [
            'plain-grass-58b2.comprehensiveaquamarine',
            'royal-block-6609.ninnetta7875',
            'bold-hall-f23e.7rochelle',
            'winter-thunder-0360.belitawhite',
            'fragrant-term-0df9.elviraeducational',
            'purple-glitter-924b.miguelalocal'
        ]

    def generate(self):
        params = self.params
        try:
            url_1 = f'{self.api}/get-download'
            pos_1 = self.sc.post(url_1, json=params, headers=self.headers, allow_redirects=True).json()
            self.result['download_link'].update({'url_1': pos_1['downloadLink']})
        except Exception as e:
            print(e)
        try:
            url_2 = f'{self.api}/get-downloadp'
            pos_2 = self.sc.post(url_2, json=params, headers=self.headers, allow_redirects=True).json()
            self.result['download_link'].update({'url_2': self.wrap_url(pos_2['downloadLink'])})
        except Exception as e:
            print(e)
        if len(list(self.result['download_link'].keys())) != 0:
            self.result['status'] = 'success'

    def wrap_url(self, original_url):
        selected_base = random.choice(self.base_urls)
        quoted_url = quote(original_url, safe='')
        b64_encoded = base64.urlsafe_b64encode(quoted_url.encode()).decode()
        return f'https://{selected_base}.workers.dev/?url={b64_encoded}'
