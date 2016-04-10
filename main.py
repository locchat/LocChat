import os
import uuid
import datetime
import urllib
import json
import re
import logging
import base64
from types import *
from urlparse import urlparse

import geo.geocell
import geo.geotypes
import geo.geotypes

from google.appengine.api import urlfetch
from google.appengine.api import users
from google.appengine.api import channel
from google.appengine.api import memcache
from google.appengine.api import taskqueue
from google.appengine.ext import ndb
from google.appengine.ext.webapp import blobstore_handlers
from google.appengine.ext import blobstore
from google.appengine.api import images

import jinja2
import webapp2
from webapp2_extras import sessions

LAT_LNG_REG = re.compile('^(\-?\d*\.\d*),(\-?\d*\.\d*)$')
BOUNDS_LAT_LNG_REG = re.compile('^(.*),(.*),(.*),(.*)$')
MID_REG = re.compile('^(.{10})(.{13})(\d*):(.*)$')

CHANNEL_DURATION_MINUTES = 10 * 60
PID_LENGTH = 4
JINJA_ENVIRONMENT = jinja2.Environment(
    loader=jinja2.FileSystemLoader(os.path.dirname(__file__)),
    extensions=['jinja2.ext.autoescape'],
    autoescape=True)

UNIQUE_ID_LENGTH = 32
UID_LENGTH = 32
EPOCH_STRING_LENGTH = 10
GEOCELL_RESOLUTION = 10
MESSAGE_FORMAT_VERSION = '0'
MESSAGE_LIST_MAX_COUNT = 100
PERMANENT_GCM_ERRORS = {'InvalidRegistration', 'NotRegistered',
                        'InvalidPackageName', 'MismatchSenderId'}

def IsDevAppserver():
  return os.environ['SERVER_SOFTWARE'].startswith('Development/')

def GetSecretKeys():
  f = open('secret_keys.json', 'r')
  keys = json.loads(f.read())
  f.close()
  return keys

SECRET_KEYS = GetSecretKeys()
WEBAPP2_CONFIG = {
    'webapp2_extras.sessions': {
        'secret_key': SECRET_KEYS['webapp2_secret_key'].encode(),
        'session_max_age': 60 * 60 * 24 * 365,
        'cookie_args':{
            'max_age': 60 * 60 * 24 * 365,
            'domain': None,
            'path': '/',
            'secure': not IsDevAppserver(),
            'httponly': True,
        }
    }}

GCM_KEY = SECRET_KEYS['gcm_key'].encode()
GCM_ENDPOINT = 'https://android.googleapis.com/gcm/send'
GCM_ENDPOINT_LENGTH = len(GCM_ENDPOINT)

def get_gcm_subscription_id(endpoint):
    if not endpoint.startswith(GCM_ENDPOINT):
        return None
    return endpoint[GCM_ENDPOINT_LENGTH + 1:]

def get_epoch_string(date):
    return str(int((date - datetime.datetime(1970, 1, 1)).total_seconds()))

def is_epoch_string(epoch):
    return isinstance(epoch, basestring) and len(epoch) == EPOCH_STRING_LENGTH

def get_datetime_from_epoch(epoch):
    return datetime.datetime(1970, 1, 1) + datetime.timedelta(0, int(epoch))

def get_box_from_bounds_str(bounds_str):
    bounds_m = BOUNDS_LAT_LNG_REG.match(bounds_str)
    if not bounds_m:
        return None
    try:
        sw_lat = float(bounds_m.group(1))
        sw_lng = float(bounds_m.group(2))
        ne_lat = float(bounds_m.group(3))
        ne_lng = float(bounds_m.group(4))
        return geo.geotypes.Box(ne_lat, ne_lng, sw_lat, sw_lng)
    except ValueError:
        return None

def get_center_point(box):
    lat = (box.south_west.lat + box.north_east.lat) / 2.0
    if box.north_east.lon < box.south_west.lon:
        lon = (box.north_east.lon + box.south_west.lon + 360.0) / 2.0
    else:
        lon = (box.north_east.lon + box.south_west.lon) / 2.0
    if lon > 180.0:
        lon -= 360.0
    return geo.geotypes.Point(lat, lon)

def referer_check(request):
    refurl = request.headers.get("Referer")
    if not refurl:
        return False
    request_domain = '{uri.scheme}://{uri.netloc}/'.format(uri=urlparse(request.url))
    refurl_domain = '{uri.scheme}://{uri.netloc}/'.format(uri=urlparse(refurl))
    return request_domain == refurl_domain


