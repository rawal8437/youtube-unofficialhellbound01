from datetime import datetime
from http.cookiejar import CookieJar, LoadError, MozillaCookieJar
from os.path import expanduser
from typing import Any, Iterable, Iterator, Mapping, Optional, Type, cast
import hashlib
import json
import logging

from typing_extensions import Final
import requests

from .constants import (BROWSE_AJAX_URL, HISTORY_URL, HOMEPAGE_URL,
                        SEARCH_HISTORY_URL, SERVICE_AJAX_URL, USER_AGENT,
                        WATCH_LATER_URL)
from .download import DownloadMixin
from .exceptions import AuthenticationError, UnexpectedError
from .initial import initial_data, initial_guide_data
from .login import YouTubeLogin
from .typing import HasStringCode
from .typing.browse_ajax import BrowseAJAXSequence
from .typing.guide_data import SectionItemDict
from .typing.playlist import PlaylistInfo
from .util import context_client_body, path as at_path
from .ytcfg import find_ytcfg, ytcfg_headers

__all__ = ('YouTube', )


class YouTube(DownloadMixin):
    def __init__(self,
                 username: Optional[str] = None,
                 password: Optional[str] = None,
                 netrc_file: Optional[str] = None,
                 cookies_path: Optional[str] = None,
                 cookiejar_cls: Type[CookieJar] = MozillaCookieJar):
        if not netrc_file:
            self.netrc_file = expanduser('~/.netrc')
        else:
            self.netrc_file = netrc_file
        if not cookies_path:
            cookies_path = expanduser('~/.config/ytch-cookies.txt')

        self.username = username
        self.password = password
        self._log: Final = logging.getLogger('youtube-unofficial')
        self._favorites_playlist_id: Optional[str] = None
        self._sess = requests.Session()
        self._init_cookiejar(cookies_path, cls=cookiejar_cls)
        self._sess.cookies = self._cj  # type: ignore[assignment]
        self._sess.headers.update({
            'User-Agent': USER_AGENT,
        })
        self._login_handler = YouTubeLogin(self._sess, self._cj, username)

    @property
    def _logged_in(self):
        return self._login_handler._logged_in  # pylint: disable=protected-access

    def _init_cookiejar(self,
                        path: str,
                        cls: Type[CookieJar] = MozillaCookieJar) -> None:
        self._log.debug('Initialising cookie jar (%s) at %s', cls.__name__,
                        path)
        try:
            with open(path):
                pass
        except IOError:
            with open(path, 'w+'):
                pass
        try:
            self._cj = cls(path)  # type: ignore[arg-type]
        except TypeError:
            self._cj = cls()
        if hasattr(self._cj, 'load'):
            try:
                self._cj.load()  # type: ignore[attr-defined]
            except LoadError:
                self._log.debug('File %s for cookies does not yet exist', path)

    def login(self) -> None:
        self._login_handler.login()

    def remove_set_video_id_from_playlist(
            self,
            playlist_id: str,
            set_video_id: str,
            csn: Optional[str] = None,
            xsrf_token: Optional[str] = None,
            headers: Optional[Mapping[str, str]] = None) -> None:
        """Removes a video from a playlist. The set_video_id is NOT the same as
        the video ID."""
        if not self._logged_in:
            raise AuthenticationError('This method requires a call to '
                                      'login() first')

        if not headers or not csn or not xsrf_token:
            soup = self._download_page_soup(WATCH_LATER_URL)
            ytcfg = find_ytcfg(soup)
            headers = ytcfg_headers(ytcfg)

        params = {'name': 'playlistEditEndpoint'}
        form_data = {
            'sej':
            json.dumps({
                'clickTrackingParams': '',
                'commandMetadata': {
                    'webCommandMetadata': {
                        'url': '/service_ajax',
                        'sendPost': True
                    }
                },
                'playlistEditEndpoint': {
                    'playlistId':
                    playlist_id,
                    'actions': [{
                        'setVideoId': set_video_id,
                        'action': 'ACTION_REMOVE_VIDEO'
                    }],
                    'params':
                    'CAE%3D',
                    'clientActions': [{
                        'playlistRemoveVideosAction': {
                            'setVideoIds': [set_video_id]
                        }
                    }]
                }
            }),
            'csn':
            csn or ytcfg['EVENT_ID'],
            'session_token':
            xsrf_token or ytcfg['XSRF_TOKEN']
        }
        data = cast(
            HasStringCode,
            self._download_page(SERVICE_AJAX_URL,
                                method='post',
                                data=form_data,
                                params=params,
                                return_json=True,
                                headers=headers))
        if data['code'] != 'SUCCESS':
            raise UnexpectedError(
                'Failed to delete video from Watch Later playlist')

    def clear_watch_history(self) -> None:
        """Clears watch history."""
        if not self._logged_in:
            raise AuthenticationError('This method requires a call to '
                                      'login() first')

        content = self._download_page_soup(HISTORY_URL)
        ytcfg = find_ytcfg(content)
        headers = ytcfg_headers(ytcfg)
        headers['x-spf-previous'] = HISTORY_URL
        headers['x-spf-referer'] = HISTORY_URL
        init_data = initial_data(content)
        params = {'name': 'feedbackEndpoint'}
        try:
            data = {
                'sej':
                json.dumps(
                    init_data['contents']['twoColumnBrowseResultsRenderer']
                    ['secondaryContents']['browseFeedActionsRenderer']
                    ['contents'][2]['buttonRenderer']['navigationEndpoint']
                    ['confirmDialogEndpoint']['content']
                    ['confirmDialogRenderer']['confirmButton']
                    ['buttonRenderer']['serviceEndpoint']),
                'csn':
                ytcfg['EVENT_ID'],
                'session_token':
                ytcfg['XSRF_TOKEN']
            }
        except KeyError:
            self._log.debug('Clear button is likely disabled. History is '
                            'likely empty')
            return

        self._download_page(SERVICE_AJAX_URL,
                            params=params,
                            data=data,
                            headers=headers,
                            return_json=True,
                            method='post')
        self._log.info('Successfully cleared history')

    def get_favorites_playlist_id(self) -> str:
        """Get the Favourites playlist ID."""
        if not self._logged_in:
            raise AuthenticationError('This method requires a call to '
                                      'login() first')
        if self._favorites_playlist_id:
            return self._favorites_playlist_id

        def check_section_items(
                items: Iterable[SectionItemDict]) -> Optional[str]:
            for item in items:
                if 'guideEntryRenderer' in item:
                    if (item['guideEntryRenderer']['icon']['iconType']
                        ) == 'LIKES_PLAYLIST':
                        return (item['guideEntryRenderer']['entryData']
                                ['guideEntryData']['guideEntryId'])
                elif 'guideCollapsibleEntryRenderer' in item:
                    renderer = item['guideCollapsibleEntryRenderer']
                    for e_item in renderer['expandableItems']:
                        if e_item['guideEntryRenderer']['icon'][
                                'iconType'] == 'LIKES_PLAYLIST':
                            return (e_item['guideEntryRenderer']['entryData']
                                    ['guideEntryData']['guideEntryId'])
            return None

        content = self._download_page_soup(HOMEPAGE_URL)
        gd = initial_guide_data(content)
        section_items = (
            gd['items'][0]['guideSectionRenderer']['items'][4]
            ['guideCollapsibleSectionEntryRenderer']['sectionItems'])

        found = check_section_items(section_items)
        if found:
            self._favorites_playlist_id = found
            return self._favorites_playlist_id

        expandable_items = (section_items[-1]['guideCollapsibleEntryRenderer']
                            ['expandableItems'])
        found = check_section_items(expandable_items)
        if not found:
            raise ValueError('Could not determine favourites playlist ID')

        self._favorites_playlist_id = found
        self._log.debug('Got favourites playlist ID: %s',
                        self._favorites_playlist_id)

        return self._favorites_playlist_id

    def clear_favorites(self) -> None:
        """Removes all videos from the Favourites playlist."""
        if not self._logged_in:
            raise AuthenticationError('This method requires a call to '
                                      'login() first')

        self.clear_playlist(self.get_favorites_playlist_id())

    def get_playlist_info(self, playlist_id: str) -> Iterator[PlaylistInfo]:
        """Get playlist information given a playlist ID."""
        if not self._logged_in:
            raise AuthenticationError('This method requires a call to '
                                      'login() first')

        url = 'https://www.youtube.com/playlist?list={}'.format(playlist_id)
        content = self._download_page_soup(url)
        ytcfg = find_ytcfg(content)
        headers = ytcfg_headers(ytcfg)
        yt_init_data = initial_data(content)

        video_list_renderer = (
            yt_init_data['contents']['twoColumnBrowseResultsRenderer']['tabs']
            [0]['tabRenderer']['content']['sectionListRenderer']['contents'][0]
            ['itemSectionRenderer']['contents'][0]['playlistVideoListRenderer']
        )
        try:
            yield from video_list_renderer['contents']
        except KeyError:
            yield from []

        next_cont = continuation = itct = None
        try:
            next_cont = video_list_renderer['continuations'][0][
                'nextContinuationData']
            continuation = next_cont['continuation']
            itct = next_cont['clickTrackingParams']
        except KeyError:
            pass

        if continuation and itct:
            while True:
                params = {
                    'ctoken': continuation,
                    'continuation': continuation,
                    'itct': itct
                }
                contents = cast(
                    BrowseAJAXSequence,
                    self._download_page(BROWSE_AJAX_URL,
                                        params=params,
                                        return_json=True,
                                        headers=headers))
                response = contents[1]['response']
                yield from (response['continuationContents']
                            ['playlistVideoListContinuation']['contents'])

                try:
                    continuations = (
                        response['continuationContents']
                        ['playlistVideoListContinuation']['continuations'])
                except KeyError:
                    break
                next_cont = continuations[0]['nextContinuationData']
                itct = next_cont['clickTrackingParams']
                continuation = next_cont['continuation']

    def clear_playlist(self, playlist_id: str) -> None:
        """
        Removes all videos from the specified playlist.

        Use `WL` for Watch Later.
        """
        if not self._logged_in:
            raise AuthenticationError('This method requires a call to '
                                      'login() first')

        playlist_info = self.get_playlist_info(playlist_id)
        url = 'https://www.youtube.com/playlist?list={}'.format(playlist_id)
        content = self._download_page_soup(url)
        ytcfg = find_ytcfg(content)
        headers = ytcfg_headers(ytcfg)
        csn = ytcfg['EVENT_ID']
        xsrf_token = ytcfg['XSRF_TOKEN']

        try:
            set_video_ids = list(
                map(lambda x: x['playlistVideoRenderer']['setVideoId'],
                    playlist_info))
        except KeyError:
            self._log.info('Caught KeyError. This probably means the playlist '
                           'is empty.')
            return

        for set_video_id in set_video_ids:
            self._log.debug('Deleting from playlist: set_video_id = %s',
                            set_video_id)
            self.remove_set_video_id_from_playlist(playlist_id,
                                                   set_video_id,
                                                   csn,
                                                   xsrf_token,
                                                   headers=headers)

    def clear_watch_later(self) -> None:
        """Removes all videos from the 'Watch Later' playlist."""
        self.clear_playlist('WL')

    def remove_video_id_from_favorites(
            self,
            video_id: str,
            headers: Optional[Mapping[str, str]] = None) -> None:
        """Removes a video from Favourites by video ID."""
        playlist_id = self.get_favorites_playlist_id()
        playlist_info = self.get_playlist_info(playlist_id)
        url = 'https://www.youtube.com/playlist?list={}'.format(playlist_id)
        content = self._download_page_soup(url)
        ytcfg = find_ytcfg(content)
        headers = ytcfg_headers(ytcfg)

        try:
            entry = list(
                filter(
                    lambda x: (x['playlistVideoRenderer']['navigationEndpoint']
                               ['watchEndpoint']['videoId']) == video_id,
                    playlist_info))[0]
        except IndexError:
            return

        set_video_id = entry['playlistVideoRenderer']['setVideoId']

        self.remove_set_video_id_from_playlist(playlist_id,
                                               set_video_id,
                                               ytcfg['EVENT_ID'],
                                               ytcfg['XSRF_TOKEN'],
                                               headers=headers)

    def get_history_info(self) -> Iterator[Mapping[str, Any]]:
        """Get information about the History playlist."""
        if not self._logged_in:
            raise AuthenticationError('This method requires a call to '
                                      'login() first')

        content = self._download_page_soup(HISTORY_URL)
        init_data = initial_data(content)
        ytcfg = find_ytcfg(content)
        headers = ytcfg_headers(ytcfg)

        section_list_renderer = (
            init_data['contents']['twoColumnBrowseResultsRenderer']['tabs'][0]
            ['tabRenderer']['content']['sectionListRenderer'])
        for section_list in section_list_renderer['contents']:
            yield from section_list['itemSectionRenderer']['contents']
        try:
            next_continuation = (section_list_renderer['continuations'][0]
                                 ['nextContinuationData'])
        except KeyError:
            return

        continuation = next_continuation['continuation']
        itct = next_continuation['clickTrackingParams']

        params = {
            'ctoken': continuation,
            'continuation': continuation,
            'itct': itct
        }
        xsrf = ytcfg['XSRF_TOKEN']

        while True:
            resp = cast(
                BrowseAJAXSequence,
                self._download_page(BROWSE_AJAX_URL,
                                    return_json=True,
                                    headers=headers,
                                    data={'session_token': xsrf},
                                    method='post',
                                    params=params))
            contents = resp[1]['response']
            section_list_renderer = (
                contents['continuationContents']['sectionListContinuation'])
            for section_list in section_list_renderer['contents']:
                yield from section_list['itemSectionRenderer']['contents']

            try:
                continuations = section_list_renderer['continuations']
            except KeyError as e:
                # Probably the end of the history
                self._log.debug('Caught KeyError: %s. Possible keys: %s', e,
                                ', '.join(section_list_renderer.keys()))
                break
            xsrf = resp[1]['xsrf_token']
            next_cont = continuations[0]['nextContinuationData']
            params['itct'] = next_cont['clickTrackingParams']
            params['ctoken'] = next_cont['continuation']
            params['continuation'] = next_cont['continuation']

    def remove_video_id_from_history(self, video_id: str) -> bool:
        """Delete a history entry by video ID."""
        if not self._logged_in:
            raise AuthenticationError('This method requires a call to '
                                      'login() first')

        history_info = self.get_history_info()
        content = self._download_page_soup(HISTORY_URL)
        ytcfg = find_ytcfg(content)
        headers = ytcfg_headers(ytcfg)

        try:
            entry = list(
                filter(
                    lambda x: 'videoRenderer' in x and x['videoRenderer'][
                        'videoId'] == video_id, history_info))[0]
        except IndexError:
            return False

        form_data = {
            'sej':
            json.dumps(
                entry['videoRenderer']['menu']['menuRenderer']
                ['topLevelButtons'][0]['buttonRenderer']['serviceEndpoint']),
            'csn':
            ytcfg['EVENT_ID'],
            'session_token':
            ytcfg['XSRF_TOKEN'],
        }
        resp = cast(
            HasStringCode,
            self._download_page(SERVICE_AJAX_URL,
                                return_json=True,
                                data=form_data,
                                method='post',
                                headers=headers,
                                params={'name': 'feedbackEndpoint'}))

        return resp['code'] == 'SUCCESS'

    def _get_authorization_sapisidhash_header(self) -> str:
        now = int(datetime.now().timestamp())
        sapisid: Optional[str] = None
        for cookie in self._cj:
            if cookie.name in ('SAPISID', '__Secure-3PAPISID'):
                sapisid = cookie.value
                break
        assert sapisid is not None
        m = hashlib.sha1()
        m.update(f'{now} {sapisid} https://www.youtube.com'.encode())
        return f'SAPISIDHASH {now}_{m.hexdigest()}'

    def pause_resume_search_history(self) -> bool:
        """Pauses or resumes history depending on the current state."""
        if not self._logged_in:
            raise AuthenticationError('This method requires a call to '
                                      'login() first')
        content = self._download_page_soup(SEARCH_HISTORY_URL)
        ytcfg = find_ytcfg(content)
        yt_init_data = initial_data(content)
        info = at_path(
            ('contents.twoColumnBrowseResultsRenderer.'
             'secondaryContents.browseFeedActionsRenderer.contents.2.'
             'buttonRenderer.navigationEndpoint.confirmDialogEndpoint.content.'
             'confirmDialogRenderer.confirmEndpoint'), yt_init_data)
        metadata = info['commandMetadata']['webCommandMetadata']
        api_url = metadata['apiUrl']
        return cast(
            Mapping[str, Any],
            self._download_page(
                f'https://www.youtube.com{api_url}',
                method='post',
                params=dict(key=ytcfg['INNERTUBE_API_KEY']),
                headers={
                    'Authority': 'www.youtube.com',
                    'Authorization':
                    self._get_authorization_sapisidhash_header(),
                    'x-goog-authuser': '0',
                    'x-origin': 'https://www.youtube.com',
                },
                json={
                    'context': {
                        'clickTracking': {
                            'clickTrackingParams': info['clickTrackingParams']
                        },
                        'client': context_client_body(ytcfg),
                        'request': {
                            'consistencyTokenJars': [],
                            'internalExperimentFlags': [],
                        },
                        'user': {
                            'onBehalfOfUser': ytcfg['DELEGATED_SESSION_ID'],
                        }
                    },
                    'feedbackTokens':
                    [info['feedbackEndpoint']['feedbackToken']],
                    'isFeedbackTokenUnencrypted': False,
                    'shouldMerge': False
                },
                return_json=True))['feedbackResponses'][0]['isProcessed']
