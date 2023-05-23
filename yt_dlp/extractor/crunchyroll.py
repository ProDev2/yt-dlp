import base64
import urllib.error
import typing

from .common import InfoExtractor
from ..utils import (
    ExtractorError,
    float_or_none,
    format_field,
    int_or_none,
    join_nonempty,
    LangSelector,
    parse_age_limit,
    parse_count,
    parse_iso8601,
    qualities,
    remove_start,
    smuggle_url,
    time_seconds,
    traverse_obj,
    unsmuggle_url,
    url_or_none,
    urlencode_postdata,
)


class CrunchyrollBaseIE(InfoExtractor):
    _BASE_URL = 'https://www.crunchyroll.com'
    _API_BASE = 'https://api.crunchyroll.com'
    _NETRC_MACHINE = 'crunchyroll'
    _AUTH_HEADERS = None
    _API_ENDPOINT = None
    _BASIC_AUTH = None
    _QUERY = {}
    _DEFAULT_LANG = '@@default'

    @property
    def is_logged_in(self):
        return self._get_cookies(self._BASE_URL).get('etp_rt')

    def _perform_login(self, username, password):
        if self.is_logged_in:
            return

        upsell_response = self._download_json(
            f'{self._API_BASE}/get_upsell_data.0.json', None, 'Getting session id',
            query={
                'sess_id': 1,
                'device_id': 'whatvalueshouldbeforweb',
                'device_type': 'com.crunchyroll.static',
                'access_token': 'giKq5eY27ny3cqz',
                'referer': f'{self._BASE_URL}/welcome/login'
            })
        if upsell_response['code'] != 'ok':
            raise ExtractorError('Could not get session id')
        session_id = upsell_response['data']['session_id']

        login_response = self._download_json(
            f'{self._API_BASE}/login.1.json', None, 'Logging in',
            data=urlencode_postdata({
                'account': username,
                'password': password,
                'session_id': session_id
            }))
        if login_response['code'] != 'ok':
            raise ExtractorError('Login failed. Server message: %s' % login_response['message'], expected=True)
        if not self.is_logged_in:
            raise ExtractorError('Login succeeded but did not set etp_rt cookie')

    def _update_query(self, lang):
        if lang in CrunchyrollBaseIE._QUERY:
            return

        webpage = self._download_webpage(
            f'{self._BASE_URL}/{lang}', None, note=f'Retrieving main page (lang={lang or None})')

        initial_state = self._search_json(r'__INITIAL_STATE__\s*=', webpage, 'initial state', None)
        CrunchyrollBaseIE._QUERY[lang] = traverse_obj(initial_state, {
            'locale': ('localization', 'locale'),
        }) or None

        if CrunchyrollBaseIE._BASIC_AUTH:
            return

        app_config = self._search_json(r'__APP_CONFIG__\s*=', webpage, 'app config', None)
        cx_api_param = app_config['cxApiParams']['accountAuthClientId' if self.is_logged_in else 'anonClientId']
        self.write_debug(f'Using cxApiParam={cx_api_param}')
        CrunchyrollBaseIE._BASIC_AUTH = 'Basic ' + base64.b64encode(f'{cx_api_param}:'.encode()).decode()

    def _update_auth(self):
        if CrunchyrollBaseIE._AUTH_HEADERS and CrunchyrollBaseIE._AUTH_REFRESH > time_seconds():
            return

        assert CrunchyrollBaseIE._BASIC_AUTH, '_update_query needs to be called at least one time beforehand'
        grant_type = 'etp_rt_cookie' if self.is_logged_in else 'client_id'
        auth_response = self._download_json(
            f'{self._BASE_URL}/auth/v1/token', None, note=f'Authenticating with grant_type={grant_type}',
            headers={'Authorization': CrunchyrollBaseIE._BASIC_AUTH}, data=f'grant_type={grant_type}'.encode())

        CrunchyrollBaseIE._AUTH_HEADERS = {'Authorization': auth_response['token_type'] + ' ' + auth_response['access_token']}
        CrunchyrollBaseIE._AUTH_REFRESH = time_seconds(seconds=traverse_obj(auth_response, ('expires_in', {float_or_none}), default=300) - 10)

    def _call_base_api(self, endpoint, internal_id, lang, note=None, query={}):
        self._update_query(lang)
        self._update_auth()

        if not endpoint.startswith('/'):
            endpoint = f'/{endpoint}'

        return self._download_json(
            f'{self._BASE_URL}{endpoint}', internal_id, note or f'Calling API: {endpoint}',
            headers=CrunchyrollBaseIE._AUTH_HEADERS, query={**CrunchyrollBaseIE._QUERY[lang], **query})

    def _call_api(self, path, internal_id, lang, note='api', query={}):
        if not path.startswith(f'/content/v2/{self._API_ENDPOINT}/'):
            path = f'/content/v2/{self._API_ENDPOINT}/{path}'

        try:
            result = self._call_base_api(
                path, internal_id, lang, f'Downloading {note} JSON ({self._API_ENDPOINT})', query=query)
        except ExtractorError as error:
            if isinstance(error.cause, urllib.error.HTTPError) and error.cause.code == 404:
                return None
            raise

        if not result:
            raise ExtractorError(f'Unexpected response when downloading {note} JSON')
        return result

    def _get_requested_langs_from_extractor_args(self, ie_key=None):
        return self._configuration_arg('language', ie_key=ie_key, casesense=False)

    @staticmethod
    def _get_requested_lang_selector(requested_langs):
        # Requested Language options:
        # 'default' or '~'     = Include audio language of video with 'internal_id'
        # 'unknown' or '?'     = Include all unknown audio languages
        # 'all'     or '*'     = Include all possible languages (will override all other options)
        # '{lang-code}'        = Include specific audio language e.g. 'ja-JP' or 'en-US'
        # '${lang-code-regex}' = Include specific audio language using regex e.g. '$ja.*' or '$en.*'
        # '{any1} > {any2}'    = Priority group -> Try `any1`, if not available `any2`
        #                        e.g. '$ja.* > en-US > ~' (Try '$ja.*', then 'en-US' and then `default`)
        # '{group1}, {group2}' = Separate different `priority groups` by ','
        # > Hint: Escape a character by prefixing it with '\'

        # Format requested_langs
        default_lang_val = CrunchyrollBaseIE._DEFAULT_LANG
        expr_mapper = LangSelector.get_standard_mapper(default_val=default_lang_val)
        return LangSelector(requested_langs, mapper=expr_mapper, default_val=default_lang_val)

    @staticmethod
    def _get_audio_langs_from_data(data):
        lang_formatter = lambda v: v.casefold().strip()
        wrap_in_list = lambda v: v and [v]

        # Find audio languages in 'audio_locales' and 'audio_locale' and merge them into a case-folded set
        audio_langs = set(traverse_obj(data, ('audio_locales', ..., {lang_formatter}), default=[]))
        audio_langs.update(traverse_obj(data, ('audio_locale', {lang_formatter}, {wrap_in_list}), default=[]))
        return audio_langs

    def _get_responses_for_langs(self, internal_id, get_response_by_id, requested_langs):
        requested_lang_selector = self._get_requested_lang_selector(requested_langs)
        default_lang = CrunchyrollBaseIE._DEFAULT_LANG
        langs_fetcher = self._get_audio_langs_from_data

        def get_meta_from_response(response):
            object_type = response.get('type')
            return object_type and traverse_obj(response, f'{object_type}_metadata')

        # Get default response and its metadata
        default_response = get_response_by_id(internal_id)
        if not default_response:
            raise ExtractorError(
                f'No video with id {internal_id} could be found (possibly region locked?)',
                expected=True)
        default_meta = get_meta_from_response(default_response)

        # Build lang table and keep selected langs
        versions = (default_meta and traverse_obj(default_meta, 'versions')) or []
        lang_table = [(version.get('guid'), langs_fetcher(version)) for version in versions]
        lang_table = [(vid, lang) for vid, langs in lang_table if vid for lang in (langs or [None])]
        lang_table.append((internal_id, default_lang))
        requested_ids = requested_lang_selector.keep_table_matches(lang_table)

        # Result dict to store requested responses in
        results = {}
        # Iterate requested ids and fetch their responses
        for vid in requested_ids:
            if vid == internal_id:
                # Use default response
                requested_response = default_response
            else:
                # Fetch response for current version (will be on a different video page)
                requested_response = get_response_by_id(vid)
                if not requested_response:
                    self.to_screen(
                        f'Requested video version with id {vid} could not be found (possibly region locked?)',
                        only_once=True)
            results[vid] = requested_response
        return results

    def _extract_formats(self, stream_response, display_id=None):
        requested_formats = self._configuration_arg('format') or ['adaptive_hls']
        available_formats = {}
        for stream_type, streams in traverse_obj(
                stream_response, (('streams', ('data', 0)), {dict.items}, ...)):
            if stream_type not in requested_formats:
                continue
            for stream in traverse_obj(streams, lambda _, v: v['url']):
                hardsub_lang = stream.get('hardsub_locale') or ''
                format_id = join_nonempty(stream_type, format_field(stream, 'hardsub_locale', 'hardsub-%s'))
                available_formats[hardsub_lang] = (stream_type, format_id, hardsub_lang, stream['url'])

        requested_hardsubs = [('' if val == 'none' else val) for val in (self._configuration_arg('hardsub') or ['none'])]
        if '' in available_formats and 'all' not in requested_hardsubs:
            full_format_langs = set(requested_hardsubs)
            self.to_screen(
                'To get all formats of a hardsub language, use '
                '"--extractor-args crunchyrollbeta:hardsub=<language_code or all>". '
                'See https://github.com/yt-dlp/yt-dlp#crunchyrollbeta-crunchyroll for more info',
                only_once=True)
        else:
            full_format_langs = set(map(str.lower, available_formats))

        audio_locale = traverse_obj(stream_response, ((None, 'meta'), 'audio_locale'), get_all=False)
        hardsub_preference = qualities(requested_hardsubs[::-1])
        formats = []
        for stream_type, format_id, hardsub_lang, stream_url in available_formats.values():
            if stream_type.endswith('hls'):
                if hardsub_lang.lower() in full_format_langs:
                    adaptive_formats = self._extract_m3u8_formats(
                        stream_url, display_id, 'mp4', m3u8_id=format_id,
                        fatal=False, note=f'Downloading {format_id} HLS manifest')
                else:
                    adaptive_formats = (self._m3u8_meta_format(stream_url, ext='mp4', m3u8_id=format_id),)
            elif stream_type.endswith('dash'):
                adaptive_formats = self._extract_mpd_formats(
                    stream_url, display_id, mpd_id=format_id,
                    fatal=False, note=f'Downloading {format_id} MPD manifest')
            else:
                self.report_warning(f'Encountered unknown stream_type: {stream_type!r}', display_id, only_once=True)
                continue
            for f in adaptive_formats:
                if f.get('acodec') != 'none':
                    f['language'] = audio_locale
                f['quality'] = hardsub_preference(hardsub_lang.lower())
            formats.extend(adaptive_formats)

        return formats

    def _extract_subtitles(self, data):
        subtitles = {}

        for locale, subtitle in traverse_obj(data, ((None, 'meta'), 'subtitles', {dict.items}, ...)):
            subtitles[locale] = [traverse_obj(subtitle, {'url': 'url', 'ext': 'format'})]

        return subtitles

    @staticmethod
    def _extract_versions_and_merge_results(lang, internal_id, responses, extract_version):
        # If only one language was requested, extract and return its version (no merge required)
        if len(responses) == 1:
            target_id, target_response = next(iter(responses.items()))
            return extract_version(lang, target_id, target_response)

        # If multiple languages were requested, extract all versions and merge them
        # NOTE: Returned arguments such as 'title', 'season', 'season_id', etc. may differ
        # from version to version. In each format, include what is different (compared to the
        # main result). Choosing a format with '-f' now applies the correct arguments.
        # ALSO: Differences in non-hashable arguments are not included in the formats.

        # Extract main response from 'responses'. Favour the one with 'internal_id'
        version_id, version_response = (internal_id, responses.pop(internal_id, None))
        if not version_response:
            # If 'internal_id' was excluded then use some other item. Its
            # arguments are overridden when a format is selected anyway.
            version_id, version_response = responses.popitem()

        # Function to check whether an attribute (key value pair) is hashable
        def is_attribute_hashable(attribute):
            key, value = attribute
            return isinstance(key, typing.Hashable) \
                and isinstance(value, typing.Hashable)

        # Extract main result (used to merge other results)
        result = extract_version(lang, version_id, version_response)
        result_formats = result.setdefault('formats', [])
        result_subtitles = result.setdefault('subtitles', {})
        result_as_set = set(filter(is_attribute_hashable, result.items()))

        # Merge all formats and subtitles into main result
        for version_id, version_response in responses.items():
            version = extract_version(lang, version_id, version_response)
            version_formats = version.get('formats') or []
            version_subtitles = version.get('subtitles') or {}
            version_as_set = set(filter(is_attribute_hashable, version.items()))

            # Only add differences to every format
            version_differences = dict(version_as_set - result_as_set)
            for version_format in version_formats:
                version_format.update(version_differences)

            # Add version formats and subtitles to result
            result_formats.extend(version_formats)
            result_subtitles.update(version_subtitles)

        # Return merged results
        return result


