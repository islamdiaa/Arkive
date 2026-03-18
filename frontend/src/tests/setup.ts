import '@testing-library/jest-dom/vitest';

// jsdom does not implement the Web Animations API used by Svelte 5 transitions (fade, fly, etc.)
// Svelte 5 uses `animation.onfinish = callback` to detect when a transition outro completes
// before removing DOM nodes. This stub fires onfinish immediately via microtask so the
// {#if} block removes the element and tests can assert its absence.
if (typeof Element !== 'undefined' && !Element.prototype.animate) {
	Element.prototype.animate = function () {
		let _onfinish: ((e: AnimationPlaybackEvent) => void) | null = null;
		let _playState = 'running';

		const animation = {
			get playState() {
				return _playState;
			},
			cancel: () => {
				_playState = 'idle';
				_onfinish = null;
			},
			finish: () => {
				_playState = 'finished';
			},
			pause: () => {},
			play: () => {},
			reverse: () => {},
			addEventListener: () => {},
			removeEventListener: () => {},
			effect: null as KeyframeEffect | null,
			currentTime: 0,
			get finished() {
				return Promise.resolve(animation as unknown as Animation);
			},
			get onfinish() {
				return _onfinish;
			},
			set onfinish(fn: ((e: AnimationPlaybackEvent) => void) | null) {
				_onfinish = fn;
				if (fn) {
					// Fire finish callback via microtask so Svelte removes the element
					Promise.resolve().then(() => {
						if (_onfinish === fn) {
							_playState = 'finished';
							fn({} as AnimationPlaybackEvent);
						}
					});
				}
			},
			oncancel: null,
		};
		return animation as unknown as Animation;
	};
}
