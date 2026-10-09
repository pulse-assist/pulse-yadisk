// Страница «Яндекс Диск»: аккаунты (вход по коду на oauth.yandex.ru/device) и подключения — папки @yadisk/… в
// «Файлах». Всё — через мост Пульса: pulse.plugin(...) к серверу плагина, pulse.navigate(...) — в разделы Пульса.
// В песочнице нет alert/confirm — подтверждения прямо на странице.

const app = document.getElementById("app");
const dialog = document.getElementById("dialog");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const gb = (bytes) => {
  if (bytes == null) return "";
  const g = bytes / 1024 ** 3;
  return g >= 100 ? `${Math.round(g).toLocaleString("ru")} ГБ` : `${g.toFixed(1).replace(".", ",")} ГБ`;
};
const filesLink = (path) => `/files?path=${encodeURIComponent(path)}`;

let data = null;
let error = "";
let poll = null;
let openMenu = null;      // имя подключения с открытым меню «⋯»
let renaming = null;      // имя подключения, которое переименовывают
let confirm = null;       // {kind: "logout"|"unmount", id}

async function load() {
  try {
    data = await pulse.plugin("/state");
    error = "";
  } catch (e) {
    if (!data) { app.innerHTML = `<div class="pl-error">Плагин не отвечает: ${esc(e.message)}</div>`; return; }
    error = e.message;
  }
  render();
  clearTimeout(poll);
  if (data.login?.status === "pending") poll = setTimeout(load, 3000);
}

async function act(fn) {
  try {
    await fn();
    error = "";
  } catch (e) {
    error = e.message;
  }
  openMenu = renaming = confirm = null;
  await load();
}

// --- отрисовка ----------------------------------------------------------------------------------------------

function render() {
  const { accounts, mounts, app: oauth } = data;
  app.innerHTML = `
    <div class="status">
      <span>${accounts.length ? `${accounts.length} ${plural(accounts.length, "аккаунт", "аккаунта", "аккаунтов")}` : "аккаунтов нет"}
        · ${mounts.length} ${plural(mounts.length, "подключение", "подключения", "подключений")}</span>
      <span class="grow"></span>
      <button class="btn" data-go="${esc(filesLink("@yadisk"))}">Открыть @yadisk в «Файлах»</button>
    </div>
    ${!oauth.configured ? `<div class="pl-error">Вход в аккаунты не настроен: нужны ClientID и Client secret приложения
      Яндекс OAuth — их выдаёт администратор вашей установки Пульса. Укажите их в
      <button class="btn-link" data-go="/settings/plugins/yadisk">настройках плагина</button>. Публичные ссылки работают и без этого.</div>` : ""}
    ${error ? `<div class="pl-error">${esc(error)}</div>` : ""}
    <div class="cols">
      <section class="box">
        <div class="box-head"><h2>Аккаунты</h2><span class="hint">вход через браузер, плагин продлевает его сам</span></div>
        ${accounts.map(renderAccount).join("")}
        ${renderLogin()}
      </section>
      <section class="box">
        <div class="box-head"><h2>Подключения</h2><span class="hint">папки в «Файлах» → @yadisk/…, их же видят агенты</span>
          <span class="grow"></span>
          <button class="btn" data-add="public">+ Публичная ссылка</button>
          <button class="btn btn-primary" data-add="account" ${accounts.length ? "" : "disabled title=\"Сначала войдите в аккаунт\""}>+ Папка аккаунта</button></div>
        ${mounts.length ? `<table class="mounts"><thead><tr><th>Папка в «Файлах»</th><th>Откуда</th><th>Доступ</th><th></th></tr></thead>
          <tbody>${mounts.map(renderMount).join("")}</tbody></table>`
          : `<div class="pl-empty">Подключений нет. Войдите в аккаунт — весь его диск подключится сам, или добавьте публичную ссылку.</div>`}
        <div class="box-foot hint">⋯ — переименовать папку, сохранить публичную ссылку к себе на диск, отключить (файлы на Диске не трогаются).</div>
      </section>
    </div>`;
}

function plural(n, one, few, many) {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
  return many;
}