class CrunchyrollCmsBaseIE(CrunchyrollBaseIE):
    _API_ENDPOINT = 'cms'
    _CMS_EXPIRY = None

    def _call_cms_api_signed(self, path, internal_id, lang, note='api'):
        if not CrunchyrollCmsBaseIE._CMS_EXPIRY or CrunchyrollCmsBaseIE._CMS_EXPIRY <= time_seconds():
            response = self._call_base_api('index/v2', None, lang, 'Retrieving signed policy')['cms_web']
            CrunchyrollCmsBaseIE._CMS_QUERY = {
                'Policy': response['policy'],
                'Signature': response['signature'],
                'Key-Pair-Id': response['key_pair_id'],
            }
            CrunchyrollCmsBaseIE._CMS_BUCKET = response['bucket']
            CrunchyrollCmsBaseIE._CMS_EXPIRY = parse_iso8601(response['expires']) - 10

        if not path.startswith('/cms/v2'):
            path = f'/cms/v2{CrunchyrollCmsBaseIE._CMS_BUCKET}/{path}'

        return self._call_base_api(
            path, internal_id, lang, f'Downloading {note} JSON (signed cms)', query=CrunchyrollCmsBaseIE._CMS_QUERY)


class CrunchyrollBetaIE(CrunchyrollCmsBaseIE):
    IE_NAME = 'crunchyroll'
    _VALID_URL = r'''(?x)
        https?://(?:beta\.|www\.)?crunchyroll\.com/
        (?P<lang>(?:\w{2}(?:-\w{2})?/)?)
        watch/(?!concert|musicvideo)(?P<id>\w+)'''
    _TESTS = [{
        # Premium only
        'url': 'https://www.crunchyroll.com/watch/GY2P1Q98Y/to-the-future',
        'info_dict': {
            'id': 'GY2P1Q98Y',
            'ext': 'mp4',
            'duration': 1380.241,
            'timestamp': 1459632600,
            'description': 'md5:a022fbec4fbb023d43631032c91ed64b',
            'title': 'World Trigger Episode 73 – To the Future',
            'upload_date': '20160402',
            'series': 'World Trigger',
            'series_id': 'GR757DMKY',
            'season': 'World Trigger',
            'season_id': 'GR9P39NJ6',
            'season_number': 1,
            'episode': 'To the Future',
            'episode_number': 73,
            'thumbnail': r're:^https://www.crunchyroll.com/imgsrv/.*\.jpeg?$',
            'chapters': 'count:2',
            'age_limit': 14,
            'like_count': int,
            'dislike_count': int,
        },
        'params': {'skip_download': 'm3u8', 'format': 'all[format_id~=hardsub]'},
    }, {
        # Premium only
        'url': 'https://www.crunchyroll.com/watch/GYE5WKQGR',
        'info_dict': {
            'id': 'GYE5WKQGR',
            'ext': 'mp4',
            'duration': 366.459,
            'timestamp': 1476788400,
            'description': 'md5:74b67283ffddd75f6e224ca7dc031e76',
            'title': 'SHELTER – Porter Robinson presents Shelter the Animation',
            'upload_date': '20161018',
            'series': 'SHELTER',
            'series_id': 'GYGG09WWY',
            'season': 'SHELTER',
            'season_id': 'GR09MGK4R',
            'season_number': 1,
            'episode': 'Porter Robinson presents Shelter the Animation',
            'episode_number': 0,
            'thumbnail': r're:^https://www.crunchyroll.com/imgsrv/.*\.jpeg?$',
            'age_limit': 14,
            'like_count': int,
            'dislike_count': int,
        },
        'params': {'skip_download': True},
    }, {
        'url': 'https://www.crunchyroll.com/watch/GJWU2VKK3/cherry-blossom-meeting-and-a-coming-blizzard',
        'info_dict': {
            'id': 'GJWU2VKK3',
            'ext': 'mp4',
            'duration': 1420.054,
            'description': 'md5:2d1c67c0ec6ae514d9c30b0b99a625cd',
            'title': 'The Ice Guy and His Cool Female Colleague Episode 1 – Cherry Blossom Meeting and a Coming Blizzard',
            'series': 'The Ice Guy and His Cool Female Colleague',
            'series_id': 'GW4HM75NP',
            'season': 'The Ice Guy and His Cool Female Colleague',
            'season_id': 'GY9PC21VE',
            'season_number': 1,
            'episode': 'Cherry Blossom Meeting and a Coming Blizzard',
            'episode_number': 1,
            'chapters': 'count:2',
            'thumbnail': r're:^https://www.crunchyroll.com/imgsrv/.*\.jpeg?$',
            'timestamp': 1672839000,
            'upload_date': '20230104',
            'age_limit': 14,
            'like_count': int,
            'dislike_count': int,
        },
        'params': {'skip_download': 'm3u8'},
    }, {
        'url': 'https://www.crunchyroll.com/watch/GM8F313NQ',
        'info_dict': {
            'id': 'GM8F313NQ',
            'ext': 'mp4',
            'title': 'Garakowa -Restore the World-',
            'description': 'md5:8d2f8b6b9dd77d87810882e7d2ee5608',
            'duration': 3996.104,
            'age_limit': 13,
            'thumbnail': r're:^https://www.crunchyroll.com/imgsrv/.*\.jpeg?$',
        },
        'params': {'skip_download': 'm3u8'},
    }, {
        'url': 'https://www.crunchyroll.com/watch/G62PEZ2E6',
        'info_dict': {
            'id': 'G62PEZ2E6',
            'description': 'md5:8d2f8b6b9dd77d87810882e7d2ee5608',
            'age_limit': 13,
            'duration': 65.138,
            'title': 'Garakowa -Restore the World-',
        },
        'playlist_mincount': 5,
    }, {
        'url': 'https://www.crunchyroll.com/watch/GY2P1Q98Y',
        'only_matching': True,
    }, {
        'url': 'https://beta.crunchyroll.com/pt-br/watch/G8WUN8VKP/the-ruler-of-conspiracy',
        'only_matching': True,
    }]
    # We want to support lazy playlist filtering and movie listings cannot be inside a playlist
    _RETURN_TYPE = 'video'

    def _real_extract(self, url):
        url, smuggled_data = unsmuggle_url(url, default={})
        lang, internal_id = self._match_valid_url(url).group('lang', 'id')

        def get_response_by_id(requested_id):
            # We need to use unsigned API call to allow ratings query string
            return traverse_obj(self._call_api(
                f'objects/{requested_id}', requested_id, lang, 'object info', {'ratings': 'true'}), ('data', 0, {dict}))

        # Fetch and 'sectionize' target languages (put them into sections, so they do not get split)
        target_langs = traverse_obj(smuggled_data, ('target_audio_langs', {list}))
        target_langs = target_langs and list(map(lambda l: [l], target_langs))

        # Fetch requested language responses
        requested_langs = target_langs or self._get_requested_langs_from_extractor_args()
        responses = self._get_responses_for_langs(internal_id, get_response_by_id, requested_langs)
        if not responses:
            raise ExtractorError(
                'None of the requested audio languages were found',
                expected=True)
        return self._extract_versions_and_merge_results(
            lang, internal_id, responses, self._extract_version)

    def _extract_version(self, lang, internal_id, response):
        object_type = response.get('type')
        if object_type == 'episode':
            result = self._transform_episode_response(response)

        elif object_type == 'movie':
            result = self._transform_movie_response(response)

        elif object_type == 'movie_listing':
            first_movie_id = traverse_obj(response, ('movie_listing_metadata', 'first_movie_id'))
            if not self._yes_playlist(internal_id, first_movie_id):
                return self.url_result(f'{self._BASE_URL}/{lang}watch/{first_movie_id}', CrunchyrollBetaIE, first_movie_id)

            def entries():
                movies = self._call_api(f'movie_listings/{internal_id}/movies', internal_id, lang, 'movie list')
                for movie_response in traverse_obj(movies, ('data', ...)):
                    yield self.url_result(
                        f'{self._BASE_URL}/{lang}watch/{movie_response["id"]}',
                        CrunchyrollBetaIE, **self._transform_movie_response(movie_response))

            return self.playlist_result(entries(), **self._transform_movie_response(response))

        else:
            raise ExtractorError(f'Unknown object type {object_type}')

        # There might be multiple audio languages for one object (`<object>_metadata.versions`),
        # so we need to get the id from `streams_link` instead or we dont know which language to choose
        streams_link = response.get('streams_link')
        if not streams_link and traverse_obj(response, (f'{object_type}_metadata', 'is_premium_only')):
            message = f'This {object_type} is for premium members only'
            if self.is_logged_in:
                raise ExtractorError(message, expected=True)
            self.raise_login_required(message)

        # We need go from unsigned to signed api to avoid getting soft banned
        stream_response = self._call_cms_api_signed(remove_start(
            streams_link, '/content/v2/cms/'), internal_id, lang, 'stream info')
        result['formats'] = self._extract_formats(stream_response, internal_id)
        result['subtitles'] = self._extract_subtitles(stream_response)

        # if no intro chapter is available, a 403 without usable data is returned
        intro_chapter = self._download_json(
            f'https://static.crunchyroll.com/datalab-intro-v2/{internal_id}.json',
            internal_id, note='Downloading chapter info', fatal=False, errnote=False)
        if isinstance(intro_chapter, dict):
            result['chapters'] = [{
                'title': 'Intro',
                'start_time': float_or_none(intro_chapter.get('startTime')),
                'end_time': float_or_none(intro_chapter.get('endTime')),
            }]

        def calculate_count(item):
            return parse_count(''.join((item['displayed'], item.get('unit') or '')))

        result.update(traverse_obj(response, ('rating', {
            'like_count': ('up', {calculate_count}),
            'dislike_count': ('down', {calculate_count}),
        })))

        return result

    @staticmethod
    def _transform_episode_response(data):
        metadata = traverse_obj(data, (('episode_metadata', None), {dict}), get_all=False) or {}
        return {
            'id': data['id'],
            'title': ' \u2013 '.join((
                ('%s%s' % (
                    format_field(metadata, 'season_title'),
                    format_field(metadata, 'episode', ' Episode %s'))),
                format_field(data, 'title'))),
            **traverse_obj(data, {
                'episode': ('title', {str}),
                'description': ('description', {str}, {lambda x: x.replace(r'\r\n', '\n')}),
                'thumbnails': ('images', 'thumbnail', ..., ..., {
                    'url': ('source', {url_or_none}),
                    'width': ('width', {int_or_none}),
                    'height': ('height', {int_or_none}),
                }),
            }),
            **traverse_obj(metadata, {
                'duration': ('duration_ms', {lambda x: float_or_none(x, 1000)}),
                'timestamp': ('upload_date', {parse_iso8601}),
                'series': ('series_title', {str}),
                'series_id': ('series_id', {str}),
                'season': ('season_title', {str}),
                'season_id': ('season_id', {str}),
                'season_number': ('season_number', ({int}, {float_or_none})),
                'episode_number': ('sequence_number', ({int}, {float_or_none})),
                'age_limit': ('maturity_ratings', -1, {parse_age_limit}),
                'language': ('audio_locale', {str}),
            }, get_all=False),
        }

    @staticmethod
    def _transform_movie_response(data):
        metadata = traverse_obj(data, (('movie_metadata', 'movie_listing_metadata', None), {dict}), get_all=False) or {}
        return {
            'id': data['id'],
            **traverse_obj(data, {
                'title': ('title', {str}),
                'description': ('description', {str}, {lambda x: x.replace(r'\r\n', '\n')}),
                'thumbnails': ('images', 'thumbnail', ..., ..., {
                    'url': ('source', {url_or_none}),
                    'width': ('width', {int_or_none}),
                    'height': ('height', {int_or_none}),
                }),
            }),
            **traverse_obj(metadata, {
                'duration': ('duration_ms', {lambda x: float_or_none(x, 1000)}),
                'age_limit': ('maturity_ratings', -1, {parse_age_limit}),
            }),
        }


