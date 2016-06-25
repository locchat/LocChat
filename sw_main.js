var VERSION = 1;
var STATIC_CACHE_NAME = 'static_' + VERSION;
var ORIGIN = location.protocol + '//' + location.hostname +
             (location.port ? ':' + location.port : '');
var STATIC_IMAGE_FILES =  [
    'ic_settings_24px.svg',
    'ic_my_location_24px.svg',
    'ic_send_24px.svg',
    'ic_photo_camera_black_24px.svg',
    'ic_notifications_active_24px.svg',
    'ic_notifications_none_24px.svg',
    'ic_notifications_off_24px.svg',
    'red_ball.png',
    'red_big_ball.png'];
var SATATIC_FILES = [
  ORIGIN + '/',
  ORIGIN + '/?homescreen',
  ORIGIN + '/_ah/channel/jsapi',
  location.protocol + '//fonts.googleapis.com/css?family=Abel',
  location.protocol + '//fonts.gstatic.com/s/abel/v6/UzN-iejR1VoXU2Oc-7LsbvesZW2xOQ-xsNqO47m55DA.woff2',
  ORIGIN + '/push.html'];

STATIC_IMAGE_FILES.forEach(function(x){
    SATATIC_FILES.push(ORIGIN + '/img/' + x);
  });

var STATIC_FILE_HASH = {};
SATATIC_FILES.forEach(function(x){ STATIC_FILE_HASH[x] = true; });

self.addEventListener('install', function(evt) {
  self.skipWaiting();
  var requests = SATATIC_FILES.map(function(url) {
    if (url.endsWith('.woff2'))
      return new Request(url, {mode:'cors'});
    return new Request(url, {mode:'no-cors'});
  });
  evt.waitUntil(
      caches.open(STATIC_CACHE_NAME)
        .then(function(cache) {
            return Promise.all(requests.map(function(request) {
              return fetch(request).then(function(response) {
                  return cache.put(request.url, response);
                })
            }));
          }));
});

self.addEventListener('activate', function(evt) {
  evt.waitUntil(
    self.clients.matchAll()
      .then(function(clients) {
          clients.forEach(function(client) {
              client.postMessage('reload');
            });
        })
      .then(function(clients) {
          return self.clients.claim();
        }));
});


self.addEventListener('fetch', function(evt) {
  if (STATIC_FILE_HASH[evt.request.url]) {
    evt.respondWith(
      caches.match(evt.request, {cacheName: STATIC_CACHE_NAME})
        .catch(function() { return fetch(evt.request); }));
    return;
  }
  if (evt.request.url.startsWith(location.protocol + '//csi.gstatic.com')) {
    evt.respondWith(new Response(''));
    return;
  }
});
self.addEventListener('push', function(event) {
  console.log('Received a push message', event);

  var title = 'LocChat';
  var body = 'You received a message.';
  var icon = '/img/locchat.png';
  var tag = 'push-notification-tag';

  event.waitUntil(
    clients.matchAll({ type: 'window' }).then(function(evt) {
      return fetch(new Request('/l',{mode:'same-origin', credentials:'include'}))
        .then(function(response){
            return response.json();
          })
        .then(function(data){
            console.log(data);
            if (data.chat_list && data.chat_list.length) {
              body = data.chat_list[0].user_name + ': ' +  data.chat_list[0].content;
            }
            return self.registration.showNotification(title, {
              body: body,
              icon: icon,
              tag: tag
            })
          })
        .catch(function(){
          self.registration.showNotification(title, {
            body: body,
            icon: icon,
            tag: tag
          })
        })
    })
  );
});

self.addEventListener('notificationclick', function(evt) {
  evt.notification.close();
  evt.waitUntil(
    clients.matchAll({ type: 'window' }).then(function(evt) {
      for(var i = 0 ; i < evt.length ; i++) {
        var c = evt[i];
        if((c.url == ORIGIN + '/push.html') && ('focus' in c))
          return c.focus();
      }
      if(clients.openWindow)
        return clients.openWindow('/');
    })
  );
}, false);


function sendRequestWithFile(formData) {
  return fetch(new Request('/bu', {method: 'POST', credentials:'include'}))
    .then(function(response) { return response.json(); })
    .then(function(json) {
      return fetch(new Request(json['url'],
                               {method: 'POST', body: formData, credentials:'include'}));
    })
}
function sendRequest(request) {
  if (!request)
    return Promise.resolve();
  var formData = new FormData();
  formData.append('pid', request.pid);
  formData.append('lat', request.lat);
  formData.append('lon', request.lon);
  formData.append('content', request.content);
  if (request.file) {
    formData.append('file', request.file);
    return sendRequestWithFile(formData);
  }
  return fetch(new Request('/m',
                           {method: 'POST', body: formData, credentials:'include'}));
}

var INDEXED_DB_NAME = 'send_queue';
var INDEXED_DB_VERSION = 1;
var STORE_NAME = 'messages';

function openDataBase() {
  return new Promise(function(resolve, reject) {
      var req = indexedDB.open(INDEXED_DB_NAME, INDEXED_DB_VERSION);
      req.onupgradeneeded = function (event) {
        req.result.createObjectStore(
            STORE_NAME, { keyPath: 'id' , autoIncrement: true });
      };
      req.onsuccess = function (event) { resolve(req.result); }
      req.onerror = reject;
    });
}
function getMessage(id) {
  return openDataBase().then(function(db) {
      return new Promise(function(resolve, reject) {
        var transaction = db.transaction(STORE_NAME, 'readonly');
        var store = transaction.objectStore(STORE_NAME);
        var req = store.get(id);
        req.onsuccess = function() { resolve(req.result); }
        req.onerror = reject;
      });
    });
}
function deleteMessage(id) {
  return openDataBase().then(function(db) {
      return new Promise(function(resolve, reject) {
        var transaction = db.transaction(STORE_NAME, 'readwrite');
        var store = transaction.objectStore(STORE_NAME);
        var req = store.delete(id);
        req.onsuccess = resolve;
        req.onerror = reject;
      });
    });
}
function sendAndDeleteMessage(id) {
  return getMessage(id).then(sendRequest).then(function() {
    return deleteMessage(id);
  });
}
self.addEventListener('sync', function(evt) {
  if (evt.tag.startsWith('send-msg:')) {
    var id = parseInt(evt.tag.substr(9))
    if (isNaN(id))
      return;
    evt.waitUntil(sendAndDeleteMessage(id));
  }
});
