'use strict';
const libQ = require('kew');

// MPD reads status and metadata in separate async commands. An update started
// before stop/clear/add/play must not finish the newly selected YaM track.
class MpdUpdates {
  constructor(mpd, ownsPlayback) {
    this.mpd = mpd;
    this.ownsPlayback = ownsPlayback;
    this.epoch = 0;
    this.active = false;
    this.transition = false;
    this.trackType = '';
    this.stamps = new WeakMap();
    this.getState = mpd.getState;
    this.pushState = mpd.pushState;
    const gate = this;
    this.wrappedGet = function () {
      const stamp = {epoch: gate.epoch, transition: gate.transition};
      return gate.getState.apply(this, arguments).then(state => {
        if (state && typeof state === 'object') gate.stamps.set(state, stamp);
        return state;
      });
    };
    this.wrappedPush = function (state) {
      if (gate.active && gate.ownsPlayback()) {
        const stamp = state && gate.stamps.get(state);
        if (gate.transition || !stamp || stamp.transition || stamp.epoch !== gate.epoch) return libQ.resolve();
        if (state.uri && state.uri !== gate.uri) return libQ.resolve();
        state = gate.withTrackType(state);
      }
      return gate.pushState.call(this, state);
    };
    mpd.getState = this.wrappedGet;
    mpd.pushState = this.wrappedPush;
  }

  withTrackType(state) {
    if (!state || !this.trackType) return state;
    return Object.assign({}, state, {trackType: this.trackType});
  }

  begin(uri, trackType) {
    this.active = true;
    this.transition = true;
    this.uri = uri;
    this.trackType = trackType || '';
    return ++this.epoch;
  }

  async ready(epoch) {
    // Read after the play command, bypassing queued pre-transition updates.
    const state = await this.getState.call(this.mpd);
    if (!this.active || epoch !== this.epoch) return;
    this.transition = false;
    return this.pushState.call(this.mpd, this.withTrackType(state));
  }

  cancel() {
    this.epoch++;
    this.active = false;
    this.transition = false;
  }

  close() {
    this.cancel();
    if (this.mpd.getState === this.wrappedGet) this.mpd.getState = this.getState;
    if (this.mpd.pushState === this.wrappedPush) this.mpd.pushState = this.pushState;
  }
}
module.exports = MpdUpdates;