class BaseHandler(webapp2.RequestHandler):
    def dispatch(self):
        self.session_store = sessions.get_store(request=self.request)
        try:
            webapp2.RequestHandler.dispatch(self)
        finally:
            self.session_store.save_sessions(self.response)

    @webapp2.cached_property
    def session(self):
        return self.session_store.get_session()

class BaseBlobHandler(blobstore_handlers.BlobstoreUploadHandler):
    def dispatch(self):
        self.session_store = sessions.get_store(request=self.request)
        try:
            webapp2.RequestHandler.dispatch(self)
        finally:
            self.session_store.save_sessions(self.response)

    @webapp2.cached_property
    def session(self):
        return self.session_store.get_session()

def is_pid(pid):
    return isinstance(pid, basestring) and len(pid) == PID_LENGTH

class UserInfo(ndb.Model):
    # Creation time
    creation_time = ndb.DateTimeProperty('c', auto_now_add=True)
    # Updated time
    updated_time = ndb.DateTimeProperty('u', auto_now=True)
    # Page id
    pid = ndb.StringProperty('p', indexed=False)
    # channel Id
    channel_id = ndb.StringProperty('i', indexed=False)
    # channel Token
    channel_token = ndb.StringProperty('t', indexed=False)
    # chnnel token Available time
    channel_token_available_time = ndb.DateTimeProperty('a', indexed=False)
    # notification Endpoint
    notification_endpoint = ndb.StringProperty('e', indexed=False)
    # Focused
    focused = ndb.BooleanProperty('f', indexed=False)
    # user Name
    user_name = ndb.StringProperty('n', indexed=False)

    @classmethod
    def _get_kind(cls):
        return 'U'

class UserPos(ndb.Model):
    north = ndb.FloatProperty('n', indexed=False)
    east = ndb.FloatProperty('e', indexed=False)
    south = ndb.FloatProperty('s', indexed=False)
    west = ndb.FloatProperty('w', indexed=False)
    cells = ndb.StringProperty('c', repeated=True, indexed=True)
    @classmethod
    def _get_kind(cls):
        return 'P'

class ImageInfo(ndb.Model):
    blob_key = ndb.BlobKeyProperty('b', indexed=False)
    url = ndb.StringProperty('u', indexed=False)
    width = ndb.IntegerProperty('w', indexed=False)
    height = ndb.IntegerProperty('h', indexed=False)
    @classmethod
    def _get_kind(cls):
        return 'I'

class ChatMessagePos(ndb.Model):
    cells = ndb.StringProperty('c', repeated=True, indexed=True)
    @classmethod
    def _get_kind(cls):
        return 'C'

class MessageData(object):
    def __init__(self, mid):
        self._sep_pos = mid[EPOCH_STRING_LENGTH + GEOCELL_RESOLUTION:].find(':')
        if self._sep_pos <= 0:
            return
        self._iid_pos = mid[EPOCH_STRING_LENGTH + GEOCELL_RESOLUTION:EPOCH_STRING_LENGTH + GEOCELL_RESOLUTION + self._sep_pos].find(',')
        self.mid = mid
        self._epoch = None
        self._uid = None
        self._iid = None
        self._point = None
        self._date = None
        self.user_info = None
        self.image_info = None

    @property
    def epoch(self):
        if not self._epoch is None:
            return self._epoch
        self._epoch = float(self.mid[:EPOCH_STRING_LENGTH])
        return self._epoch

    @property
    def cell(self):
        return self.mid[EPOCH_STRING_LENGTH:EPOCH_STRING_LENGTH+GEOCELL_RESOLUTION]

    @property
    def uid(self):
        if not self._uid is None:
            return self._uid
        if self._iid_pos <= 0:
            self._uid = long(self.mid[EPOCH_STRING_LENGTH+GEOCELL_RESOLUTION:EPOCH_STRING_LENGTH+GEOCELL_RESOLUTION+self._sep_pos])
        else:
            self._uid = long(self.mid[EPOCH_STRING_LENGTH+GEOCELL_RESOLUTION:EPOCH_STRING_LENGTH+GEOCELL_RESOLUTION+self._iid_pos])
        return self._uid

    @property
    def iid(self):
        if not self._iid is None:
            return self._iid
        if self._iid_pos <= 0:
            return None
        else:
            self._iid = long(self.mid[EPOCH_STRING_LENGTH+GEOCELL_RESOLUTION+self._iid_pos+1:EPOCH_STRING_LENGTH+GEOCELL_RESOLUTION+self._sep_pos])
        return self._iid

    @property
    def content(self):
        return self.mid[EPOCH_STRING_LENGTH+GEOCELL_RESOLUTION+self._sep_pos+1:]

    @property
    def point(self):
        if not self._point is None:
            return self._point
        self._point = get_center_point(geo.geocell.compute_box(self.cell))
        return self._point

    @property
    def date(self):
        if not self._date is None:
            return self._date
        self._date = get_datetime_from_epoch(self.epoch)
        return self._date

    def is_inside(self, box):
        point = self.point
        if point.lat < box.south_west.lat or box.north_east.lat < point.lat:
            return False
        if (box.north_east.lon < box.south_west.lon):
            if box.north_east.lon < point.lon and point.lon < box.south_west.lon:
                return False
        else:
            if point.lon < box.south_west.lon or box.north_east.lon < point.lon:
                return False
        return True

    def to_chat_dict(self):
        user_name = 'Anonymous'
        if self.user_info is None:
            key = ndb.Key(UserInfo, self.uid)
            self.user_info = key.get()
        if self.user_info and self.user_info.user_name:
            user_name = self.user_info.user_name
        result = {
            'epoch': self.epoch,
            'user_name': user_name,
            'content': self.content,
            'lat': self.point.lat,
            'lon': self.point.lon,
            'date': self.date.isoformat(),
        }

        if self.iid is None:
            return result
        if self.image_info is None:
            key = ndb.Key(ImageInfo, self.iid)
            self.image_info = key.get()
        if self.image_info:
            result['image'] = [{
                'url': self.image_info.url,
                'width': self.image_info.width,
                'height': self.image_info.height
            }]
        return result

