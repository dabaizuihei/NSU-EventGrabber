(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const api = () => (window.pywebview && window.pywebview.api) || null;
  const checkedNotifyProviders = () =>
    Array.from(document.querySelectorAll("[data-notify]"))
      .filter((el) => el.checked)
      .map((el) => el.value);

  let cursor = 0;
  let filter = "all";
  let running = false;
  let lastLogSignature = "";
  let polling = false;
  let refreshQueued = false;
  let notificationHighWater = 0;
  let lastState = null;
  let autoScanStarted = false;
  let loginPending = false;
  let latestNoticeKey = "";
  let latestNoticeTimer = null;
  const recentToastKeys = new Map();
  const tagState = { blocklist: [], include: [], exclude: [] };

  // ---------------------------------------------------------------- 工具
  const esc = (text) =>
    String(text ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");

  const pad = (n) => String(n).padStart(2, "0");

  const clock = (ts) => {
    if (!ts) return "尚未扫描";
    const d = new Date(ts * 1000);
    return `上次扫描 ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  };

  const displayVersion = (value) => {
    const text = String(value || "").trim().replace(/^v/i, "");
    return text ? `v${text}` : "v1.0.3";
  };

  const duration = (seconds) => {
    if (!seconds || seconds <= 0) return "未记录";
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    if (h > 0) return `${h} 小时 ${m} 分`;
    return `${Math.max(1, m)} 分钟`;
  };

  function toast(title, content, isError) {
    // 同一条通知可能因为并发刷新/重复点击在极短时间内到达两次。
    // 这里做 1.6 秒的内容级去重，正常的后续提醒不会受影响。
    const key = `${isError ? "1" : "0"}\u0001${title}\u0001${content}`;
    const now = Date.now();
    if (now - (recentToastKeys.get(key) || 0) < 1600) return;
    recentToastKeys.set(key, now);
    setTimeout(() => {
      if (recentToastKeys.get(key) === now) recentToastKeys.delete(key);
    }, 2000);
    const box = document.createElement("div");
    box.className = "toast" + (isError ? " error" : "");
    box.innerHTML = `<strong>${esc(title)}</strong><p>${esc(content || "")}</p>`;
    $("toasts").appendChild(box);
    setTimeout(() => {
      box.style.opacity = "0";
      box.style.transition = "opacity .25s";
      setTimeout(() => box.remove(), 260);
    }, 5200);
  }

  // ---------------------------------------------------------------- 渲染
  function renderAuth(state) {
    const auth = state.auth || {};
    const on = !!auth.logged_in;
    // 凭据已经落盘就按已登录渲染；login_open 只用于未登录时的等待提示。
    const opening = !!auth.login_open && !on;
    $("authDot").className = "dot" + (on ? " on" : auth.has_refresh ? " warn" : "");
    $("accountName").textContent = on
      ? (auth.display_name || auth.username || "已登录")
      : "未登录";
    $("accountMeta").textContent = opening
      ? "请在弹出的登录窗口中完成登录…"
      : on
        ? `登录态剩余 ${duration(auth.remaining)}`
        : auth.has_refresh
          ? "凭据待恢复，重新登录一次即可"
          : "点右侧按钮打开登录窗口";
    $("btnLogin").classList.toggle("hidden", on);
    $("btnLogout").classList.toggle("hidden", !on);
    $("btnLogin").disabled = opening;
    $("btnLogin").textContent = opening ? "登录中…" : "登录";

    // 进入应用时如果已经有登录态，自动扫一次；手动登录后也只触发一次。
    if (on && !autoScanStarted && !state.scanning) {
      autoScanStarted = true;
      setTimeout(async () => {
        const bridge = api();
        if (!bridge) return;
        const result = await bridge.scan();
        if (result && result.ok) tick();
      }, 700);
    }
  }

  function renderStatus(state) {
    running = !!state.running;
    const pill = $("runPill");
    pill.className = "pill" + (running ? " on" : "");
    $("runText").textContent = running ? "监控运行中" : "已停止";
    $("btnToggle").textContent = running ? "停止监控" : "开始监控";
    $("btnToggle").className = "btn " + (running ? "danger" : "primary");
    $("lastScan").textContent = state.scanning ? "正在扫描…" : clock(state.last_scan_at);
    const hidden = Number(state.hidden_count || 0);
    $("activityHint").textContent = state.initialized
      ? `${state.activities.length} 个活动${hidden ? ` · 已隐藏 ${hidden} 个` : ""} · 每 ${state.config.scan_interval} 秒扫描`
      : "首次扫描会建立基线，不会误报存量活动";
    const stats = state.stats || {};
    $("statUnreg").textContent = stats.unregistered ?? 0;
    $("statReg").textContent = stats.registered ?? 0;
    $("footStatus").textContent = state.scanning ? "扫描中…" : "";
  }

  function statusChip(activity) {
    if (activity.auditing) return { cls: "auditing", text: "审核中" };
    if (activity.registered) return { cls: "registered", text: "已报名" };
    if (activity.status === 0) return { cls: "", text: "审核中" };
    if (activity.status === 2) return { cls: "blocked", text: "报名失败" };
    if (activity.status === 3) return { cls: "", text: "已取消" };
    if (activity.past) return { cls: "past", text: "已截止" };
    if (activity.full) return { cls: "full", text: "已满额" };
    if (activity.status === 4) return { cls: "open", text: "未报名" };
    if (activity.blocked) return { cls: "blocked", text: "屏蔽词" };
    return { cls: "", text: "未知" };
  }

  function renderTable(state) {
    const body = $("activityBody");
    let rows = (state.activities || []).filter((a) => {
      // 开关打开时，已报名里不再显示活动时间已过、但状态还没标记结束的记录；审核中的始终保留。
      if (state.config.hide_past_registered && a.registered && !a.auditing && a.past_activity) {
        return false;
      }
      if (filter === "open") return a.status === 4 && !a.registered && !a.past;
      if (filter === "registered") return !!a.registered;
      return true;
    });

    if (!rows.length) {
      body.innerHTML = `<tr><td colspan="6" class="empty">${
        state.activities.length
          ? "当前筛选下没有活动"
          : Number(state.hidden_count || 0)
            ? "当前关键词设置下没有可显示的活动"
            : "还没有数据，先点「立即扫描」"
      }</td></tr>`;
      return;
    }

    const html = rows.map((a) => {
      const chip = statusChip(a);
      const tags = [];
      if (a.blocked) tags.push('<span class="tag danger">屏蔽词</span>');
      if (a.full && !a.registered && chip.cls !== "full") tags.push('<span class="tag">满额</span>');
      const timeText = esc(a.activity_start || "待公布");
      const remainingText = esc(a.remaining || "名额未知");
      const registerText = esc(a.register_start || "待公布");
      let action = "";
      if (a.registered) {
        if (a.register_id) {
          action = `<button class="btn ghost small" data-cancel="${esc(a.id)}" ${
            a.can_cancel ? "" : 'disabled title="平台未开放取消"'
          }>取消</button>`;
        }
      } else if (!a.past && a.status === 4) {
        action = `<button class="btn ghost small" data-register="${esc(a.id)}" ${
          a.full ? "disabled" : ""
        }>${a.full ? "满额" : "报名"}</button>`;
      }
      return `
        <tr>
          <td><span class="chip ${chip.cls}">${chip.text}</span></td>
          <td class="cell-name">
            <div class="title-cell"><span title="${esc(a.title)}">${esc(a.title)}</span>${tags.join("")}</div>
            <div class="row-meta">
              <span>活动时间 <b>${timeText}</b></span>
              <span>剩余名额 <b>${remainingText}</b></span>
              <span>报名时间 <b>${registerText}</b></span>
            </div>
          </td>
          <td class="col-time sub">${timeText}</td>
          <td class="col-quota sub">${remainingText}</td>
          <td class="col-reg sub">${registerText}</td>
          <td class="col-action">${action}</td>
        </tr>`;
    });
    body.innerHTML = html.join("");
  }

  function renderLogs(state) {
    const logs = state.logs || [];
    const signature = logs.length + "|" + (logs.length ? logs[logs.length - 1].time : 0);
    if (signature === lastLogSignature) return;
    lastLogSignature = signature;

    const box = $("log");
    const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    box.innerHTML = logs
      .map((l) => {
        const level = (l.level || "").toLowerCase();
        const cls = level === "error" ? " error" : level === "warning" ? " warn" : "";
        const time = new Date(l.time * 1000);
        return `<div class="log-line${cls}"><span class="log-time">${pad(time.getHours())}:${pad(
          time.getMinutes()
        )}:${pad(time.getSeconds())}</span>${esc(l.message)}</div>`;
      })
      .join("");
    if (nearBottom) box.scrollTop = box.scrollHeight;
  }

  function renderNotifications(state) {
    for (const notification of state.notifications || []) {
      const id = Number(notification.id);
      if (!Number.isFinite(id) || id <= notificationHighWater) continue;
      notificationHighWater = id;
      toast(notification.title, notification.content, false);
    }
    const nextCursor = Number(state.notifications_cursor);
    if (Number.isFinite(nextCursor)) {
      cursor = Math.max(cursor, nextCursor);
      notificationHighWater = Math.max(notificationHighWater, nextCursor);
    }
  }

  function clearLatestNotice() {
    if (latestNoticeTimer) {
      clearTimeout(latestNoticeTimer);
      latestNoticeTimer = null;
    }
    latestNoticeKey = "";
  }

  function renderUpdate(state) {
    const update = state.update || {};
    const text = $("updateState");
    const button = $("btnCheckUpdate");
    const badge = $("updateBadge");
    const version = $("aboutVersion");
    if (!text || !button) return;
    const current = displayVersion(update.current || state.app_version);
    if (version) version.textContent = current;
    if (badge) {
      badge.textContent = update.available ? `有新版本${displayVersion(update.latest)}` : "";
      badge.classList.toggle("hidden", !update.available);
    }
    button.disabled = !!update.checking;
    button.dataset.releaseUrl = update.available ? update.url || "" : "";
    button.title = update.available ? "打开 Release 页面" : "检查更新";

    if (!update.checking && !update.error && !update.available && update.latest) {
      const key = `${update.latest}|${update.checked_at || 0}`;
      if (key !== latestNoticeKey) {
        clearLatestNotice();
        latestNoticeKey = key;
        text.hidden = false;
        text.textContent = "当前已是最新版本";
        latestNoticeTimer = setTimeout(() => {
          latestNoticeTimer = null;
          text.hidden = true;
          text.textContent = "";
        }, 5000);
      } else if (!latestNoticeTimer) {
        text.hidden = true;
        text.textContent = "";
      } else {
        text.hidden = false;
        text.textContent = "当前已是最新版本";
      }
      return;
    }

    clearLatestNotice();
    text.hidden = false;
    if (update.checking) {
      text.textContent = "正在检查更新…";
    } else if (update.error) {
      text.textContent = `检查失败：${update.error}`;
    } else if (update.available) {
      // 新版本提示放在检查更新按钮左侧，正文区不再重复。
      text.textContent = "";
      text.hidden = true;
    } else {
      text.textContent = "尚未检查更新";
    }
  }

  function render(state) {
    lastState = state;
    renderAuth(state);
    renderStatus(state);
    renderTable(state);
    renderLogs(state);
    renderUpdate(state);
    renderNotifications(state);
  }

  // ---------------------------------------------------------------- 轮询
  // 后端在登录完成/扫描结束时会调用它，绕过窗口失焦造成的定时器暂停。
  function forceRefresh() {
    loginPending = false;
    // 正在请求时不能把 polling 直接置 false 再发一次：两个响应会拿到
    // 同一批通知，顶部就会弹两遍。只排队，等当前请求结束后再拉。
    if (polling) {
      refreshQueued = true;
      return;
    }
    tick();
  }
  window.__nsuRefresh = forceRefresh;

  async function tick() {
    const bridge = api();
    // 登录窗口打开期间也保持轮询：登录调用本身已经改成后台执行，
    // 这里不能再被 loginPending 卡住，否则登录完成后没有自动刷新。
    if (!bridge || polling) return;
    polling = true;
    try {
      // pywebview 的 JS API 偶发不 resolve。给每次请求加超时，
      // 否则 polling 会永远为 true，后面所有轮询都静默失效。
      const state = await Promise.race([
        bridge.get_state(cursor),
        new Promise((_, reject) =>
          setTimeout(() => reject(new Error("get_state timeout")), 6000)
        ),
      ]);
      render(state);
    } catch (err) {
      console.warn("get_state failed", err);
    } finally {
      polling = false;
      if (refreshQueued) {
        refreshQueued = false;
        tick();
      }
    }
  }

  // 锁住最外层页面，避免触控板双指滚动把整个窗口拖动；内容区照常滚动
  document.addEventListener("wheel", (event) => {
    if (event.target.closest(".content, .table-wrap, .log, .modal-body")) return;
    event.preventDefault();
  }, { passive: false });

  // ---------------------------------------------------------------- 弹层
  function openModal(id) {
    $(id).classList.remove("hidden");
    if (id === "settingsModal") {
      if (lastState) {
        fillSettings(lastState);
      } else {
        // 首屏还没拿到状态就打开设置时，先主动拉一次，避免设置页只剩空壳。
        tick().then(() => {
          if (lastState) fillSettings(lastState);
        });
      }
    }
  }

  function closeModal(id) {
    $(id).classList.add("hidden");
  }

  function splitTags(text) {
    return String(text ?? "")
      .split(/[|,，;；\s]+/)
      .map((t) => t.trim())
      .filter(Boolean);
  }

  function renderTagEditor(kind) {
    const list = $(kind + "Tags");
    if (!list) return;
    list.innerHTML = tagState[kind]
      .map(
        (tag, index) =>
          `<span class="kw-tag">${esc(tag)}<button type="button" data-remove-tag data-kind="${kind}" data-index="${index}" title="删除">×</button></span>`
      )
      .join("");
  }

  function addTags(kind, raw) {
    const incoming = splitTags(raw);
    if (!incoming.length) return;
    incoming.forEach((tag) => {
      if (!tagState[kind].includes(tag)) tagState[kind].push(tag);
    });
    const input = $(kind + "TagInput");
    if (input) input.value = "";
    renderTagEditor(kind);
  }

  function switchSettingsTab(name) {
    document.querySelectorAll("[data-settings-tab]").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.settingsTab === name);
    });
    document.querySelectorAll("[data-settings-pane]").forEach((pane) => {
      pane.classList.toggle("active", pane.dataset.settingsPane === name);
    });
    // 每次进入“关于”都重新展示 5 秒的“当前已是最新版本”，
    // 避免自动检查发生在设置页关闭时，用户打开时提示已经消失。
    if (name === "about" && lastState) {
      latestNoticeKey = "";
      renderUpdate(lastState);
    }
  }

  function fillSettings(state) {
    const cfg = state.config || {};
    switchSettingsTab("signup");
    $("setScanInterval").value = cfg.scan_interval ?? 300;
    $("setAutoRegister").checked = !!cfg.auto_register;
    $("setAutoMonitor").checked = !!cfg.auto_monitor;
    $("setCloseToTray").checked = cfg.close_to_tray !== false;
    $("setAutoCheckUpdate").checked = cfg.auto_check_update !== false;
    $("setHidePast").checked = cfg.hide_past_registered !== false;
    $("setUpcoming").value = cfg.upcoming_days ?? 3;
    tagState.blocklist = splitTags(cfg.blocklist);
    tagState.include = splitTags(cfg.keyword_include);
    tagState.exclude = splitTags(cfg.keyword_exclude);
    renderTagEditor("blocklist");
    renderTagEditor("include");
    renderTagEditor("exclude");
    ["blocklist", "include", "exclude"].forEach((kind) => {
      const input = $(kind + "TagInput");
      if (input) input.value = "";
    });
    $("setWebhook").value = cfg.webhook_url ?? "";
    $("setAutoRelogin").checked = cfg.auto_relogin !== false;
    document.querySelectorAll("[data-notify]").forEach((el) => {
      el.checked = (cfg.notify_providers || []).includes(el.value);
    });
  }

  // 保存前把输入框里还没回车/点“添加”的文本转成标签，避免只存成输入框文本。
  function flushPendingTags() {
    ["blocklist", "include", "exclude"].forEach((kind) => {
      const input = $(kind + "TagInput");
      if (input && input.value.trim()) addTags(kind, input.value);
    });
  }

  // ---------------------------------------------------------------- 事件
  function bind() {
    $("btnToggle").addEventListener("click", async () => {
      const bridge = api();
      if (!bridge) return;
      if (running) await bridge.stop();
      else await bridge.start();
      tick();
    });

    $("btnScan").addEventListener("click", async () => {
      const bridge = api();
      if (!bridge) return;
      const result = await bridge.scan();
      toast("扫描", (result && result.message) || "已开始扫描", !(result && result.ok));
      setTimeout(tick, 600);
    });

    $("btnLogin").addEventListener("click", openLoginWindow);
    $("btnLogout").addEventListener("click", async () => {
      if (!confirm("退出登录会清除本地凭据，确定吗？")) return;
      const bridge = api();
      autoScanStarted = false;
      if (bridge) await bridge.logout();
      tick();
    });

    $("btnSettings").addEventListener("click", () => openModal("settingsModal"));

    document.querySelectorAll("[data-settings-tab]").forEach((btn) => {
      btn.addEventListener("click", () => switchSettingsTab(btn.dataset.settingsTab));
    });

    document.querySelectorAll("[data-add-tag]").forEach((btn) => {
      btn.addEventListener("click", () => {
        addTags(btn.dataset.addTag, $(btn.dataset.addTag + "TagInput").value);
      });
    });

    document.querySelectorAll("[data-tag-input]").forEach((input) => {
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === "," || e.key === "，") {
          e.preventDefault();
          addTags(input.dataset.tagInput, input.value);
        }
      });
    });

    $("settingsModal").addEventListener("click", (e) => {
      const remove = e.target.closest("[data-remove-tag]");
      if (!remove) return;
      const kind = remove.dataset.kind;
      const index = Number(remove.dataset.index);
      if (tagState[kind] && index >= 0) {
        tagState[kind].splice(index, 1);
        renderTagEditor(kind);
      }
    });

    document.querySelectorAll("[data-close]").forEach((btn) => {
      btn.addEventListener("click", () => closeModal(btn.dataset.close));
    });

    document.querySelectorAll(".modal").forEach((modal) => {
      modal.addEventListener("click", (e) => {
        // 设置页不通过点击遮罩关闭：只允许右上角关闭或 Esc，
        // 避免编辑标签/开关时误触空白直接丢失操作。
        if (e.target === modal && modal.id !== "settingsModal") {
          closeModal(modal.id);
        }
      });
    });

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        document.querySelectorAll(".modal:not(.hidden)").forEach((m) => closeModal(m.id));
      }
    });

    // 主窗口从登录子窗口切回来后，定时器可能被系统暂停；聚焦即补一次。
    window.addEventListener("focus", () => tick());
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) tick();
    });

    $("tabs").addEventListener("click", (e) => {
      const tab = e.target.closest(".tab");
      if (!tab) return;
      document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
      filter = tab.dataset.filter;
      if (lastState) renderTable(lastState);
    });

    $("activityBody").addEventListener("click", async (e) => {
      const registerBtn = e.target.closest("[data-register]");
      if (registerBtn) {
        const id = registerBtn.dataset.register;
        if (!confirm("确认以当前账号报名这个活动？")) return;
        registerBtn.disabled = true;
        const bridge = api();
        const result = await bridge.register(id);
        toast("报名", (result && result.message) || "已提交", !(result && result.ok));
        return;
      }
      const cancelBtn = e.target.closest("[data-cancel]");
      if (cancelBtn) {
        if (cancelBtn.disabled) return;
        const id = cancelBtn.dataset.cancel;
        if (!confirm("确认取消这个活动的报名？")) return;
        cancelBtn.disabled = true;
        const bridge = api();
        const result = await bridge.cancel(id);
        toast("取消报名", (result && result.message) || "已提交", !(result && result.ok));
        setTimeout(tick, 500);
      }
    });

    $("btnClearLog").addEventListener("click", async () => {
      const bridge = api();
      if (bridge) await bridge.clear_logs();
      lastLogSignature = "";
      tick();
    });

    $("btnTestNotify").addEventListener("click", async () => {
      const bridge = api();
      if (!bridge) return;
      const result = await bridge.test_notify(
        checkedNotifyProviders(),
        $("setWebhook").value
      );
      toast("通知测试", (result && result.message) || "", !(result && result.ok));
    });

    $("btnResetSettings").addEventListener("click", async () => {
      if (!confirm("恢复默认设置会清除当前所有设置，登录状态不会退出。确定吗？")) return;
      const bridge = api();
      if (!bridge) return;
      const result = await bridge.reset_settings();
      toast("设置", (result && result.message) || "已恢复默认设置", !(result && result.ok));
      if (result && result.ok) {
        closeModal("settingsModal");
        await tick();
      }
    });

    $("btnCheckUpdate").addEventListener("click", async () => {
      const button = $("btnCheckUpdate");
      const releaseUrl = button.dataset.releaseUrl || "";
      if (releaseUrl) {
        const bridge = api();
        if (bridge && bridge.open_url) {
          await bridge.open_url(releaseUrl);
        } else {
          window.open(releaseUrl, "_blank", "noopener");
        }
        return;
      }
      const bridge = api();
      if (!bridge) return;
      const result = await bridge.check_update(true);
      toast("检查更新", (result && result.message) || "正在检查更新…", !(result && result.ok));
      setTimeout(tick, 700);
    });

    $("btnSaveSettings").addEventListener("click", async () => {
      flushPendingTags();
      const providers = checkedNotifyProviders();
      const patch = {
        SCAN_INTERVAL_SECONDS: Number($("setScanInterval").value) || 300,
        AUTO_REGISTER: $("setAutoRegister").checked,
        AUTO_MONITOR: $("setAutoMonitor").checked,
        CLOSE_TO_TRAY: $("setCloseToTray").checked,
        AUTO_CHECK_UPDATE: $("setAutoCheckUpdate").checked,
        HIDE_PAST_REGISTERED: $("setHidePast").checked,
        UPCOMING_DAYS: Number($("setUpcoming").value) || 0,
        REGISTER_BLOCKLIST: tagState.blocklist.join("|"),
        KEYWORD_INCLUDE: tagState.include.join("|"),
        KEYWORD_EXCLUDE: tagState.exclude.join("|"),
        NOTIFY_PROVIDERS: providers,
        WEBHOOK_URL: $("setWebhook").value,
        AUTO_RELOGIN: $("setAutoRelogin").checked,
      };
      const bridge = api();
      const result = await bridge.save_settings(patch);
      toast("设置", (result && result.message) || "已保存", !(result && result.ok));
      if (result && result.ok) {
        closeModal("settingsModal");
        tick();
      }
    });

    async function openLoginWindow() {
      const bridge = api();
      if (!bridge || loginPending) return;
      loginPending = true;
      $("btnLogin").disabled = true;
      $("btnLogin").textContent = "登录中…";
      $("accountMeta").textContent = "请在弹出的登录窗口中完成登录…";
      // pywebview 偶发不 resolve 这个 Promise；不能让 loginPending 永远卡住。
      const guard = setTimeout(() => {
        loginPending = false;
        polling = false;
        tick();
      }, 2500);
      try {
        const result = await bridge.login();
        toast("登录", (result && result.message) || "", !(result && result.ok));
      } catch (err) {
        toast("登录", `登录窗口异常：${err}`, true);
      } finally {
        clearTimeout(guard);
        loginPending = false;
        polling = false;
        tick();
      }
    }
  }

  // ---------------------------------------------------------------- 启动
  let bootStarted = false;
  function boot() {
    // pywebviewready 可能在 DOM 解析完成前触发；此时先不绑定事件，
    // 等 DOMContentLoaded / 后面的补启动再执行，避免漏掉设置等按钮。
    if (document.readyState === "loading") return;
    if (!bootStarted) {
      bootStarted = true;
      try {
        bind();
      } catch (err) {
        // 单个 DOM 绑定失败不能阻断首次状态加载。
        console.error("界面绑定失败", err);
      }
      // 显式走 window，且输入框已改名，避免 DOM 命名属性遮蔽内置 setInterval。
      window.setInterval(tick, 1200);
    }
    tick();
    if (!api()) {
      setTimeout(() => {
        if (!api()) {
          $("accountMeta").textContent = window.pywebview
            ? "正在等待桌面接口就绪…"
            : "请在桌面应用中打开（当前是浏览器预览）";
        }
      }, 1500);
      // 首次可能在 WebView API 注入前执行；除了周期轮询再补一次。
      setTimeout(tick, 300);
    }
  }
  window.__nsuBoot = boot;

  // 不再只押注一个 pywebviewready 事件：某些 WebView 会在脚本加载前就派发它。
  // boot 是幂等的，多个入口重复调用只会多触发一次 tick，不会重复绑定事件。
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }
  window.addEventListener("pywebviewready", boot, { once: true });
  window.addEventListener("load", boot, { once: true });
  setTimeout(boot, 400);
  setTimeout(boot, 1600);
  setTimeout(boot, 3200);
})();