class CrunchyrollBetaShowIE(CrunchyrollCmsBaseIE):
    IE_NAME = 'crunchyroll:playlist'
    _VALID_URL = r'''(?x)
        https?://(?:beta\.|www\.)?crunchyroll\.com/
        (?P<lang>(?:\w{2}(?:-\w{2})?/)?)
        series/(?P<id>\w+)'''
    _TESTS = [{
        'url': 'https://www.crunchyroll.com/series/GY19NQ2QR/Girl-Friend-BETA',
        'info_dict': {
            'id': 'GY19NQ2QR',
            'title': 'Girl Friend BETA',
            'description': 'md5:99c1b22ee30a74b536a8277ced8eb750',
            # XXX: `thumbnail` does not get set from `thumbnails` in playlist
            #  'thumbnail': r're:^https://www.crunchyroll.com/imgsrv/.*\.jpeg?$',
            'age_limit': 14,
        },
        'playlist_mincount': 10,
    }, {
        'url': 'https://beta.crunchyroll.com/it/series/GY19NQ2QR',
        'only_matching': True,
    }]

    def _real_extract(self, url):
        lang, internal_id = self._match_valid_url(url).group('lang', 'id')

        # Fetch requested languages (for Crunchyroll 'single episode' extractor)
        requested_langs = self._get_requested_langs_from_extractor_args(ie_key=CrunchyrollBetaIE.ie_key())
        requested_lang_selector = self._get_requested_lang_selector(requested_langs)
        default_lang = CrunchyrollBaseIE._DEFAULT_LANG

        def episode_responses():
            seasons_response = self._call_cms_api_signed(f'seasons?series_id={internal_id}', internal_id, lang, 'seasons')
            for season in traverse_obj(seasons_response, ('items', ..., {dict})):
                episodes_response = self._call_cms_api_signed(
                    f'episodes?season_id={season["id"]}', season["id"], lang, 'episode list')
                for episode_response in traverse_obj(episodes_response, ('items', ..., {dict})):
                    yield episode_response["id"], episode_response

        # Fetch all episode responses for playlist and extract all audio langs
        episode_table = [(eid, self._get_audio_langs_from_data(resp) or [], resp)
                         for eid, resp in episode_responses() if eid]
        # Include None in 'episode_langs' if the language of any episode is unknown
        episode_langs = {lang for _, langs, _ in episode_table
                         for lang in (langs or [None])}
        # Default behavior (extract everything) is always available, therefore add it
        episode_langs.add(default_lang)

        # Get all requested langs (formatted) that are available
        matching_langs = requested_lang_selector.get_list_matches(episode_langs)
        matching_default = requested_lang_selector.has_match(default_lang, matching_langs)

        def entries():
            for ep_id, ep_langs, ep_response in episode_table:
                # If 'default' is part of 'matching_langs' use default behavior (which means extract any episode)
                # NOTE: If 'ep_langs' is empty 'has_matches' will search for unknown/None lang in 'matching_langs'
                matching_ep_langs = requested_lang_selector.has_matches(ep_langs, matching_langs) \
                    if not matching_default else ['~']
                if matching_ep_langs:
                    smuggled_data = {
                        'target_audio_langs': matching_ep_langs,
                    }

                    yield self.url_result(
                        smuggle_url(f'{self._BASE_URL}/{lang}watch/{ep_id}', smuggled_data),
                        CrunchyrollBetaIE, **CrunchyrollBetaIE._transform_episode_response(ep_response))

        return self.playlist_result(
            entries(), internal_id,
            **traverse_obj(self._call_api(f'series/{internal_id}', internal_id, lang, 'series'), ('data', 0, {
                'title': ('title', {str}),
                'description': ('description', {lambda x: x.replace(r'\r\n', '\n')}),
                'age_limit': ('maturity_ratings', -1, {parse_age_limit}),
                'thumbnails': ('images', ..., ..., ..., {
                    'url': ('source', {url_or_none}),
                    'width': ('width', {int_or_none}),
                    'height': ('height', {int_or_none}),
                })
            })))


