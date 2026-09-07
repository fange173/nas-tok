/* util.js — DOM 查询 / fetch / 格式化 / 复制剪贴板(由 split_frontend.py 机械切割,勿手改顺序) */

    /** localStorage 安全访问：Safari「阻止所有 Cookie」等环境下读写会抛异常 */
    export function lsGet(key) {
      try { return localStorage.getItem(key); } catch (_) { return null; }
    }

    export function lsSet(key, value) {
      try { localStorage.setItem(key, value); } catch (_) {}
    }

    export function isMobileFeed() {
      return window.matchMedia("(max-width: 640px)").matches;
    }

    export const $ = (sel) => document.querySelector(sel);
    export const $$ = (sel) => Array.from(document.querySelectorAll(sel));

    export async function api(url, options = {}) {
      const opts = {
        credentials: "include",
        headers: { "Content-Type": "application/json", ...(options.headers || {}) },
        ...options,
      };
      const res = await fetch(url, opts);
      let data = null;
      const ct = res.headers.get("content-type") || "";
      if (ct.includes("application/json")) {
        data = await res.json();
      } else {
        data = await res.text();
      }
      if (!res.ok) {
        const detail = (data && data.detail) || res.statusText;
        const err = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
        err.status = res.status;
        err.data = data;
        // 会话过期统一回登录页（登录接口自身的 401 除外），避免各界面只反复弹「未登录」无法恢复
        if (res.status === 401 && !url.startsWith("/api/auth/login") && !url.startsWith("/api/share/")) {
          // 首访探测 /api/auth/me 也会得到 401：仅当用户本就不在登录页
          // （会话中途失效）时才提示「已失效」，避免误导新访客
          const active = document.querySelector(".page.active");
          if (active && active.id !== "page-login") {
            const loginErr = document.getElementById("login-error");
            if (loginErr) loginErr.textContent = "登录已失效，请重新登录";
          }
          showPage("page-login");
        }
        throw err;
      }
      return data;
    }

    export function showPage(id) {
      $$(".page").forEach((p) => p.classList.remove("active"));
      const el = document.getElementById(id);
      if (el) el.classList.add("active");
    }

    export function fmtTime(s) {
      s = Math.floor(s || 0);
      const m = Math.floor(s / 60);
      const sec = s % 60;
      return `${m}:${String(sec).padStart(2, "0")}`;
    }

    /** 将 ISO / 时间戳等格式化为 YYYY-MM-DD HH:mm:ss */
    export function fmtDateTime(value) {
      if (value == null || value === "") return "—";
      let d;
      if (value instanceof Date) {
        d = value;
      } else if (typeof value === "number") {
        d = new Date(value > 1e12 ? value : value * 1000);
      } else {
        const raw = String(value).trim();
        // 已是友好格式则直接返回
        if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}(:\d{2})?$/.test(raw)) {
          return raw.length === 16 ? raw + ":00" : raw;
        }
        // 兼容 2024-01-01T12:00:00Z / 无时区
        d = new Date(raw.includes("T") || raw.endsWith("Z") ? raw : raw.replace(" ", "T"));
      }
      if (Number.isNaN(d.getTime())) return String(value);
      const p = (n) => String(n).padStart(2, "0");
      return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
    }

    /** datetime-local 输入值：YYYY-MM-DDTHH:mm */
    export function toDatetimeLocalValue(value) {
      const s = fmtDateTime(value);
      if (!s || s === "—") return "";
      return s.slice(0, 16).replace(" ", "T");
    }

    export function escapeHtml(str) {
      return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }

    export function scanModeLabel(mode) {
      return mode === "recursive" ? "递归子目录" : "仅根目录";
    }

    export function fmtBytes(n) {
      const x = Number(n) || 0;
      if (x < 1024) return x + " B";
      if (x < 1024 * 1024) return (x / 1024).toFixed(1) + " KB";
      if (x < 1024 * 1024 * 1024) return (x / 1024 / 1024).toFixed(1) + " MB";
      return (x / 1024 / 1024 / 1024).toFixed(1) + " GB";
    }

    export async function copyText(text) {
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          await navigator.clipboard.writeText(text);
          return true;
        }
      } catch (_) {}
      try {
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.left = "-9999px";
        document.body.appendChild(ta);
        ta.select();
        const ok = document.execCommand("copy");
        ta.remove();
        return ok;
      } catch (_) {
        return false;
      }
    }