class MainPage(BaseHandler):
    def get(self):
        if not IsDevAppserver():
            if self.request.url == 'https://www.locchat.com/' or self.request.url[0:8] == 'http://www.locchat.com/':
                self.redirect('https://locchat.com/')
                return
            if self.request.url[0:8] != 'https://':
                self.redirect('https://' + self.request.url[7:])
                return
        logging.info('MainPage')
        lat = self.session.get('lat')
        lon = self.session.get('lon')
        zoom = self.session.get('zoom')
        if not type(zoom) is FloatType:
            zoom = 5.0
            self.session['zoom'] = zoom
        if not (type(lat) is FloatType and type(lon) is FloatType):
            lat = 35.658068
            lon = 139.751599
            ll_str = self.request.headers.get('X-AppEngine-CityLatLong')
            if ll_str:
                ll_m = LAT_LNG_REG.match(ll_str)
                if ll_m:
                    lat = float(ll_m.group(1))
                    lon = float(ll_m.group(2))
            self.session['lat'] = lat
            self.session['lon'] = lon

        template_values = {
            'lat': lat,
            'lon': lon,
            'zoom': zoom,
        }
        template = JINJA_ENVIRONMENT.get_template('index.html')
        self.response.write(template.render(template_values))

class AckHandler(BaseHandler):
    def post(self):
        if not referer_check(self.request):
            return
        uid = self.session.get('uid')
        memcache.delete('WTIME' + str(uid))

class HandShakeHandler(BaseHandler):
    def post(self):
        if not referer_check(self.request):
            return
        uid = self.session.get('uid')
        pid = self.request.get('pid')
        renew_channel = self.request.get('renew')

        logging.info('HandShakeHandler')
        if not is_pid(pid):
            logging.info('pid format error ')
            self.response.write(json.dumps({'error': 'pid format error'}))
            return
        if uid == None:
            uid = UserInfo.allocate_ids(1)[0]
            self.session['uid'] = uid

        if not isinstance(uid, (int, long)):
            logging.info('uid format error ')
            self.response.write(json.dumps({'error': 'pid format error'}))
            del self.session['uid']
            return

        logging.info('uid:' + str(uid))
        user_info, kick_channel = ndb.transaction(lambda: login_transaction(uid,
                                                              pid,
                                                              renew_channel))
        logging.info(user_info)
        logging.info(kick_channel)
        if kick_channel:
            logging.info('kicking off: kick_channel: ' + kick_channel + ' new_pid: ' + pid)
            channel.send_message(kick_channel, json.dumps({'pid': pid}))
        self.response.write(json.dumps({'token': user_info.channel_token}))