class CrunchyrollMusicIE(CrunchyrollBaseIE):
    IE_NAME = 'crunchyroll:music'
    _VALID_URL = r'''(?x)
        https?://(?:www\.)?crunchyroll\.com/
        (?P<lang>(?:\w{2}(?:-\w{2})?/)?)
        watch/(?P<type>concert|musicvideo)/(?P<id>\w{10})'''
    _TESTS = [{
        'url': 'https://www.crunchyroll.com/watch/musicvideo/MV88BB7F2C',
        'info_dict': {
            'ext': 'mp4',
            'id': 'MV88BB7F2C',
            'display_id': 'crossing-field',
            'title': 'Crossing Field',
            'track': 'Crossing Field',
            'artist': 'LiSA',
            'thumbnail': r're:(?i)^https://www.crunchyroll.com/imgsrv/.*\.jpeg?$',
            'genre': ['Anime'],
        },
        'params': {'skip_download': 'm3u8'},
    }, {
        'url': 'https://www.crunchyroll.com/watch/concert/MC2E2AC135',
        'info_dict': {
            'ext': 'mp4',
            'id': 'MC2E2AC135',
            'display_id': 'live-is-smile-always-364joker-at-yokohama-arena',
            'title': 'LiVE is Smile Always-364+JOKER- at YOKOHAMA ARENA',
            'track': 'LiVE is Smile Always-364+JOKER- at YOKOHAMA ARENA',
            'artist': 'LiSA',
            'thumbnail': r're:(?i)^https://www.crunchyroll.com/imgsrv/.*\.jpeg?$',
            'description': 'md5:747444e7e6300907b7a43f0a0503072e',
            'genre': ['J-Pop'],
        },
        'params': {'skip_download': 'm3u8'},
    }, {
        'url': 'https://www.crunchyroll.com/watch/musicvideo/MV88BB7F2C/crossing-field',
        'only_matching': True,
    }, {
        'url': 'https://www.crunchyroll.com/watch/concert/MC2E2AC135/live-is-smile-always-364joker-at-yokohama-arena',
        'only_matching': True,
    }]
    _API_ENDPOINT = 'music'

    def _real_extract(self, url):
        lang, internal_id, object_type = self._match_valid_url(url).group('lang', 'id', 'type')
        path, name = {
            'concert': ('concerts', 'concert info'),
            'musicvideo': ('music_videos', 'music video info'),
        }[object_type]
        response = traverse_obj(self._call_api(f'{path}/{internal_id}', internal_id, lang, name), ('data', 0, {dict}))
        if not response:
            raise ExtractorError(f'No video with id {internal_id} could be found (possibly region locked?)', expected=True)

        streams_link = response.get('streams_link')
        if not streams_link and response.get('isPremiumOnly'):
            message = f'This {response.get("type") or "media"} is for premium members only'
            if self.is_logged_in:
                raise ExtractorError(message, expected=True)
            self.raise_login_required(message)

        result = self._transform_music_response(response)
        stream_response = self._call_api(streams_link, internal_id, lang, 'stream info')
        result['formats'] = self._extract_formats(stream_response, internal_id)

        return result

    @staticmethod
    def _transform_music_response(data):
        return {
            'id': data['id'],
            **traverse_obj(data, {
                'display_id': 'slug',
                'title': 'title',
                'track': 'title',
                'artist': ('artist', 'name'),
                'description': ('description', {str}, {lambda x: x.replace(r'\r\n', '\n') or None}),
                'thumbnails': ('images', ..., ..., {
                    'url': ('source', {url_or_none}),
                    'width': ('width', {int_or_none}),
                    'height': ('height', {int_or_none}),
                }),
                'genre': ('genres', ..., 'displayValue'),
                'age_limit': ('maturity_ratings', -1, {parse_age_limit}),
            }),
        }


