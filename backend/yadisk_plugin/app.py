"""Плагин «Яндекс Диск»: аккаунты через вход в браузере, подключения (папки аккаунта, общие папки, публичные
ссылки) и хранилище @yadisk для «Файлов» и агентов.

Страница (pulse.plugin): GET /state, POST|DELETE /login, DELETE /accounts/{uid}, GET /browse, GET /public,
POST /mounts, PATCH|DELETE /mounts/{name}, POST /mounts/{name}/save.
Агенты: pulse yadisk accounts|login|login status|logout|mounts|browse|mount folder|mount public|rename|unmount|
save|publish. Хранилище для ядра — /fs/… (pulse_plugin.fs).
"""

from fastapi import Body, HTTPException, Query
from pulse_plugin import CommandError, Plugin
from pulse_plugin.fs import FsError

from . import net
from .disk import API, AuthExpired
from .oauth import OAUTH, app_from
from .service import UserError, Yadisk, YadiskFiles, join
from .store import Store

plugin = Plugin()
app = plugin.app
# прокси в РФ (настройки proxy и proxy_all) — если сервер Пульса не в России и oauth.yandex.ru не отвечает
http = net.build_http(plugin.settings, plugin.secrets)


def expired_signal(account: dict) -> None:
    """Продлить вход не удалось — один сигнал на аккаунт, пока владелец не войдёт снова."""
    try:
        plugin.signal(f"Войдите в Яндекс снова: {account['login']}",
                      body=f"Вход в аккаунт Яндекса {account['login']} истёк — его подключения в @yadisk не открываются. "
                           "Откройте [Яндекс Диск](/p/yadisk/main) и войдите ещё раз.", priority=70, tags=["яндекс-диск"])
    except Exception:  # noqa: BLE001 — сигнал не обязателен: страница всё равно покажет, что вход истёк
        pass


yadisk = Yadisk(Store(plugin.data_dir), http, lambda: app_from(plugin.settings, plugin.secrets), expired_signal)
files = YadiskFiles(yadisk)
plugin.provide_files(files)


def app_info() -> dict:
    return {"configured": yadisk.app() is not None}


def page_error(exc: Exception) -> HTTPException:
    if isinstance(exc, UserError):
        return HTTPException(400, str(exc))
    if isinstance(exc, FsError):
        return HTTPException(exc.status if exc.status < 500 else 502, str(exc))
    return HTTPException(502, str(exc))