function renderAccount(a) {
  const share = a.total_space ? Math.min(100, Math.round((a.used_space / a.total_space) * 100)) : 0;
  const asking = confirm?.kind === "logout" && confirm.id === a.uid;
  return `<div class="account">
    <span class="avatar">${esc((a.name || a.login || "?").slice(0, 1).toUpperCase())}</span>
    <div class="account-body">
      <div class="account-name">${esc(a.name || a.login)} <span>· ${esc(a.login)}</span>
        ${a.expired ? `<span class="badge badge-warn">вход истёк — войдите снова</span>` : ""}</div>
      ${a.total_space ? `<div class="space"><div class="space-bar"><div style="width:${share}%"></div></div>
        <span>${gb(a.used_space)} из ${gb(a.total_space)}</span></div>` : a.error ? `<div class="hint">${esc(a.error)}</div>` : ""}
    </div>
    ${asking
      ? `<span class="inline-form"><span class="hint">Выйти и убрать подключения?</span>
          <button class="btn btn-sm btn-danger" data-logout-yes="${esc(a.uid)}">Выйти</button>
          <button class="btn btn-sm" data-cancel>Нет</button></span>`
      : `<button class="btn btn-sm" data-logout="${esc(a.uid)}">Выйти</button>`}
  </div>`;
}

function renderLogin() {
  const login = data.login;
  if (!data.app.configured) return "";
  if (login?.status === "pending") {
    const left = Math.max(0, Math.round(login.expires_at - Date.now() / 1000));
    return `<div class="login">
      <h3>Вход в ${data.accounts.length ? "ещё один " : ""}аккаунт Яндекса</h3>
      <p>Откройте страницу Яндекса, войдите в нужный аккаунт и введите код. Пароль вы вводите на сайте Яндекса — плагин его не видит.</p>
      <div class="code-row"><span class="code" id="code">${esc(formatCode(login.user_code))}</span>
        <button class="btn" data-copy="${esc(login.user_code)}">Копировать</button></div>
      <div class="code-row">
        <a class="btn btn-primary" style="display:inline-flex;align-items:center;text-decoration:none" href="${esc(login.verification_url)}" target="_blank" rel="noopener">Открыть ${esc(login.verification_url.replace(/^https?:\/\//, ""))} ↗</a>
        <span class="waiting">ждём подтверждения · код действует ещё ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}</span>
        <span style="flex-grow:1"></span><button class="btn-link" data-login-cancel>Отмена</button>
      </div>
    </div>`;
  }
  const done = login?.status === "done" ? `<div class="hint">Вход выполнен: ${esc(login.account)}.</div>` : "";
  const failed = login && ["error", "expired"].includes(login.status) ? `<div class="error">${esc(login.error)}</div>` : "";
  return `<div class="login-start">${failed}${done}
    <button class="btn ${data.accounts.length ? "" : "btn-primary"}" data-login>+ Войти в аккаунт Яндекса</button></div>`;
}

function formatCode(code) {
  const s = String(code || "");
  return s.length > 4 ? `${s.slice(0, 4)} ${s.slice(4)}` : s;
}

function renderMount(m) {
  const source = m.kind === "public"
    ? `публичная ссылка<div class="sub mono">${esc(m.url.replace(/^https?:\/\//, ""))}</div>`
    : m.account
      ? `${m.path === "/" ? "весь диск" : `папка «${esc(m.path)}»`} · ${esc(m.account.login)}
         ${m.account.expired ? `<div class="sub"><span class="badge badge-warn">вход истёк</span></div>` : ""}`
      : `<span class="badge badge-warn">аккаунт отключён</span>`;
  const name = renaming === m.name
    ? `<form class="inline-form" data-rename-form="${esc(m.name)}"><input name="to" value="${esc(m.name)}" aria-label="Новое имя">
        <button class="btn btn-sm btn-primary">Сохранить</button><button type="button" class="btn btn-sm" data-cancel>Отмена</button></form>`
    : `<b>${esc(m.name)}</b> ${m.kind === "public" ? `<span class="badge">ссылка</span>` : ""}<div class="sub mono">${esc(m.files_path)}</div>`;
  const asking = confirm?.kind === "unmount" && confirm.id === m.name;
  const actions = asking
    ? `<span class="inline-form"><span class="hint">Отключить?</span>
        <button class="btn btn-sm btn-danger" data-unmount-yes="${esc(m.name)}">Отключить</button><button class="btn btn-sm" data-cancel>Нет</button></span>`
    : `<button class="btn-link" data-go="${esc(filesLink(m.files_path))}">Открыть</button>
       <span class="menu"><button class="btn btn-sm" data-menu="${esc(m.name)}" aria-label="Действия: ${esc(m.name)}">⋯</button>
       ${openMenu === m.name ? `<div class="menu-list">
          <button data-rename="${esc(m.name)}">Переименовать</button>
          ${m.kind === "public" && data.accounts.length ? `<button data-save="${esc(m.name)}">Сохранить к себе на диск</button>` : ""}
          <button class="btn-danger" data-unmount="${esc(m.name)}">Отключить</button></div>` : ""}</span>`;
  return `<tr><td class="name">${name}</td><td>${source}</td>
    <td>${m.readonly ? `<span class="badge badge-ro">только чтение</span>` : "чтение и запись"}</td>
    <td class="actions">${actions}</td></tr>`;
}