@ndb.transactional
def login_transaction(uid, pid, renew_channel):
    now = datetime.datetime.now()
    renew_token = False
    kick_channel = None

    key = ndb.Key(UserInfo, uid)
    user_info = key.get()
    if user_info is None:
        logging.info('new user')
        user_info = UserInfo(key=key)
        user_info.pid = pid
        user_info.channel_id = str(uuid.uuid4());
        renew_token = True
    else:
        logging.info('existing user')
        kick_channel = user_info.channel_id
        user_info.pid = pid
        if renew_channel:
            logging.info('new channel')
            user_info.channel_id = str(uuid.uuid4());
            renew_token = True
        if user_info.channel_token_available_time < now:
            logging.info('channel token is old')
            renew_token = True

    if renew_token:
        logging.info('renew_token')
        user_info.channel_token = channel.create_channel(
            user_info.channel_id,
            duration_minutes = CHANNEL_DURATION_MINUTES)
        user_info.channel_token_available_time = now + datetime.timedelta(0, CHANNEL_DURATION_MINUTES * 60)
    user_info.focused = True
    user_info.put()
    return user_info, kick_channel

def get_recent_chat_list_in_box(box):
    query_geocells = geo.geocell.best_bbox_search_cells(box, cost_function)
    memcache_results = memcache.get_multi(['MP' + cell for cell in query_geocells])
    messages = {}
    for mp_cell, mid_list in memcache_results.items():
        for mid in mid_list:
            message = MessageData(mid)
            if message.is_inside(box):
                messages[mid] = message

    futures = []
    for cell in query_geocells:
        if 'MP' + cell in memcache_results:
            futures.append(None)
            continue
        query = ChatMessagePos.query(ChatMessagePos.cells == cell).order(-ChatMessagePos.key)
        future = query.fetch_async(MESSAGE_LIST_MAX_COUNT * 2, keys_only=True)
        futures.append(future)

    mem_put_set = {}
    for i, future in enumerate(futures):
        if future == None:
            continue
        result = future.get_result();
        mid_for_mem_list = []
        for key in result:
            mid = key.id()
            mid_for_mem_list.append(mid)
            message = MessageData(mid)
            # messages[mid] = message
            if message.is_inside(box):
                messages[mid] = message
        mem_put_set['MP' + query_geocells[i]] = mid_for_mem_list

    if len(mem_put_set):
        client = memcache.Client()
        rpc = client.set_multi_async(mem_put_set)
    top_messages = sorted(set(messages.keys()), reverse=True)[0:MESSAGE_LIST_MAX_COUNT]
    chat_list = []

    user_info_futures = {}
    for message_key in top_messages:
        uid = messages[message_key].uid
        if uid in user_info_futures:
            continue
        key = ndb.Key(UserInfo, uid)
        user_info_futures[uid] = key.get_async()

    image_info_futures = {}
    for message_key in top_messages:
        iid = messages[message_key].iid
        if iid is None or iid in image_info_futures:
            continue
        key = ndb.Key(ImageInfo, iid)
        image_info_futures[iid] = key.get_async()

    user_info_map = {}
    for message_key in top_messages:
        message = messages[message_key]
        if message.user_info is None:
            message.user_info = user_info_futures[message.uid].get_result()
        if (not message.iid is None) and (message.image_info is None):
            message.image_info = image_info_futures[message.iid].get_result()
        chat_list.append(message.to_chat_dict())
    if len(mem_put_set):
        result = rpc.get_result()
        logging.info(result)
    return chat_list

class PosUpdateHandler(BaseHandler):
    def get(self):
        if not referer_check(self.request):
            return
        try:
            now = datetime.datetime.now()
            uid = self.session.get('uid')
            if not uid:
                return
            zoom = float(self.request.get('z'));
            pid = self.request.get('pid')
            bounds_str = self.request.get('b')
            box = get_box_from_bounds_str(bounds_str)
            if not box:
                self.response.write('error')
                return
            center = get_center_point(box)
            self.session['zoom'] = zoom
            self.session['lat'] = center.lat
            self.session['lon'] = center.lon

            default_queue = taskqueue.Queue('default')
            timestamp = now.isoformat();
            last_task_name = memcache.get('LASTPTASK' + str(uid))
            if last_task_name:
                logging.info('delete last_task_name ' + last_task_name)
                delete_task_rpc = default_queue.delete_tasks_by_name_async(last_task_name)

            memcache.set('PTIME' + str(uid), timestamp)
            new_task_name = str(uuid.uuid4());
            memcache.set('LASTPTASK' + str(uid), new_task_name)
            logging.info('new_task_name ' + new_task_name)

            rpc = default_queue.add_async(taskqueue.Task(url='/p_worker', name = new_task_name, params={
                'task_name': new_task_name,
                'timestamp': timestamp,
                'uid': uid,
                'pid': pid,
                's': box.south,
                'w': box.west,
                'n': box.north,
                'e': box.east,
            }, countdown = 1))
            chat_list = get_recent_chat_list_in_box(box)
            self.response.write(json.dumps({'chat_list': chat_list}))
            if last_task_name:
                result = delete_task_rpc.get_result()
            rpc.get_result()
            return
        except ValueError:
            self.response.write('error')