def run(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except (UserError, FsError) as exc:
        raise page_error(exc) from exc


# --- страница -------------------------------------------------------------------------------------------

@app.get("/state")
def state() -> dict:
    return {"app": app_info(), "accounts": yadisk.accounts(space=True), "mounts": yadisk.mounts(),
            "login": yadisk.login_state()}


@app.get("/login")
def login_status() -> dict | None:
    """Только состояние входа — страница опрашивает его, пока владелец вводит код (без запросов к Диску)."""
    return yadisk.login_state()


@app.post("/login")
def login() -> dict:
    return run(yadisk.start_login)


@app.delete("/login")
def cancel_login() -> dict:
    yadisk.cancel_login()
    return {"login": None}


@app.delete("/accounts/{uid}")
def logout(uid: str) -> dict:
    return run(yadisk.logout, uid)


@app.get("/browse")
def browse(account: str = Query(...), path: str = Query("/")) -> dict:
    return run(yadisk.browse, account, path)


@app.get("/public")
def public_preview(url: str = Query(...)) -> dict:
    return run(yadisk.preview_public, url)


@app.post("/mounts")
def add_mount(body: dict = Body(...)) -> dict:
    if body.get("kind") == "public":
        return run(yadisk.add_public, body.get("url") or "", body.get("name") or None)
    return run(yadisk.add_folder, body.get("account") or "", body.get("path") or "/", body.get("name") or None)


@app.patch("/mounts/{name}")
def rename_mount(name: str, body: dict = Body(...)) -> dict:
    return run(yadisk.rename, name, body.get("name") or "")


@app.delete("/mounts/{name}")
def remove_mount(name: str) -> dict:
    return run(yadisk.unmount, name)


@app.post("/mounts/{name}/save")
def save_mount(name: str, body: dict = Body(default={})) -> dict:
    return run(yadisk.save_public, name, (body or {}).get("account") or "")


# --- команды агентов --------------------------------------------------------------------------------------

def command(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except (UserError, FsError) as exc:
        raise CommandError(str(exc)) from exc


def mount_path(value: str) -> str:
    """«@yadisk/Работа/отчёт.pdf» или «Работа/отчёт.pdf» → путь в хранилище."""
    value = (value or "").strip().strip("/")
    return value[len("@yadisk/"):] if value.startswith("@yadisk/") else value


@plugin.command("accounts", "Аккаунты Яндекса: логин, место на диске, не истёк ли вход")
def accounts_command(args: dict) -> dict:
    return {"app": app_info(), "accounts": yadisk.accounts(space=True)}


@plugin.command("login", "Начать вход в аккаунт Яндекса: код и ссылка для владельца")
def login_command(args: dict) -> dict:
    state = command(yadisk.start_login)
    return {**state, "tell_owner": f"Откройте {state['verification_url']}, войдите в нужный аккаунт Яндекса и введите "
                                   f"код {state['user_code']} — или нажмите «Войти» на странице [Яндекс Диск](/p/yadisk/main)."}


@plugin.command("login status", "Чем закончился вход: pending, done, error, expired")
def login_status_command(args: dict) -> dict:
    return yadisk.login_state() or {"status": "none"}


@plugin.command("logout", "Выйти из аккаунта и убрать его подключения")
def logout_command(args: dict) -> dict:
    return command(yadisk.logout, args.get("account") or "")


@plugin.command("mounts", "Подключения: папки в @yadisk и откуда они")
def mounts_command(args: dict) -> dict:
    return {"mounts": yadisk.mounts()}


@plugin.command("browse", "Папки аккаунта (в т.ч. общие) — чтобы выбрать, что подключить")
def browse_command(args: dict) -> dict:
    return command(yadisk.browse, args.get("account") or "", args.get("path") or "/")


@plugin.command("mount folder", "Подключить весь диск или папку аккаунта (общие папки — тоже)")
def mount_folder_command(args: dict) -> dict:
    return command(yadisk.add_folder, args.get("account") or "", args.get("path") or "/", args.get("name") or None)


@plugin.command("mount public", "Подключить публичную ссылку (только чтение)")
def mount_public_command(args: dict) -> dict:
    if not args.get("url"):
        raise CommandError("нужен --url https://disk.yandex.ru/d/…")
    return command(yadisk.add_public, args["url"], args.get("name") or None)


@plugin.command("rename", "Переименовать подключение")
def rename_command(args: dict) -> dict:
    if not args.get("name") or not args.get("to"):
        raise CommandError("нужны --name и --to")
    return command(yadisk.rename, args["name"], args["to"])


@plugin.command("unmount", "Отключить подключение (файлы на Диске не трогаются)")
def unmount_command(args: dict) -> dict:
    if not args.get("name"):
        raise CommandError("нужен --name")
    return command(yadisk.unmount, args["name"])


@plugin.command("save", "Сохранить публичную ссылку к себе на диск (в «Загрузки»)")
def save_command(args: dict) -> dict:
    if not args.get("name"):
        raise CommandError("нужен --name подключения-ссылки")
    return command(yadisk.save_public, args["name"], args.get("account") or "")


def connection_check() -> dict:
    """По шагам: приложение OAuth, прокси, сеть до входа и до Диска, аккаунты. Кнопка «Проверить подключение»."""
    checks = []

    def add(title: str, ok: bool, detail: str | None = None) -> None:
        checks.append({"title": title, "ok": ok, "detail": detail})

    configured = yadisk.app() is not None
    add("Приложение Яндекс OAuth", configured,
        "ClientID и Client secret заданы" if configured else "не заданы ClientID и Client secret — без них работают только публичные ссылки")
    try:
        proxy = net.proxy_of(plugin.settings, plugin.secrets)
        if proxy and proxy.startswith("socks"):
            import socksio  # noqa: F401 — httpx нужен пакет socksio для socks5
        if proxy:
            add("Прокси", True, f"{net.proxy_label(proxy)} — " + ("все запросы к Яндексу" if plugin.setting("proxy_all")
                                                                 else "только вход (oauth.yandex.ru)"))
    except net.ProxyError as exc:
        add("Прокси", False, str(exc))
    except ImportError:
        add("Прокси", False, "для socks5 нужен пакет socksio — переустановите плагин или используйте http-прокси")
    hint = "" if plugin.secret("proxy") else " Если сервер Пульса не в России, задайте прокси в РФ в настройках плагина."
    ok, detail = net.reach(yadisk.http, f"{OAUTH}/")
    add("Вход: oauth.yandex.ru", ok, detail + ("" if ok else "." + hint))
    ok, detail = net.reach(yadisk.http, f"{API}/")
    add("Диск: cloud-api.yandex.net", ok, detail + ("" if ok else ". Включите «Все запросы к Яндексу через прокси»."
                                                   if plugin.secret("proxy") else "." + hint))
    for account in yadisk.accounts():
        if account["expired"]:
            add(f"Аккаунт {account['login']}", False, "вход истёк — войдите снова на странице плагина")
            continue
        try:
            yadisk.disk(account["uid"]).info()
            add(f"Аккаунт {account['login']}", True, "вход действует, Диск отвечает")
        except AuthExpired as exc:
            add(f"Аккаунт {account['login']}", False, str(exc))
        except FsError as exc:
            add(f"Аккаунт {account['login']}", False, str(exc))
    return {"ok": all(c["ok"] for c in checks), "checks": checks}


@app.get("/check")
def check_page() -> dict:
    return connection_check()


@plugin.command("check", "Проверить подключение: приложение OAuth, прокси, доступ ко входу и к Диску, аккаунты")
def check_command(args: dict) -> dict:
    return connection_check()


@plugin.command("publish", "Публичная ссылка на файл или папку из @yadisk")
def publish_command(args: dict) -> dict:
    path = mount_path(args.get("path") or "")
    name, _, rest = path.partition("/")
    if not rest:
        raise CommandError("нужен --path файла или папки внутри подключения: @yadisk/<подключение>/…")
    mount = command(yadisk.mount, name)
    if mount["kind"] != "account":
        return {"path": f"@yadisk/{path}", "public_url": mount["url"]}
    url = command(yadisk.disk(mount["account"]).publish, join(mount.get("path") or "/", rest))
    return {"path": f"@yadisk/{path}", "public_url": url}