class CrunchyrollArtistIE(CrunchyrollBaseIE):
    IE_NAME = 'crunchyroll:artist'
    _VALID_URL = r'''(?x)
        https?://(?:www\.)?crunchyroll\.com/
        (?P<lang>(?:\w{2}(?:-\w{2})?/)?)
        artist/(?P<id>\w{10})'''
    _TESTS = [{
        'url': 'https://www.crunchyroll.com/artist/MA179CB50D',
        'info_dict': {
            'id': 'MA179CB50D',
            'title': 'LiSA',
            'genre': ['J-Pop', 'Anime', 'Rock'],
            'description': 'md5:16d87de61a55c3f7d6c454b73285938e',
        },
        'playlist_mincount': 83,
    }, {
        'url': 'https://www.crunchyroll.com/artist/MA179CB50D/lisa',
        'only_matching': True,
    }]
    _API_ENDPOINT = 'music'

    def _real_extract(self, url):
        lang, internal_id = self._match_valid_url(url).group('lang', 'id')
        response = traverse_obj(self._call_api(
            f'artists/{internal_id}', internal_id, lang, 'artist info'), ('data', 0))

        def entries():
            for attribute, path in [('concerts', 'concert'), ('videos', 'musicvideo')]:
                for internal_id in traverse_obj(response, (attribute, ...)):
                    yield self.url_result(f'{self._BASE_URL}/watch/{path}/{internal_id}', CrunchyrollMusicIE, internal_id)

        return self.playlist_result(entries(), **self._transform_artist_response(response))

    @staticmethod
    def _transform_artist_response(data):
        return {
            'id': data['id'],
            **traverse_obj(data, {
                'title': 'name',
                'description': ('description', {str}, {lambda x: x.replace(r'\r\n', '\n')}),
                'thumbnails': ('images', ..., ..., {
                    'url': ('source', {url_or_none}),
                    'width': ('width', {int_or_none}),
                    'height': ('height', {int_or_none}),
                }),
                'genre': ('genres', ..., 'displayValue'),
            }),
        }