// --- действия ------------------------------------------------------------------------------------------------

app.addEventListener("click", async (e) => {
  const t = e.target.closest("button, a");
  if (!t) { if (openMenu) { openMenu = null; render(); } return; }
  const d = t.dataset;
  if (d.go) pulse.navigate(d.go);
  else if (d.login !== undefined) await act(() => pulse.plugin("/login", { method: "POST" }));
  else if (d.loginCancel !== undefined) await act(() => pulse.plugin("/login", { method: "DELETE" }));
  else if (d.copy) copy(d.copy);
  else if (d.logout) { confirm = { kind: "logout", id: d.logout }; render(); }
  else if (d.logoutYes) await act(() => pulse.plugin(`/accounts/${encodeURIComponent(d.logoutYes)}`, { method: "DELETE" }));
  else if (d.unmount) { confirm = { kind: "unmount", id: d.unmount }; openMenu = null; render(); }
  else if (d.unmountYes) await act(() => pulse.plugin(`/mounts/${encodeURIComponent(d.unmountYes)}`, { method: "DELETE" }));
  else if (d.menu) { openMenu = openMenu === d.menu ? null : d.menu; render(); }
  else if (d.rename) { renaming = d.rename; openMenu = null; render(); app.querySelector("[data-rename-form] input")?.select(); }
  else if (d.save) await act(() => pulse.plugin(`/mounts/${encodeURIComponent(d.save)}/save`, { method: "POST", body: {} }));
  else if (d.cancel !== undefined) { confirm = renaming = null; render(); }
  else if (d.add) openDialog(d.add);
});

app.addEventListener("submit", async (e) => {
  const form = e.target.closest("[data-rename-form]");
  if (!form) return;
  e.preventDefault();
  const to = form.elements.to.value.trim();
  await act(() => pulse.plugin(`/mounts/${encodeURIComponent(form.dataset.renameForm)}`, { method: "PATCH", body: { name: to } }));
});

async function copy(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const range = document.createRange();      // во фрейме-песочнице буфер может быть закрыт — выделяем код
    range.selectNodeContents(document.getElementById("code"));
    getSelection().removeAllRanges();
    getSelection().addRange(range);
  }
}

// --- окно «Подключить папку» --------------------------------------------------------------------------------

const form = { tab: "account", account: "", path: "/", folders: [], name: "", url: "", preview: null, error: "", busy: false };

function openDialog(tab) {
  Object.assign(form, { tab, account: data.accounts[0]?.uid || "", path: "/", folders: [], name: "", url: "",
                        preview: null, error: "", busy: false });
  renderDialog();
  if (tab === "account") browse("/");
}

function closeDialog() { dialog.innerHTML = ""; }

async function browse(path) {
  form.path = path;
  form.name = "";
  form.error = "";
  form.folders = null;
  renderDialog();
  try {
    const res = await pulse.plugin("/browse", { params: { account: form.account, path } });
    form.folders = res.folders;
  } catch (e) {
    form.folders = [];
    form.error = e.message;
  }
  renderDialog();
}

async function checkUrl() {
  form.preview = null;
  form.error = "";
  if (!form.url.trim()) { renderDialog(); return; }
  try {
    form.preview = await pulse.plugin("/public", { params: { url: form.url.trim() } });
  } catch (e) {
    form.error = e.message;
  }
  renderDialog();
}