def cost_function(num_cells, resolution):
  """The default cost function, used if none is provided by the developer."""
  # return 1e10000 if num_cells > pow(geo.geocell._GEOCELL_GRID_SIZE, 2) else 0
  return 1e10000 if num_cells > 8 else 0


class PosUpdateWorkerHandler(webapp2.RequestHandler):
    def post(self):
        try:
            queuename = self.request.headers.get('X-AppEngine-QueueName')
            if queuename == None:
                logging.info('no quene name')
                return
            logging.info('PosUpdateWorkerHandler task_name' + self.request.get('task_name'))
            uid = self.request.get('uid')
            ptime = memcache.get('PTIME' + uid)
            timestamp = self.request.get('timestamp')
            if ptime and ptime != timestamp:
                return
            pid = self.request.get('pid')
            s = float(self.request.get('s'))
            w = float(self.request.get('w'))
            n = float(self.request.get('n'))
            e = float(self.request.get('e'))
            key = ndb.Key(UserInfo, long(uid))
            user_info = key.get()
            if not user_info:
                logging.info('user_info not match')
                return
            if user_info.pid != str(pid):
                logging.info('pid not match [' + user_info.pid + '][' + str(pid) + ']')
                return
            box = geo.geotypes.Box(n, e, s, w)
            geocells = geo.geocell.best_bbox_search_cells(box, cost_function)
            memcache.delete_multi(['UP' + cell for cell in geocells], seconds = 1)
            user_pos = UserPos(id = long(uid),
                               north = n,
                               east = e,
                               south = s,
                               west = w,
                               cells = geocells)
            user_pos.put()

            chat_list = get_recent_chat_list_in_box(box)
            now = datetime.datetime.now()
            if user_info.channel_token_available_time > now:
                json_message = json.dumps({'chat_list': chat_list, 'pid': pid})
                channel.send_message(user_info.channel_id, json_message)

        except ValueError:
            logging.error('ValueError')

class PostMessageHandler(BaseHandler):
    def post(self):
        logging.error('PostMessageHandler')
        if not referer_check(self.request):
            logging.error('referer_check error')
            return
        uid = self.session.get('uid')
        if not uid:
            logging.error('no uid')
            return
        pid = self.request.get('pid')
        key = ndb.Key(UserInfo, long(uid))
        user_info = key.get()
        if not user_info:
            logging.info('user_info not match')
            return
        lat = float(self.request.get('lat'))
        lon = float(self.request.get('lon'))
        content = self.request.get('content')
        try:
            content = base64.b64decode(content)
        except TypeError:
            logging.info('decode error')
        if len(content) > 140:
            self.response.write('too long')
            return
        if lon < -180.0:
            lon += 360.0
        point = geo.geotypes.Point(lat, lon)
        epoch = get_epoch_string(datetime.datetime.now())
        cell = geo.geocell.compute(point, resolution=GEOCELL_RESOLUTION)
        mid = epoch + cell + str(uid) + ':' + content
        logging.info('PostMessageHandler mid:' + mid)

        cells = [cell[:res] for res in range(1, GEOCELL_RESOLUTION + 1)]
        memcache.delete_multi(['MP' + cell for cell in cells], seconds = 1)
        chat_message_pos = ChatMessagePos(id = mid, cells = cells)
        chat_message_pos.put()
        taskqueue.add(url='/m_worker', params={
            'mid': mid,
        })

