/* YaM: navigate only the browser that requested playback. */
(function () {
  'use strict';
  var attempts = 0;
  function attach() {
    var injector = window.angular && window.angular.element(document.documentElement).injector();
    if (!injector) {
      if (++attempts < 100) window.setTimeout(attach, 100);
      return;
    }
    var socket = injector.get('socketService');
    if (socket.yamNowPlaying) return;
    socket.yamNowPlaying = true;
    var state = injector.get('$state');
    var browse = injector.get('browseService');
    var root = injector.get('$rootScope');
    var pending = null;
    var timer;
    var emit = socket.emit;
    function clear() {
      pending = null;
      window.clearTimeout(timer);
    }
    socket.emit = function (event, data) {
      if (['playItemsList', 'addPlay', 'replaceAndPlay', 'addPlayList', 'play', 'stop'].indexOf(event) !== -1) {
        clear();
        var item = data && (data.item || data);
        if (item && item.service === 'yam' && /^yam\/track\/[0-9]+(?::[0-9]+)?$/.test(item.uri)) {
          pending = item.uri;
          timer = window.setTimeout(clear, 45000);
        }
      }
      if (event === 'playItemsList' && data && data.item && data.item.service === 'yam'
          && /^yam\/track\/[0-9]+(?::[0-9]+)?$/.test(data.item.uri)) {
        var request = data;
        // Grid play buttons omit list/index; recover the containing visible
        // list so Next follows that playlist instead of a single added song.
        if (!Array.isArray(data.list)) {
          var lists = browse.lists || [];
          for (var i = 0; i < lists.length; i++) {
            var items = lists[i].items || [];
            var index = items.indexOf(data.item);
            if (index < 0) index = items.findIndex(function (item) {
              return item.service === 'yam' && item.type === 'song' && item.uri === data.item.uri;
            });
            if (index >= 0) {
              request = Object.assign({}, data, {list: items, index: index});
              break;
            }
          }
        }
        return emit.call(this, 'callMethod', {
          endpoint: 'music_service/yam', method: 'playFromBrowse', data: request
        });
      }
      return emit.apply(this, arguments);
    };
    socket.on('yamPlaybackStarted', function (data) {
      if (!pending || !data || data.uri !== pending) return;
      clear();
      root.$evalAsync(function () { state.go('volumio.playback'); });
    });
    socket.on('disconnect', clear);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', attach);
  else attach();
}());