function renderDialog() {
  const account = data.accounts.find((a) => a.uid === form.account);
  const crumbs = ["/", ...form.path.split("/").filter(Boolean).map((_, i, parts) => "/" + parts.slice(0, i + 1).join("/"))];
  const defaultName = form.tab === "account"
    ? (form.path === "/" ? (account?.name || account?.login || "") : form.path.split("/").pop())
    : (form.preview?.name || "");
  const body = form.tab === "account" ? `
      <label class="field">Аккаунт<select id="f-account">${data.accounts.map((a) =>
        `<option value="${esc(a.uid)}" ${a.uid === form.account ? "selected" : ""}>${esc(a.name || a.login)} · ${esc(a.login)}</option>`).join("")}</select></label>
      <div class="field">Папка
        <div class="browser">
          <div class="crumbs">${crumbs.map((p) => `<button data-crumb="${esc(p)}">${p === "/" ? "Диск" : esc(p.split("/").pop())}</button>`).join(" / ")}</div>
          <div class="folder-list">${form.folders === null ? `<div class="pl-empty">Загружаю…</div>`
            : form.folders.length ? form.folders.map((f) => `<button class="folder" data-folder="${esc(f.path)}">${esc(f.name)}</button>`).join("")
            : `<div class="pl-empty">Вложенных папок нет</div>`}</div>
        </div>
        <span class="hint">Будет подключена папка <b>${form.path === "/" ? "весь диск" : esc(form.path)}</b>. Общие папки, которые
          вам открыли, видны здесь после того, как вы приняли приглашение в Яндекс Диске.</span></div>`
    : `
      <label class="field">Ссылка на Яндекс Диск<input id="f-url" value="${esc(form.url)}" placeholder="https://disk.yandex.ru/d/…" autocomplete="off"></label>
      ${form.preview ? `<div class="preview"><b>${esc(form.preview.name)}</b><span class="hint">${form.preview.type === "dir"
        ? `папка${form.preview.items != null ? ` · ${form.preview.items} в корне` : ""}` : `файл · ${gb(form.preview.size)}`}</span></div>` : ""}
      <span class="hint">Публичная ссылка — только чтение и без входа в аккаунт. Чтобы править файлы, сохраните её к себе
        на диск (⋯ → «Сохранить к себе»). Ссылки с паролем пока не поддерживаются.</span>`;
  dialog.innerHTML = `<div class="overlay" data-close-overlay><div class="modal" role="dialog" aria-label="Подключить папку">
    <div class="modal-head"><h2>Подключить папку</h2><button class="btn-link" data-close aria-label="Закрыть">✕</button></div>
    <div class="tabs"><button class="${form.tab === "account" ? "on" : ""}" data-tab="account" ${data.accounts.length ? "" : "disabled"}>Папка аккаунта</button>
      <button class="${form.tab === "public" ? "on" : ""}" data-tab="public">Публичная ссылка</button></div>
    <div class="modal-body">${body}
      <label class="field">Имя папки в «Файлах»<input id="f-name" value="${esc(form.name)}" placeholder="${esc(defaultName)}" autocomplete="off">
        <span class="hint mono">@yadisk/${esc(form.name || defaultName)}</span></label>
      ${form.error ? `<div class="error">${esc(form.error)}</div>` : ""}
    </div>
    <div class="modal-foot"><button class="btn" data-close>Отмена</button>
      <button class="btn btn-primary" data-submit ${form.busy || (form.tab === "public" && !form.preview) ? "disabled" : ""}>Подключить</button></div>
  </div></div>`;
}

dialog.addEventListener("click", async (e) => {
  if (e.target.dataset.closeOverlay !== undefined) { closeDialog(); return; }
  const t = e.target.closest("button");
  if (!t) return;
  const d = t.dataset;
  if (d.close !== undefined) closeDialog();
  else if (d.tab) { form.tab = d.tab; form.error = ""; renderDialog(); if (d.tab === "account" && !form.folders.length) browse(form.path); }
  else if (d.crumb) browse(d.crumb);
  else if (d.folder) browse(d.folder);
  else if (d.submit !== undefined) {
    form.busy = true;
    renderDialog();
    const body = form.tab === "account"
      ? { kind: "account", account: form.account, path: form.path, name: form.name || null }
      : { kind: "public", url: form.url.trim(), name: form.name || null };
    try {
      await pulse.plugin("/mounts", { method: "POST", body });
      closeDialog();
      await load();
    } catch (err) {
      form.busy = false;
      form.error = err.message;
      renderDialog();
    }
  }
});

dialog.addEventListener("input", (e) => {
  if (e.target.id === "f-name") {
    form.name = e.target.value;
    const hint = e.target.parentElement.querySelector(".hint");
    if (hint) hint.textContent = `@yadisk/${form.name || e.target.placeholder}`;
  } else if (e.target.id === "f-url") form.url = e.target.value;
});

dialog.addEventListener("change", (e) => {
  if (e.target.id === "f-account") { form.account = e.target.value; browse("/"); }
  else if (e.target.id === "f-url") checkUrl();
});

load();