class PostMessageWorkerHandler(webapp2.RequestHandler):
    def post(self):
        queuename = self.request.headers.get('X-AppEngine-QueueName')
        if queuename == None:
            logging.info('no quene name')
            return
        now = datetime.datetime.now()
        mid = self.request.get('mid')
        logging.info('PostMessageWorkerHandler mid:' + mid)
        message_data = MessageData(mid)
        max_res_geocell = geo.geocell.compute(message_data.point)
        cells = [max_res_geocell[:res] for res in range(1, GEOCELL_RESOLUTION + 1)]
        memcache_results = memcache.get_multi(['UP' + cell for cell in cells])

        all_uid_set = set()
        for up_cell, uid_list in memcache_results.items():
            all_uid_set = all_uid_set.union(uid_list)
        futures = []

        for cell in cells:
            if 'UP' + cell in memcache_results:
                futures.append(None)
                continue
            future = UserPos.query(UserPos.cells == cell).fetch_async()
            futures.append(future)

        mem_put_set = {}
        for i, future in enumerate(futures):
            if future == None:
                continue
            result = future.get_result();
            uid_list = []
            for user_pos in result:
                uid_list.append(user_pos.key.id())
            mem_put_set['UP' + cells[i]] = uid_list
            all_uid_set = all_uid_set.union(uid_list)

        client = memcache.Client()
        rpc = client.set_multi_async(mem_put_set)

        user_pos_get_futures = ndb.get_multi_async([ndb.Key(UserPos, uid) for uid in all_uid_set])
        user_info_get_futures = []
        while user_pos_get_futures:
            future = ndb.Future.wait_any(user_pos_get_futures)
            user_pos_get_futures.remove(future)
            user_pos = future.get_result()
            is_inside = message_data.is_inside(geo.geotypes.Box(user_pos.north, user_pos.east, user_pos.south, user_pos.west))
            if is_inside:
                user_info_get_futures.append(UserInfo.get_by_id_async(user_pos.key.id()))
        json_message = json.dumps({
            'chat': message_data.to_chat_dict()})
        gcm_recipients = []
        gcm_retry_recipients = []
        channel_send_count = 0
        timestamp = datetime.datetime.now().isoformat()
        while user_info_get_futures:
            future = ndb.Future.wait_any(user_info_get_futures)
            user_info_get_futures.remove(future)
            user_info = future.get_result()
            send_via_channel = False
            gcm_subscription_id = None
            uid = user_info.key.id()

            if user_info.channel_token_available_time > now:
                send_via_channel = True
            if user_info.notification_endpoint:
                gcm_subscription_id = get_gcm_subscription_id(user_info.notification_endpoint)

            if gcm_subscription_id is not None:
                if not user_info.focused:
                    gcm_recipients.append({'uid':uid, 'gcm_subscription_id':gcm_subscription_id})
                elif send_via_channel:
                    logging.info('gcm_retry_recipients.append')
                    memcache.set('WTIME' + str(user_info.key.id()), timestamp)
                    gcm_retry_recipients.append({'uid':uid, 'gcm_subscription_id':gcm_subscription_id})

            if send_via_channel:
                channel.send_message(user_info.channel_id, json_message)
                channel_send_count += 1

        logging.info('Channel API count: %d' % channel_send_count)
        sendToGCM(gcm_recipients)
        result = rpc.get_result()
        self.response.write('ok')
        if (len(gcm_retry_recipients) == 0):
            return
        taskqueue.add(url='/m_retry_worker', params={
            'mid': mid,
            'recipients': json.dumps(gcm_retry_recipients),
            'timestamp': timestamp
        }, countdown = 2)

class PostMessageRetryWorkerHandler(webapp2.RequestHandler):
    def post(self):
        queuename = self.request.headers.get('X-AppEngine-QueueName')
        if queuename == None:
            logging.info('no quene name')
            return
        mid = self.request.get('mid')
        recipients = json.loads(self.request.get('recipients'))
        timestamp = self.request.get('timestamp')
        gcm_recipients = []
        logging.info('PostMessageRetryWorkerHandler ' + mid)
        logging.info(recipients)
        for recipient in recipients:
            mem_timestamp = memcache.get('WTIME' + str(recipient['uid']))
            if mem_timestamp and mem_timestamp == timestamp:
                gcm_recipients.append({'uid':recipient['uid'], 'gcm_subscription_id':recipient['gcm_subscription_id']})
        sendToGCM(gcm_recipients)

