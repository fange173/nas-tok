/* admin.js — 后台管理(用户/库/分享/日志/标记)(由 split_frontend.py 机械切割,勿手改顺序) */
import { canAssignLibraries, canManageUser, isSysadmin, roleLabel } from './auth.js';
import { pauseAll, resetAndLoad } from './feed.js';
import { syncFeedShareState } from './share-page.js';
import { state } from './state.js';
import { $, $$, api, copyText, escapeHtml, fmtBytes, fmtDateTime, scanModeLabel, showPage } from './util.js';

    /* ---------- 后台 ---------- */
    export function showAdminToast(msg, ms) {
      const el = $("#admin-toast");
      if (!el) return;
      el.textContent = msg;
      el.classList.add("show");
      clearTimeout(el._t);
      el._t = setTimeout(() => el.classList.remove("show"), ms || 2400);
    }

    export const ADMIN_PANEL_LOADERS = {
      logs: () => loadLogs(),
      users: () => loadUsers(),
      libs: () => loadLibs(),
      shares: () => loadShares(),
      tags: () => loadAdminTags(),
    };

    export function loadAdminPanel(panel) {
      const loader = ADMIN_PANEL_LOADERS[panel];
      if (loader) loader();
    }

    // 用一份用户列表刷新日志/标记面板的用户筛选下拉
    export function fillUserFilters(data) {
      const logSel = $("#log-user-filter");
      const logCur = logSel.value;
      logSel.innerHTML = '<option value="">全部用户</option>';
      const tagSel = $("#tags-user-filter");
      const tagCur = tagSel.value;
      tagSel.innerHTML = '<option value="">全部可管理用户</option>';
      (data.items || []).forEach((u) => {
        const logOpt = document.createElement("option");
        logOpt.value = u.username;
        logOpt.textContent = u.username;
        logSel.appendChild(logOpt);
        const tagOpt = document.createElement("option");
        tagOpt.value = String(u.id);
        tagOpt.textContent = u.username;
        tagSel.appendChild(tagOpt);
      });
      logSel.value = logCur;
      tagSel.value = tagCur;
    }

    export function openAdmin(panel) {
      pauseAll();
      showPage("page-admin");
      const target = panel || "libs";
      $$(".admin-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.panel === target));
      $$(".panel").forEach((p) => p.classList.toggle("active", p.id === `panel-${target}`));
      loadAdminPanel(target);
      const activeTab = $(`.admin-tabs button[data-panel="${target}"]`);
      if (activeTab) activeTab.scrollIntoView({ block: "nearest", inline: "nearest" });
      requestAnimationFrame(updateAdminTabsMask);
      // users 面板的 loadUsers 已请求同一接口并刷新下拉，避免重复请求
      if (target !== "users") {
        api("/api/admin/users").then(fillUserFilters).catch(() => {});
      }
    }

    // 移动端 tab 条横向滚动：滚到底去掉渐隐遮罩
    export const adminTabsEl = $(".admin-tabs");
    export function updateAdminTabsMask() {
      if (!adminTabsEl) return;
      const atEnd = adminTabsEl.scrollLeft + adminTabsEl.clientWidth >= adminTabsEl.scrollWidth - 2;
      adminTabsEl.classList.toggle("scrolled-end", atEnd);
    }
    if (adminTabsEl) {
      adminTabsEl.addEventListener("scroll", updateAdminTabsMask, { passive: true });
      window.addEventListener("resize", updateAdminTabsMask);
    }

    $$(".admin-tabs button").forEach((btn) => {
      btn.addEventListener("click", () => {
        $$(".admin-tabs button").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        $$(".panel").forEach((p) => p.classList.remove("active"));
        $(`#panel-${btn.dataset.panel}`).classList.add("active");
        loadAdminPanel(btn.dataset.panel);
        // 确保被点击的 tab 滚进可视区域
        btn.scrollIntoView({ behavior: "smooth", block: "nearest", inline: "nearest" });
      });
    });

    let _logsReqSeq = 0;
    export async function loadLogs() {
      const seq = ++_logsReqSeq;
      const username = $("#log-user-filter").value;
      const action = $("#log-action-filter").value;
      const qs = new URLSearchParams({
        page: String(state.logsPage),
        limit: "30",
      });
      if (username) qs.set("username", username);
      if (action) qs.set("action", action);
      try {
        const data = await api(`/api/admin/logs?${qs}`);
        if (seq !== _logsReqSeq) return; // 已有更新的分页请求，丢弃旧响应
        state.logsTotal = data.total;
        const tbody = $("#logs-tbody");
        tbody.innerHTML = "";
        const actionMap = { login: "登录", view: "观看", edit: "编辑", delete: "删除", share: "分享", admin: "管理", tag_create: "新建标记", tag_update: "修改标记", tag_delete: "删除标记", tag_assign: "视频标记" };
        data.items.forEach((log) => {
          const tr = document.createElement("tr");
          tr.innerHTML = `
            <td>${escapeHtml(fmtDateTime(log.created_at))}</td>
            <td>${escapeHtml(log.username)}</td>
            <td>${escapeHtml(actionMap[log.action] || log.action)}</td>
            <td class="wrap">${escapeHtml(log.detail)}</td>`;
          tbody.appendChild(tr);
        });
        if (!data.items.length) {
          tbody.innerHTML = '<tr><td class="table-empty" colspan="4">暂无操作记录</td></tr>';
        }
        const pages = Math.max(1, Math.ceil(data.total / data.limit));
        $("#logs-page-info").textContent = `${data.page} / ${pages}（共 ${data.total}）`;
        $("#logs-prev").disabled = (data.page || 1) <= 1;
        $("#logs-next").disabled = (data.page || 1) >= pages;
      } catch (ex) {
        if (seq !== _logsReqSeq) return; // 旧请求失败晚归，不再打扰新结果
        showAdminToast(ex.message || "加载日志失败");
      }
    }

    $("#log-refresh").addEventListener("click", () => { state.logsPage = 1; loadLogs(); });
    $("#log-user-filter").addEventListener("change", () => { state.logsPage = 1; loadLogs(); });
    $("#log-action-filter").addEventListener("change", () => { state.logsPage = 1; loadLogs(); });
    $("#logs-prev").addEventListener("click", () => {
      if (state.logsPage > 1) { state.logsPage--; loadLogs(); }
    });
    $("#logs-next").addEventListener("click", () => {
      const pages = Math.max(1, Math.ceil(state.logsTotal / 30));
      if (state.logsPage < pages) { state.logsPage++; loadLogs(); }
    });

    export let _userLibsTarget = null;

    export function closeUserLibsModal() {
      _userLibsTarget = null;
      $("#user-libs-modal").classList.remove("open");
    }

    export async function openUserLibsModal(user) {
      _userLibsTarget = user;
      $("#user-libs-title").textContent = `配置存储库 · ${user.username}`;
      $("#user-libs-sub").textContent = "勾选后该用户只能看到对应存储库中的媒体。";
      const box = $("#user-libs-checks");
      box.innerHTML = '<div class="empty-sub">加载中…</div>';
      $("#user-libs-modal").classList.add("open");
      const seq = ++state._userLibsSeq;
      const targetId = user.id;
      try {
        const [libsData, userData] = await Promise.all([
          api("/api/admin/libraries"),
          api(`/api/admin/users/${user.id}/libraries`),
        ]);
        if (seq !== state._userLibsSeq || !_userLibsTarget || _userLibsTarget.id !== targetId) return;
        const selected = new Set(userData.library_ids || []);
        const libs = libsData.items || [];
        box.innerHTML = "";
        if (!libs.length) {
          box.innerHTML = '<div class="empty-sub">暂无存储库，请先在「存储库」面板添加</div>';
          return;
        }
        libs.forEach((lib) => {
          const label = document.createElement("label");
          label.className = "user-lib-check";
          const cb = document.createElement("input");
          cb.type = "checkbox";
          cb.value = String(lib.id);
          cb.checked = selected.has(lib.id);
          const text = document.createElement("span");
          text.textContent = `${lib.name}（${lib.path || "/"}）${lib.enabled ? "" : " · 已停用"}`;
          label.append(cb, text);
          box.appendChild(label);
        });
      } catch (ex) {
        box.innerHTML = `<div class="empty-sub">${escapeHtml(ex.message || "加载失败")}</div>`;
      }
    }

    $("#user-libs-cancel").addEventListener("click", closeUserLibsModal);
    $("#user-libs-modal").addEventListener("click", (e) => {
      if (e.target === $("#user-libs-modal")) closeUserLibsModal();
    });
    $("#user-libs-save").addEventListener("click", async () => {
      if (!_userLibsTarget) return;
      const targetId = _userLibsTarget.id;
      const ids = [...$$("#user-libs-checks input[type=checkbox]:checked")].map((el) => Number(el.value));
      try {
        await api(`/api/admin/users/${targetId}/libraries`, {
          method: "PUT",
          body: JSON.stringify({ library_ids: ids }),
        });
        showAdminToast("存储库已更新", 1800);
        closeUserLibsModal();
        loadUsers();
        if (targetId === state.user?.id) {
          state.seed = null;
          resetAndLoad(true);
        }
      } catch (ex) {
        showAdminToast(ex.message || "保存失败");
      }
    });

    export async function loadUsers() {
      try {
        const data = await api("/api/admin/users");
        fillUserFilters(data);
        const tbody = $("#users-tbody");
        tbody.innerHTML = "";
        const roleSel = $("#new-user-role");
        if (roleSel) {
          [...roleSel.options].forEach((opt) => {
            if (opt.value === "admin") opt.hidden = !isSysadmin();
          });
          if (!isSysadmin()) roleSel.value = "user";
        }
        if (!data.items.length) {
          tbody.innerHTML = '<tr><td class="table-empty" colspan="6">暂无用户</td></tr>';
          return;
        }
        data.items.forEach((u) => {
          const tr = document.createElement("tr");
          const badgeClass =
            u.role === "sysadmin" ? "badge-admin" : u.role === "admin" ? "badge-admin" : "badge-user";
          const libNames = (u.libraries || []).map((l) => l.name);
          const libText = libNames.length ? libNames.join("、") : "未分配";
          tr.innerHTML = `
            <td>${u.id}</td>
            <td>${escapeHtml(u.username)}</td>
            <td><span class="badge ${badgeClass}">${escapeHtml(roleLabel(u.role))}</span></td>
            <td title="${escapeHtml(libText)}">${escapeHtml(libText)}</td>
            <td><span class="badge ${u.is_active ? "badge-on" : "badge-off"}">${u.is_active ? "启用" : "禁用"}</span></td>
            <td></td>`;
          const td = tr.lastElementChild;
          td.style.display = "flex";
          td.style.gap = "6px";
          td.style.flexWrap = "wrap";

          if (canAssignLibraries(u)) {
            const libsBtn = document.createElement("button");
            libsBtn.type = "button";
            libsBtn.className = "btn btn-ghost btn-sm";
            libsBtn.textContent = "分配库";
            libsBtn.addEventListener("click", () => openUserLibsModal(u));
            td.appendChild(libsBtn);
          }

          if (canManageUser(u)) {
            const toggleBtn = document.createElement("button");
            toggleBtn.type = "button";
            toggleBtn.className = "btn btn-ghost btn-sm";
            toggleBtn.textContent = u.is_active ? "禁用" : "启用";
            toggleBtn.addEventListener("click", async () => {
              try {
                await api(`/api/admin/users/${u.id}/status`, {
                  method: "PUT",
                  body: JSON.stringify({ is_active: !u.is_active }),
                });
                showAdminToast(u.is_active ? "已禁用用户" : "已启用用户", 1800);
                loadUsers();
              } catch (ex) {
                showAdminToast(ex.message);
              }
            });

            const resetBtn = document.createElement("button");
            resetBtn.type = "button";
            resetBtn.className = "btn btn-ghost btn-sm";
            resetBtn.textContent = "重置密码";
            resetBtn.addEventListener("click", async () => {
              const pwd = prompt(`为 ${u.username} 设置新密码（至少 6 位）`);
              if (pwd == null) return;
              if (pwd.trim().length < 6) {
                showAdminToast("密码至少 6 位");
                return;
              }
              try {
                await api(`/api/admin/users/${u.id}/password`, {
                  method: "PUT",
                  body: JSON.stringify({ password: pwd.trim() }),
                });
                showAdminToast("密码已重置", 1800);
              } catch (ex) {
                showAdminToast(ex.message);
              }
            });

            const delBtn = document.createElement("button");
            delBtn.type = "button";
            delBtn.className = "btn btn-sm btn-danger-ghost";
            delBtn.textContent = "删除";
            delBtn.addEventListener("click", async () => {
              if (!confirm(`确定删除用户「${u.username}」？此操作不可恢复。`)) return;
              try {
                await api(`/api/admin/users/${u.id}`, { method: "DELETE" });
                showAdminToast("用户已删除", 1800);
                loadUsers();
              } catch (ex) {
                showAdminToast(ex.message);
              }
            });

            td.append(toggleBtn, resetBtn, delBtn);
          } else if (!canAssignLibraries(u) && !td.childNodes.length) {
            td.textContent = u.id === state.user?.id ? "当前账户" : "—";
          }
          tbody.appendChild(tr);
        });
      } catch (ex) {
        showAdminToast(ex.message);
      }
    }

    $("#create-user-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await api("/api/admin/users", {
          method: "POST",
          body: JSON.stringify({
            username: $("#new-user-name").value.trim(),
            password: $("#new-user-pass").value,
            role: $("#new-user-role").value,
          }),
        });
        $("#new-user-name").value = "";
        $("#new-user-pass").value = "";
        showAdminToast("用户已创建", 1800);
        loadUsers();
      } catch (ex) {
        showAdminToast(ex.message || "创建失败");
      }
    });

    export async function loadLibs() {
      try {
        const data = await api("/api/admin/libraries");
        $("#lib-hint").innerHTML = `<strong>媒体根目录：</strong>${escapeHtml(data.media_root)}<br>${escapeHtml(data.hint || "")}`;

        const discoverList = $("#discover-list");
        discoverList.innerHTML = "";
        if (data.discoverable && data.discoverable.length) {
          data.discoverable.forEach((d) => {
            const chip = document.createElement("button");
            chip.type = "button";
            chip.className = "discover-chip";
            chip.textContent = `快捷：${d.name}`;
            chip.addEventListener("click", () => {
              $("#new-lib-name").value = d.name;
              $("#new-lib-path").value = d.path || "";
            });
            discoverList.appendChild(chip);
          });
        }

        const tbody = $("#libs-tbody");
        tbody.innerHTML = "";
        if (!data.items.length) {
          tbody.innerHTML = '<tr><td class="table-empty" colspan="8">暂无存储库，可手动添加或浏览目录添加</td></tr>';
        }
        data.items.forEach((lib) => {
          const tr = document.createElement("tr");
          tr.innerHTML = `
            <td>${escapeHtml(lib.name)}</td>
            <td class="wrap">${escapeHtml(lib.path || "/")}</td>
            <td>${lib.video_count ?? 0}</td>
            <td>${fmtBytes(lib.byte_size)}</td>
            <td>${lib.exists ? "是" : "否"}</td>
            <td>${escapeHtml(scanModeLabel(lib.scan_mode))}</td>
            <td><span class="badge ${lib.enabled ? "badge-on" : "badge-off"}">${lib.enabled ? "启用" : "停用"}</span></td>
            <td></td>`;
          const actions = tr.lastElementChild;
          actions.style.display = "flex";
          actions.style.gap = "6px";
          actions.style.flexWrap = "wrap";

          const toggleBtn = document.createElement("button");
          toggleBtn.type = "button";
          toggleBtn.className = "btn btn-ghost btn-sm";
          toggleBtn.textContent = lib.enabled ? "停用" : "启用";
          toggleBtn.addEventListener("click", async () => {
            try {
              await api(`/api/admin/libraries/${lib.id}`, {
                method: "PUT",
                body: JSON.stringify({ enabled: !lib.enabled }),
              });
              showAdminToast(lib.enabled ? "已停用存储库" : "已启用存储库", 1800);
              loadLibs();
              refreshBrowserIfOpen();
            } catch (ex) {
              showAdminToast(ex.message);
            }
          });

          const delBtn = document.createElement("button");
          delBtn.type = "button";
          delBtn.className = "btn btn-sm btn-danger-ghost";
          delBtn.textContent = "删除";
          delBtn.addEventListener("click", async () => {
            if (!confirm(`删除存储库「${lib.name}」？\n仅删除配置，不会删除磁盘视频文件。`)) return;
            try {
              await api(`/api/admin/libraries/${lib.id}`, { method: "DELETE" });
              showAdminToast("存储库已删除", 1800);
              loadLibs();
              refreshBrowserIfOpen();
            } catch (ex) {
              showAdminToast(ex.message || "删除失败");
            }
          });

          actions.append(toggleBtn, delBtn);
          tbody.appendChild(tr);
        });
      } catch (ex) {
        showAdminToast(ex.message);
      }
    }

    export function isBrowserModalOpen() {
      return $("#browser-modal").classList.contains("open");
    }

    export function refreshBrowserIfOpen() {
      if (isBrowserModalOpen()) loadBrowser(state.browserPath || "");
    }

    export function openBrowserModal() {
      $("#browser-modal").classList.add("open");
      loadBrowser(state.browserPath || "");
    }

    export function closeBrowserModal() {
      $("#browser-modal").classList.remove("open");
    }

    export async function loadBrowser(path) {
      try {
        const qs = new URLSearchParams();
        if (path) qs.set("path", path);
        const data = await api(`/api/admin/libraries/browse?${qs}`);
        state.browserPath = data.path || "";
        state.browserParent = data.parent || "";
        state.browserRegistered = !!data.registered;

        const label = data.path ? `/${data.path}` : "/";
        $("#browser-path-label").textContent = label;
        $("#browser-up").disabled = !data.path;
        $("#browser-add-current").disabled = !!data.registered;
        $("#browser-add-current").textContent = data.registered
          ? (data.path ? "当前目录已添加" : "根目录已作为默认库")
          : "添加当前目录为库";

        const list = $("#browser-list");
        list.innerHTML = "";
        if (!data.children.length) {
          list.innerHTML = '<div class="browser-empty">此目录下没有子文件夹</div>';
          return;
        }
        data.children.forEach((child) => {
          const row = document.createElement("div");
          row.className = "browser-row";
          const name = document.createElement("div");
          name.className = "name";
          name.textContent = `📁 ${child.name}`;
          name.title = "进入目录";
          name.addEventListener("click", () => loadBrowser(child.path));

          const meta = document.createElement("div");
          meta.className = "meta";
          meta.textContent = child.registered
            ? `已添加 · ${child.library?.name || ""}`
            : `视频约 ${child.direct_video_count || 0} 个${child.has_subdir ? " · 有子目录" : ""}`;

          const addBtn = document.createElement("button");
          addBtn.type = "button";
          addBtn.className = "btn btn-sm " + (child.registered ? "btn-ghost" : "btn-primary");
          addBtn.textContent = child.registered ? "已添加" : "添加";
          addBtn.disabled = !!child.registered;
          addBtn.addEventListener("click", async (e) => {
            e.stopPropagation();
            await addLibraryFromPath(child.path, child.name);
          });

          row.append(name, meta, addBtn);
          list.appendChild(row);
        });
      } catch (ex) {
        showAdminToast(ex.message || "浏览目录失败");
      }
    }

    export async function addLibraryFromPath(path, defaultName) {
      const name = prompt("存储库名称", defaultName || path.split("/").pop() || "新媒体库");
      if (name == null) return;
      const trimmed = name.trim();
      if (!trimmed) {
        showAdminToast("名称不能为空");
        return;
      }
      try {
        await api("/api/admin/libraries", {
          method: "POST",
          body: JSON.stringify({ name: trimmed, path: path || "", enabled: true }),
        });
        showAdminToast(`已添加存储库「${trimmed}」`, 2000);
        loadLibs();
        refreshBrowserIfOpen();
      } catch (ex) {
        showAdminToast(ex.message || "添加失败");
      }
    }

    $("#open-browser-btn").addEventListener("click", () => openBrowserModal());
    $("#browser-close").addEventListener("click", () => closeBrowserModal());
    $("#browser-modal").addEventListener("click", (e) => {
      if (e.target === $("#browser-modal")) closeBrowserModal();
    });

    $("#browser-up").addEventListener("click", () => {
      loadBrowser(state.browserParent || "");
    });
    $("#browser-root").addEventListener("click", () => loadBrowser(""));
    $("#browser-add-current").addEventListener("click", () => {
      const path = state.browserPath || "";
      const defaultName = path ? path.split("/").pop() : "默认库";
      addLibraryFromPath(path, defaultName);
    });

    $("#backup-download-btn")?.addEventListener("click", async () => {
      try {
        const res = await fetch("/api/admin/backup", { method: "POST", credentials: "include" });
        if (!res.ok) {
          let detail = res.statusText;
          try { const j = await res.json(); detail = j.detail || detail; } catch (_) {}
          throw new Error(detail);
        }
        const blob = await res.blob();
        const a = document.createElement("a");
        const cd = res.headers.get("Content-Disposition") || "";
        const m = cd.match(/filename="?([^"]+)"?/);
        a.href = URL.createObjectURL(blob);
        a.download = (m && m[1]) || "nasTok-backup.db";
        a.click();
        URL.revokeObjectURL(a.href);
        showAdminToast("备份已开始下载");
      } catch (ex) {
        showAdminToast(ex.message || "备份失败");
      }
    });
    $("#backup-restore-input")?.addEventListener("change", async (e) => {
      const file = e.target.files && e.target.files[0];
      e.target.value = "";
      if (!file) return;
      if (!confirm("恢复备份会替换当前数据库，确定继续？")) return;
      const fd = new FormData();
      fd.append("file", file);
      try {
        const res = await fetch("/api/admin/restore", { method: "POST", credentials: "include", body: fd });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || res.statusText);
        showAdminToast("恢复成功，请重新登录");
        setTimeout(() => location.reload(), 800);
      } catch (ex) {
        showAdminToast(ex.message || "恢复失败");
      }
    });
    $("#lib-sync-btn").addEventListener("click", async () => {
      const btn = $("#lib-sync-btn");
      btn.disabled = true;
      btn.textContent = "同步中…";
      try {
        const data = await api("/api/admin/libraries/sync", { method: "POST" });
        if (data.added && data.added.length) {
          const names = data.added.slice(0, 3).join("、");
          const more = data.added.length > 3 ? ` 等 ${data.added.length} 个` : "";
          showAdminToast(`已同步：${names}${more}`, 2800);
        } else {
          showAdminToast("没有新的一级子目录需要同步", 2200);
        }
        loadLibs();
        refreshBrowserIfOpen();
      } catch (ex) {
        showAdminToast(ex.message || "同步失败");
      } finally {
        btn.disabled = false;
        btn.textContent = "同步一级子目录";
      }
    });

    $("#create-lib-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await api("/api/admin/libraries", {
          method: "POST",
          body: JSON.stringify({
            name: $("#new-lib-name").value.trim(),
            path: $("#new-lib-path").value.trim(),
            enabled: true,
          }),
        });
        $("#new-lib-name").value = "";
        $("#new-lib-path").value = "";
        showAdminToast("存储库已添加", 1800);
        loadLibs();
        refreshBrowserIfOpen();
      } catch (ex) {
        showAdminToast(ex.message || "添加失败");
      }
    });

    let _sharesReqSeq = 0;
    export async function loadShares() {
      const seq = ++_sharesReqSeq;
      try {
        const qs = new URLSearchParams({
          page: String(state.sharesPage || 1),
          limit: "30",
        });
        const data = await api(`/api/admin/shares?${qs}`);
        if (seq !== _sharesReqSeq) return; // 已有更新的分页请求，丢弃旧响应
        state.sharesTotal = data.total || 0;
        const pages = Math.max(1, Math.ceil((data.total || 0) / (data.limit || 30)));
        $("#shares-page-info").textContent = `${data.page || 1} / ${pages}`;
        $("#shares-prev").disabled = (data.page || 1) <= 1;
        $("#shares-next").disabled = (data.page || 1) >= pages;

        const tbody = $("#shares-tbody");
        tbody.innerHTML = "";
        if (!data.items || !data.items.length) {
          tbody.innerHTML = '<tr><td class="table-empty" colspan="9">暂无分享</td></tr>';
          return;
        }
        data.items.forEach((s) => {
          const tr = document.createElement("tr");
          tr.innerHTML = `
            <td>${escapeHtml(s.title || "")}</td>
            <td class="wrap">${escapeHtml(s.path || "")}</td>
            <td>${escapeHtml(s.created_by_name || "")}</td>
            <td>${escapeHtml(fmtDateTime(s.created_at))}</td>
            <td>${s.view_count ?? 0}</td>
            <td>${s.unique_view_count ?? 0}</td>
            <td>${escapeHtml(s.last_viewed_at || "-")}</td>
            <td><span class="badge ${s.is_active ? "badge-on" : "badge-off"}">${s.is_active ? "有效" : "已停用"}</span></td>
            <td></td>`;
          const actions = tr.lastElementChild;
          actions.style.display = "flex";
          actions.style.gap = "6px";
          actions.style.flexWrap = "wrap";

          const copyBtn = document.createElement("button");
          copyBtn.type = "button";
          copyBtn.className = "btn btn-ghost btn-sm";
          copyBtn.textContent = "复制";
          copyBtn.addEventListener("click", async () => {
            const url = `${location.origin}${s.url}`;
            const ok = await copyText(url);
            showAdminToast(ok ? "已复制分享链接" : url, 2200);
          });

          const detailBtn = document.createElement("button");
          detailBtn.type = "button";
          detailBtn.className = "btn btn-ghost btn-sm";
          detailBtn.textContent = "明细";
          detailBtn.addEventListener("click", () => openShareDetail(s.id, s.title));

          const toggleBtn = document.createElement("button");
          toggleBtn.type = "button";
          toggleBtn.className = "btn btn-ghost btn-sm";
          toggleBtn.textContent = s.is_active ? "停用" : "启用";
          toggleBtn.addEventListener("click", async () => {
            try {
              const nextActive = !s.is_active;
              await api(`/api/admin/shares/${s.id}`, {
                method: "PUT",
                body: JSON.stringify({ is_active: nextActive }),
              });
              showAdminToast(nextActive ? "已启用分享" : "已停用分享", 1800);
              syncFeedShareState(s.path, nextActive);
              loadShares();
            } catch (ex) {
              showAdminToast(ex.message || "操作失败");
            }
          });

          const delBtn = document.createElement("button");
          delBtn.type = "button";
          delBtn.className = "btn btn-sm btn-danger-ghost";
          delBtn.textContent = "删除";
          delBtn.addEventListener("click", async () => {
            if (!confirm(`删除分享「${s.title}」？\n公开链接将立即失效。`)) return;
            try {
              await api(`/api/admin/shares/${s.id}`, { method: "DELETE" });
              showAdminToast("分享已删除", 1800);
              syncFeedShareState(s.path, false);
              $("#share-detail-card").classList.add("hidden");
              loadShares();
            } catch (ex) {
              showAdminToast(ex.message || "删除失败");
            }
          });

          actions.append(copyBtn, detailBtn, toggleBtn, delBtn);
          tbody.appendChild(tr);
        });
      } catch (ex) {
        if (seq !== _sharesReqSeq) return; // 旧请求失败晚归，不再打扰新结果
        showAdminToast(ex.message || "加载分享失败");
      }
    }

    export async function openShareDetail(id, title) {
      try {
        const data = await api(`/api/admin/shares/${id}`);
        $("#share-detail-card").classList.remove("hidden");
        $("#share-detail-title").textContent = `访问明细 · ${title || ("#" + id)}`;
        const tbody = $("#share-detail-tbody");
        tbody.innerHTML = "";
        if (!data.recent_views || !data.recent_views.length) {
          tbody.innerHTML = '<tr><td class="table-empty" colspan="3">暂无访问记录</td></tr>';
          return;
        }
        data.recent_views.forEach((v) => {
          const tr = document.createElement("tr");
          tr.innerHTML = `
            <td>${escapeHtml(v.viewed_at || "")}</td>
            <td>${escapeHtml((v.ip_hash || "").slice(0, 10))}</td>
            <td class="wrap">${escapeHtml(v.user_agent || "")}</td>`;
          tbody.appendChild(tr);
        });
      } catch (ex) {
        showAdminToast(ex.message || "加载明细失败");
      }
    }

    $("#shares-refresh").addEventListener("click", () => loadShares());
    $("#shares-prev").addEventListener("click", () => {
      if (state.sharesPage > 1) {
        state.sharesPage -= 1;
        loadShares();
      }
    });
    $("#shares-next").addEventListener("click", () => {
      const pages = Math.max(1, Math.ceil((state.sharesTotal || 0) / 30));
      if (state.sharesPage < pages) {
        state.sharesPage += 1;
        loadShares();
      }
    });
    $("#share-detail-close").addEventListener("click", () => {
      $("#share-detail-card").classList.add("hidden");
    });


    /* ---------- 后台标记管理 ---------- */

    let _tagsReqSeq = 0;
    export async function loadAdminTags() {
      if (!$("#panel-tags") || !$("#panel-tags").classList.contains("active")) return;
      const seq = ++_tagsReqSeq;
      try {
        const uf = $("#tags-user-filter");
        const userId = uf ? uf.value : "";
        const params = new URLSearchParams({ page: String(state.tagsAdminPage || 1), limit: "50" });
        if (userId) params.set("user_id", userId);
        const data = await api(`/api/admin/tags?${params}`);
        if (seq !== _tagsReqSeq) return; // 已有更新的分页请求，丢弃旧响应
        state.tagsAdminTotal = data.total;
        const tbody = $("#tags-tbody");
        tbody.innerHTML = "";
        data.items.forEach((t) => {
          const tr = document.createElement("tr");
          tr.innerHTML = `<td>${escapeHtml(t.username)}</td><td>${escapeHtml(t.name)}</td><td>${escapeHtml(t.created_at)}</td><td>${escapeHtml(t.updated_at)}</td><td></td>`;
          const td = tr.querySelector("td:last-child");
          const renameBtn = document.createElement("button");
          renameBtn.type = "button";
          renameBtn.className = "btn btn-ghost btn-sm";
          renameBtn.textContent = "重命名";
          renameBtn.addEventListener("click", () => {
            const newName = prompt("重命名标记", t.name);
            if (!newName || !newName.trim() || newName.trim() === t.name) return;
            api(`/api/admin/users/${t.user_id}/tags/${t.id}`, {
              method: "PUT",
              body: JSON.stringify({ name: newName.trim() }),
            }).then(() => loadAdminTags()).catch((ex) => showAdminToast(ex.message || "失败", 2000));
          });
          const delBtn = document.createElement("button");
          delBtn.type = "button";
          delBtn.className = "btn btn-ghost btn-sm btn-danger-ghost";
          delBtn.textContent = "删除";
          delBtn.addEventListener("click", () => {
            if (!confirm(`确定删除用户 ${t.username} 的标记「${t.name}」？`)) return;
            api(`/api/admin/users/${t.user_id}/tags/${t.id}`, { method: "DELETE" })
              .then(() => loadAdminTags()).catch((ex) => showAdminToast(ex.message || "失败", 2000));
          });
          td.append(renameBtn, delBtn);
          tbody.appendChild(tr);
        });
        if (!data.items.length) {
          tbody.innerHTML = '<tr><td class="table-empty" colspan="5">暂无标记</td></tr>';
        }
        const pages = Math.max(1, Math.ceil(data.total / 50));
        state.tagsAdminPage = data.page;
        $("#tags-page-info").textContent = `${data.page} / ${pages}`;
        $("#tags-prev").disabled = data.page <= 1;
        $("#tags-next").disabled = data.page >= pages;
      } catch (ex) {
        if (seq !== _tagsReqSeq) return; // 旧请求失败晚归，不再打扰新结果
        showAdminToast(ex.message || "加载失败", 2200);
      }
    }

    $("#tags-user-filter").addEventListener("change", () => { state.tagsAdminPage = 1; loadAdminTags(); });
    $("#tags-refresh").addEventListener("click", () => { state.tagsAdminPage = 1; loadAdminTags(); });
    $("#tags-prev").addEventListener("click", () => {
      if ((state.tagsAdminPage || 1) > 1) { state.tagsAdminPage = (state.tagsAdminPage || 1) - 1; loadAdminTags(); }
    });
    $("#tags-next").addEventListener("click", () => {
      const pages = Math.max(1, Math.ceil((state.tagsAdminTotal || 0) / 50));
      if ((state.tagsAdminPage || 1) < pages) { state.tagsAdminPage = (state.tagsAdminPage || 1) + 1; loadAdminTags(); }
    });
