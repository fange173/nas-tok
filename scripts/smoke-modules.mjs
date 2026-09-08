/**
 * smoke-modules.mjs — 无浏览器环境下 import 全部前端模块。
 * 能抓住:语法错误、import 未解析、TDZ/eval 顺序崩溃(模块顶层副作用真实执行)。
 * 用法:node scripts/smoke-modules.mjs
 */
const noop = () => {};

function fakeEl() {
  return {
    addEventListener: noop,
    removeEventListener: noop,
    classList: { add: noop, remove: noop, toggle: noop, contains: () => false },
    style: {},
    dataset: {},
    value: "",
    checked: false,
    textContent: "",
    innerHTML: "",
    insertAdjacentHTML: noop,
    disabled: false,
    title: "",
    append: noop,
    appendChild: noop,
    insertBefore: noop,
    remove: noop,
    setAttribute: noop,
    getAttribute: () => null,
    removeAttribute: noop,
    click: noop,
    closest: () => null,
    querySelector: () => fakeEl(),
    querySelectorAll: () => [],
    scrollIntoView: noop,
    setPointerCapture: noop,
    releasePointerCapture: noop,
    focus: noop,
    play: () => Promise.resolve(),
    pause: noop,
    load: noop,
  };
}

const store = new Map();
globalThis.window = globalThis;
globalThis.self = globalThis;
globalThis.addEventListener = noop;
globalThis.removeEventListener = noop;
globalThis.dispatchEvent = () => true;
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};
globalThis.location = { pathname: "/", href: "http://x/", origin: "http://x" };
// node >= 21 自带只读 navigator,无需注入;代码内对 navigator.* 均有判空
globalThis.matchMedia = () => ({ matches: false, media: "", addEventListener: noop });
globalThis.document = {
  querySelector: () => fakeEl(),
  querySelectorAll: () => [],
  getElementById: () => fakeEl(),
  createElement: () => fakeEl(),
  addEventListener: noop,
  removeEventListener: noop,
  body: fakeEl(),
  fullscreenElement: null,
  execCommand: () => false,
};
globalThis.fetch = () => Promise.reject(new TypeError("smoke: no network"));
globalThis.requestAnimationFrame = () => 0;
globalThis.cancelAnimationFrame = noop;
globalThis.HTMLMediaElement = { NETWORK_NO_SOURCE: 3 };

const base = "../app/static/js/";
const main = await import(base + "main.js");
const { state } = await import(base + "state.js");

if (!state || !Array.isArray(state.videos)) throw new Error("state 未正确导出");
if (typeof main.bootstrap !== "function") throw new Error("bootstrap 未导出");

const feed = await import(base + "feed.js");
const player = await import(base + "player.js");
for (const [m, fns] of [
  [feed, ["enterFeed", "resetAndLoad", "playCurrent", "nextVideo", "streamUrl", "showFeedToast"]],
  [player, ["revealChrome", "bumpChrome", "setAlbumIndex", "updateDockForKind"]],
]) {
  for (const fn of fns) {
    if (typeof m[fn] !== "function") throw new Error(`缺少导出: ${fn}`);
  }
}

// 顶层副作用(事件绑定/bootstrap())已在 import 时执行过;至此无异常即通过
console.log("SMOKE OK: 11 个模块全部链接成功,顶层副作用无崩溃,关键导出齐全");