def sendToGCM(gcm_recipients):
    if (len(gcm_recipients) == 0):
        return
    logging.info('Sending to GCM. count: %d' % len(gcm_recipients))
    registration_ids = []
    for recipient in gcm_recipients:
        logging.info("GCM uid:%s gcm_id:%s" % (recipient.get('uid'), recipient.get('gcm_subscription_id')))
        registration_ids.append(recipient.get('gcm_subscription_id'))

    post_data = json.dumps({
        'registration_ids': registration_ids,
        'collapse_key': str(type),
    })
    result = urlfetch.fetch(url=GCM_ENDPOINT,
                            payload=post_data,
                            method=urlfetch.POST,
                            headers={
                                'Content-Type': 'application/json',
                                'Authorization': 'key=' + GCM_KEY,
                            },
                            validate_certificate=True,
                            allow_truncated=True)
    if result.status_code != 200:
        logging.error("GCM send failed %d:\n%s" % (result.status_code,
                                                   result.content))
        return
    try:
        logging.info("GCM result:\n%s" % (result.content))
        result_json = json.loads(result.content)
    except:
        logging.exception("Failed to decode GCM JSON response")
        return stats

    for i, res in enumerate(result_json['results']):
        if 'error' in res and res['error'] in PERMANENT_GCM_ERRORS:
            uid = gcm_recipients[i].get('uid')
            ndb.transaction(lambda: unscribe_transaction(uid))
    return

class ListMessageHandler(BaseHandler):
    def get(self):
        if not referer_check(self.request):
            return
        uid = self.session.get('uid')
        if not uid:
            logging.info('nouid')
            return
        user_pos = ndb.Key(UserPos, long(uid)).get()
        if not user_pos:
            logging.info('no user_pos')
            return
        box = geo.geotypes.Box(user_pos.north, user_pos.east, user_pos.south, user_pos.west)
        chat_list = get_recent_chat_list_in_box(box)
        self.response.write(json.dumps({'chat_list': chat_list}))

@ndb.transactional
def scribe_transaction(uid, endpoint):
    logging.info('scribe_transaction: ' + str(uid))
    key = ndb.Key(UserInfo, uid)
    user_info = key.get()
    if user_info is None:
        logging.info('invalid user: ' + uid)
        return
    user_info.notification_endpoint = endpoint
    user_info.put()

@ndb.transactional
def unscribe_transaction(uid):
    logging.info('unscribe_transaction: ' + str(uid))
    key = ndb.Key(UserInfo, uid)
    user_info = key.get()
    if user_info is None:
        logging.info('invalid user: ' + uid)
        return
    if user_info.notification_endpoint is None:
        logging.info('notification_endpoint is notset')
        return
    user_info.notification_endpoint = None
    user_info.put()


class SubscribeHandler(BaseHandler):
    def post(self):
        if not referer_check(self.request):
            return
        uid = self.session.get('uid')
        endpoint = self.request.get('endpoint')
        if not uid or not endpoint:
            return
        ndb.transaction(lambda: scribe_transaction(uid, endpoint))
        self.response.write('')

class UnsubscribeHandler(BaseHandler):
    def post(self):
        if not referer_check(self.request):
            return
        uid = self.session.get('uid')
        if not uid:
            return
        ndb.transaction(lambda: unscribe_transaction(uid))
        self.response.write('')

@ndb.transactional
def change_focus_transaction(uid, pid, focused):
    logging.info('change_focus_transaction: ' + str(uid) + '  ' + str(focused))
    key = ndb.Key(UserInfo, uid)
    user_info = key.get()
    if user_info is None:
        logging.info('invalid user: ' + uid)
        return
    if user_info.pid != pid:
        logging.info('pid miss match')
        return
    user_info.focused = focused
    user_info.put()

class ChangeFocusHandler(BaseHandler):
    def post(self):
        if not referer_check(self.request):
            return
        uid = self.session.get('uid')
        if not uid:
            return
        pid = self.request.get('pid')
        focuced = False
        if self.request.get('v'):
            focuced = True
        ndb.transaction(lambda: change_focus_transaction(uid, pid, focuced))
        self.response.write('')

@ndb.transactional
def change_name_transaction(uid, pid, name):
    logging.info('change_name_transaction: ' + str(uid))
    logging.info(name)
    logging.info(pid)
    key = ndb.Key(UserInfo, uid)
    user_info = key.get()
    if user_info is None:
        logging.info('invalid user: ' + uid)
        return
    if user_info.pid != pid:
        logging.info('pid miss match')
        logging.info('pid miss match user_info.pid:' + user_info.pid)
        logging.info('pid miss match pid' + pid)
        return
    user_info.user_name = name
    user_info.put()


class ChangeNameHandler(BaseHandler):
    def post(self):
        if not referer_check(self.request):
            return
        uid = self.session.get('uid')
        if not uid:
            return
        pid = self.request.get('pid')
        name = self.request.get('name')
        if len(name) > 140:
            self.response.write('too long')
            return
        if len(name) == 0:
            self.response.write('empty name')
            return
        ndb.transaction(lambda: change_name_transaction(uid, pid, name))
        self.response.write('')

