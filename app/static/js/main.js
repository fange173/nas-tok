/* main.js — 启动路由(分享/登录/Feed)与全局装配 */
// 注意：本模块由 index.html 以带版本参数的 URL 加载(/static/js/main.js?v=…)，任何裸
// `import './main.js'` 都会创建第二个模块实例、顶层副作用全部执行两遍。不要 import 本模块，
// 需要复用的函数请放到对应功能模块（如 enterFeed 在 feed.js）。
import './shell.js';
import { enterFeed } from './feed.js';
import { syncSettingsUi } from './player.js';
import { bindSearchPage } from './search.js';
import { openPublicShare } from './share-page.js';
import { loadServerSettings, state } from './state.js';
import { api, showPage } from './util.js';

    export async function bootstrap() {
      const shareMatch = location.pathname.match(/^\/s\/([A-Za-z0-9_-]+)$/);
      if (shareMatch) {
        await openPublicShare(shareMatch[1]);
        return;
      }
      try {
        const user = await api("/api/auth/me");
        state.user = user;
        // 登录后拉取逐用户设置(DB 真源),合并进 state 并回填开关;localStorage 仅离线兜底
        await loadServerSettings();
        syncSettingsUi();
        if (user.must_change_password) {
          showPage("page-change-pwd");
        } else {
          enterFeed();
        }
      } catch {
        showPage("page-login");
      }
    }

    bindSearchPage();
    bootstrap();

    // PWA:仅安全上下文(https/localhost)注册;http://局域网IP 不生效属预期
    if ("serviceWorker" in navigator && window.isSecureContext) {
      navigator.serviceWorker
        .register("/static/sw.js", { scope: "/" })
        .catch((err) => console.warn("ServiceWorker 注册失败:", err));
    }
