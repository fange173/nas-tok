/* auth.js — 登录/改密/登出表单与角色判断(由 split_frontend.py 机械切割,勿手改顺序) */
import { clearFeedTrack, enterFeed, invalidatePendingLoad, pauseAll } from './feed.js';
import { bindGlobalProgress } from './player.js';
import { state } from './state.js';
import { $, $$, api, showPage } from './util.js';

    /* ---------- 密码小眼睛 ---------- */
    $$(".toggle-eye").forEach((btn) => {
      btn.addEventListener("click", () => {
        const input = document.getElementById(btn.dataset.target);
        if (!input) return;
        const show = input.type === "password";
        input.type = show ? "text" : "password";
        btn.setAttribute("aria-label", show ? "隐藏密码" : "显示密码");
        btn.title = show ? "隐藏密码" : "显示密码";
      });
    });


    $("#login-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const err = $("#login-error");
      const btn = $("#login-btn");
      err.textContent = "";
      btn.disabled = true;
      try {
        const data = await api("/api/auth/login", {
          method: "POST",
          body: JSON.stringify({
            username: $("#login-username").value.trim(),
            password: $("#login-password").value,
            remember: $("#login-remember").checked,
          }),
        });
        state.user = data.user;
        if (data.user.must_change_password) {
          showPage("page-change-pwd");
        } else {
          enterFeed();
        }
      } catch (ex) {
        err.textContent = ex.message || "登录失败";
      } finally {
        btn.disabled = false;
      }
    });

    $("#change-pwd-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const err = $("#pwd-error");
      const btn = $("#change-pwd-btn");
      err.textContent = "";
      const np = $("#new-password").value;
      const cp = $("#confirm-password").value;
      if (np !== cp) {
        err.textContent = "两次输入的新密码不一致";
        return;
      }
      btn.disabled = true;
      btn.textContent = "提交中…";
      try {
        const data = await api("/api/auth/change-password", {
          method: "POST",
          body: JSON.stringify({
            old_password: $("#old-password").value,
            new_password: np,
          }),
        });
        state.user = data.user;
        enterFeed();
      } catch (ex) {
        err.textContent = ex.message || "修改失败";
      } finally {
        btn.disabled = false;
        btn.textContent = "确认修改";
      }
    });

    export async function doLogout() {
      try { await api("/api/auth/logout", { method: "POST" }); } catch (_) {}
      invalidatePendingLoad();
      pauseAll();
      clearFeedTrack();
      bindGlobalProgress(null);
      state.user = null;
      state.videos = [];
      state.index = 0;
      state.page = 1;
      state.seed = null;
      state.tagId = null;
      state.tagged = false;
      state.tags = null;
      state.query = "";
      state.tab = "all";
      state.viewed = new Set();
      showPage("page-login");
    }
    $("#logout-btn").addEventListener("click", doLogout);
    $("#admin-logout-btn").addEventListener("click", doLogout);


    export function isSysadmin() {
      const u = state.user;
      if (!u) return false;
      if (u.is_sysadmin) return true;
      return u.username === "admin" || u.role === "sysadmin";
    }

    export function isStaff() {
      const u = state.user;
      if (!u) return false;
      if (u.is_staff) return true;
      return u.role === "sysadmin" || u.role === "admin" || u.username === "admin";
    }

    export function roleLabel(role) {
      if (role === "sysadmin") return "系统管理员";
      if (role === "admin") return "管理员";
      return "用户";
    }

    export function canManageUser(target) {
      if (!state.user || !target) return false;
      if (target.id === state.user.id) return false;
      if (target.username === "admin" || target.role === "sysadmin" || target.is_sysadmin) return false;
      if (isSysadmin()) return target.role === "admin" || target.role === "user";
      if (state.user.role === "admin") return target.role === "user";
      return false;
    }

    export function canAssignLibraries(target) {
      if (!state.user || !target) return false;
      if (isSysadmin()) return true;
      if (state.user.role === "admin") {
        if (target.id === state.user.id) return true;
        if (target.username === "admin" || target.role === "sysadmin" || target.is_sysadmin) return false;
        return target.role === "user";
      }
      return false;
    }