class UserInfoHandler(BaseHandler):
    def post(self):
        if not referer_check(self.request):
            return
        uid = self.session.get('uid')
        key = ndb.Key(UserInfo, uid)
        user_info = key.get()
        logging.info(user_info.user_name)
        self.response.write(json.dumps({'user_name': user_info.user_name}))

class BlobUrlHandler(BaseHandler):
    def post(self):
        if not referer_check(self.request):
            return
        self.response.write(json.dumps({'url': blobstore.create_upload_url('/cm')}))

class CameraMessageHandler(BaseBlobHandler):
    def post(self):
        logging.info('CameraMessageHandler')
        upload_files = self.get_uploads('file')  # 'file' is file upload field in the form
        if len(upload_files) == 0:
            logging.error('nofile')
            return
        if not referer_check(self.request):
            return
        uid = self.session.get('uid')
        if not uid:
            return
        pid = self.request.get('pid')
        key = ndb.Key(UserInfo, long(uid))
        user_info = key.get()
        if not user_info:
            logging.info('user_info not match')
            return
        if user_info.pid != str(pid):
            logging.info('pid not match [' + user_info.pid + '][' + str(pid) + ']')
            return
        blob_info = upload_files[0]
        logging.info(blob_info.key())
        width = -1
        height = -1
        try:
            data = blobstore.fetch_data(blob_info.key(), 0, 1000000)
            img = images.Image(image_data=data)
            width = img.width
            height = img.height
            if not IsDevAppserver():
                img.rotate(0)
                img.execute_transforms(parse_source_metadata=True)
                logging.info(img.get_original_metadata())
        finally:
            logging.info("width %d" % width)
            logging.info("height %d" % height)
        iid = ImageInfo.allocate_ids(1)[0]
        logging.info("iid %d" % iid)
        image_url = images.get_serving_url(str(blob_info.key()), secure_url=True)
        logging.info(image_url)
        image_info = ImageInfo(id = iid,
                               blob_key = blob_info.key(),
                               url = image_url,
                               width = width,
                               height = height)
        image_info.put()

        lat = float(self.request.get('lat'))
        lon = float(self.request.get('lon'))
        content = self.request.get('content')
        try:
            content = base64.b64decode(content)
        except TypeError:
            logging.info('decode error')
        if len(content) > 140:
            self.response.write('too long')
            return
        point = geo.geotypes.Point(lat, lon)
        epoch = get_epoch_string(datetime.datetime.now())
        cell = geo.geocell.compute(point, resolution=GEOCELL_RESOLUTION)
        mid = epoch + cell + str(uid) + ',' + str(iid) + ':' + content
        logging.info('CameraMessageHandler mid:' + mid)
        cells = [cell[:res] for res in range(1, GEOCELL_RESOLUTION + 1)]
        memcache.delete_multi(['MP' + cell for cell in cells], seconds = 1)
        chat_message_pos = ChatMessagePos(id = mid, cells = cells)
        chat_message_pos.put()
        taskqueue.add(url='/m_worker', params={
            'mid': mid,
        })

class ServiceWorkerScriptHandler(BaseHandler):
    def get(self):
        self.response.headers['Content-Type'] = 'application/javascript'
        self.response.headers['Pragma'] = 'no-cache'
        self.response.headers['Cache-Control'] = 'no-cache'
        self.response.write('importScripts(\'sw_main.js?4\');')


app = webapp2.WSGIApplication([
    ('/', MainPage),
    ('/a', AckHandler),
    ('/h', HandShakeHandler),
    ('/p', PosUpdateHandler),
    ('/p_worker', PosUpdateWorkerHandler),
    ('/m', PostMessageHandler),
    ('/m_worker', PostMessageWorkerHandler),
    ('/m_retry_worker', PostMessageRetryWorkerHandler),
    ('/l', ListMessageHandler),
    ('/subscribe', SubscribeHandler),
    ('/unsubscribe', UnsubscribeHandler),
    ('/f', ChangeFocusHandler),
    ('/n', ChangeNameHandler),
    ('/u', UserInfoHandler),
    ('/bu', BlobUrlHandler),
    ('/cm', CameraMessageHandler),
    ('/sw.js', ServiceWorkerScriptHandler),
], debug = True, config = WEBAPP2_CONFIG)
