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
