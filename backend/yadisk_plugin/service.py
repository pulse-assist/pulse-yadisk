"""Аккаунты, вход, подключения и хранилище @yadisk.

Аккаунт — вход через oauth.yandex.ru/device (oauth.py), токены продлеваются сами. Подключение — папка верхнего
уровня в @yadisk: весь диск или папка аккаунта (сюда же — принятые общие папки: они лежат на диске аккаунта) либо
публичная ссылка (только чтение, без входа). Хранилище (YadiskFiles) отдаёт подключения ядру Пульса по протоколу
pulse_plugin.fs.
"""

import re
import tempfile
import threading
import time
import uuid
from collections.abc import Callable

import httpx
from pulse_plugin.fs import Exists, FileProvider, FsError, NotEmpty, NotFound, ReadOnly, entry

from .disk import AuthExpired, Disk, Public, item
from .oauth import App, OAuthError, Pending, YandexOAuth
from .store import Store

REFRESH_BEFORE_SEC = 3600
_PUBLIC = re.compile(r"^https://(disk\.yandex\.[a-z]{2,3}|yadi\.sk|disk\.360\.yandex\.[a-z]{2,3})/(d|i|a)/[\w-]+", re.I)
_BAD_NAME = re.compile(r'[/\\<>:"|?*\x00]')


class UserError(Exception):
    """Ошибка, понятная владельцу и агенту (400)."""


def clean_name(name: str) -> str:
    name = " ".join((name or "").split())
    if not name or name.startswith(".") or _BAD_NAME.search(name) or len(name) > 100:
        raise UserError("Имя папки — до 100 символов, без / \\ < > : \" | ? * и не с точки")
    return name


def join(*parts: str) -> str:
    return "/".join(p.strip("/") for p in parts if p and p.strip("/"))


class Yadisk:
    def __init__(self, store: Store, http: httpx.Client, app: Callable[[], App | None],
                 on_expired: Callable[[dict], None] = lambda account: None, clock=time.time, sleep=time.sleep):
        self.store = store
        self.http = http
        self.app = app
        self.on_expired = on_expired
        self.clock = clock
        self.sleep = sleep
        self._refresh_lock = threading.Lock()

    def _oauth(self) -> YandexOAuth:
        app = self.app()
        if app is None:
            raise UserError("Вход не настроен: укажите ClientID и Client secret приложения Яндекс OAuth в настройках "
                            "плагина «Яндекс Диск» (их выдаёт администратор вашей установки Пульса)")
        return YandexOAuth(app, self.http)

    # --- вход ---------------------------------------------------------------------------------------------

    def login_state(self) -> dict | None:
        login = self.store.load().get("login")
        if not login:
            return None
        if login["status"] == "pending" and self.clock() > login["expires_at"]:
            login = {**login, "status": "expired", "error": "Код входа истёк — начните вход заново"}
        return {k: login.get(k) for k in ("status", "user_code", "verification_url", "expires_at", "error", "account")}

    def start_login(self, background: bool = True) -> dict:
        oauth = self._oauth()

        def device_id(data: dict) -> str:
            data.setdefault("device_id", uuid.uuid4().hex)
            return data["device_id"]
        try:
            code = oauth.device_code(self.store.change(device_id), "Пульс")
        except OAuthError as exc:
            raise UserError(str(exc)) from None
        except httpx.HTTPError as exc:
            raise UserError(f"Яндекс не ответил: {exc.__class__.__name__}") from None
        login = {**code, "status": "pending", "error": None, "account": None, "id": uuid.uuid4().hex}
        self.store.change(lambda data: data.__setitem__("login", login))
        if background:
            threading.Thread(target=self._poll_loop, args=(login["id"],), daemon=True).start()
        return self.login_state()

    def cancel_login(self) -> None:
        self.store.change(lambda data: data.pop("login", None))

    def _poll_loop(self, login_id: str) -> None:
        while True:
            login = self.store.load().get("login") or {}
            if login.get("id") != login_id or login.get("status") != "pending":
                return
            self.sleep(max(int(login.get("interval") or 5), 2))
            if self.poll_once(login_id) != "pending":
                return

    def poll_once(self, login_id: str | None = None) -> str:
        """Один опрос Яндекса: pending | done | error | expired."""
        login = self.store.load().get("login") or {}
        if not login or (login_id and login.get("id") != login_id) or login.get("status") != "pending":
            return login.get("status") or "none"
        if self.clock() > login["expires_at"]:
            return self._finish_login(login["id"], "expired", error="Код входа истёк — начните вход заново")
        try:
            tokens = self._oauth().poll(login["device_code"])
        except Pending:
            return "pending"
        except (OAuthError, UserError) as exc:
            return self._finish_login(login["id"], "error", error=str(exc))
        except httpx.HTTPError:
            return "pending"                         # сеть моргнула — спросим ещё раз
        try:
            account = self._add_account(tokens)
        except FsError as exc:
            return self._finish_login(login["id"], "error", error=f"Вход выполнен, но Диск не ответил: {exc}")
        return self._finish_login(login["id"], "done", account=account["name"])

    def _finish_login(self, login_id: str, status: str, error: str | None = None, account: str | None = None) -> str:
        def apply(data: dict) -> None:
            login = data.get("login")
            if login and login.get("id") == login_id:
                login.update(status=status, error=error, account=account, device_code=None)
        self.store.change(apply)
        return status

    def _add_account(self, tokens: dict) -> dict:
        info = Disk(self.http, lambda: tokens["access_token"]).info()
        user = info.get("user") or {}
        uid = str(user.get("uid") or user.get("login"))
        account = {"uid": uid, "login": user.get("login") or uid, "name": user.get("display_name") or user.get("login"),
                   **tokens, "expired": False}

        def apply(data: dict) -> dict:
            accounts = data.setdefault("accounts", {})
            known = uid in accounts
            accounts[uid] = {**accounts.get(uid, {}), **account}
            mounts = data.setdefault("mounts", [])
            if not known and not any(m.get("account") == uid for m in mounts):
                name = self._free_name(mounts, account["name"] or account["login"])
                mounts.append({"name": name, "kind": "account", "account": uid, "path": "/"})
            data["expired_signal"] = [u for u in data.get("expired_signal", []) if u != uid]
            return accounts[uid]
        return self.store.change(apply)

    # --- аккаунты -----------------------------------------------------------------------------------------

    def _account(self, ref: str) -> dict:
        accounts = self.store.load().get("accounts") or {}
        ref = (ref or "").strip()
        if not ref and len(accounts) == 1:
            return next(iter(accounts.values()))
        for account in accounts.values():
            if ref.casefold() in (account["uid"], account["login"].casefold(), (account.get("name") or "").casefold()):
                return account
        names = ", ".join(a["login"] for a in accounts.values()) or "нет ни одного — сначала войдите"
        raise UserError(f"Аккаунт «{ref}» не найден; аккаунты: {names}")

    def token(self, uid: str) -> str:
        account = (self.store.load().get("accounts") or {}).get(uid)
        if account is None:
            raise AuthExpired("аккаунт отключён")
        if account.get("expired"):
            raise AuthExpired(f"вход в Яндекс ({account['login']}) истёк — войдите снова на странице плагина")
        if account["expires_at"] - self.clock() > REFRESH_BEFORE_SEC:
            return account["access_token"]
        with self._refresh_lock:
            account = self.store.load()["accounts"][uid]
            if account["expires_at"] - self.clock() > REFRESH_BEFORE_SEC:
                return account["access_token"]
            try:
                if not account.get("refresh_token"):
                    raise OAuthError("нет refresh token")
                tokens = self._oauth().refresh(account["refresh_token"])
            except (OAuthError, UserError, httpx.HTTPError):
                self._expire(uid)
                raise AuthExpired(f"вход в Яндекс ({account['login']}) истёк — войдите снова на странице плагина")

            def apply(data: dict) -> None:
                data["accounts"][uid].update({k: v for k, v in tokens.items() if v})
            self.store.change(apply)
            return tokens["access_token"]

    def _expire(self, uid: str) -> None:
        def apply(data: dict) -> dict | None:
            account = (data.get("accounts") or {}).get(uid)
            if account is None:
                return None
            account["expired"] = True
            signalled = data.setdefault("expired_signal", [])
            if uid in signalled:
                return None
            signalled.append(uid)
            return account
        account = self.store.change(apply)
        if account is not None:
            self.on_expired(account)

    def disk(self, uid: str) -> Disk:
        def token() -> str:
            return self.token(uid)
        return Disk(self.http, token)

    def accounts(self, space: bool = False) -> list[dict]:
        out = []
        for account in (self.store.load().get("accounts") or {}).values():
            row = {"uid": account["uid"], "login": account["login"], "name": account.get("name"),
                   "expired": bool(account.get("expired"))}
            if space and not row["expired"]:
                try:
                    info = self.disk(account["uid"]).info()
                    row.update(used_space=info.get("used_space"), total_space=info.get("total_space"))
                except FsError as exc:
                    row["error"] = str(exc)
                    row["expired"] = isinstance(exc, AuthExpired)
            out.append(row)
        return out

    def logout(self, ref: str, unmount: bool = True) -> dict:
        account = self._account(ref)

        def apply(data: dict) -> None:
            data.get("accounts", {}).pop(account["uid"], None)
            if unmount:
                data["mounts"] = [m for m in data.get("mounts", []) if m.get("account") != account["uid"]
                                  or m.get("kind") == "public"]
        self.store.change(apply)
        return {"logged_out": account["login"]}

    # --- подключения --------------------------------------------------------------------------------------

    @staticmethod
    def _free_name(mounts: list[dict], wanted: str) -> str:
        base = clean_name(wanted)
        taken = {m["name"].casefold() for m in mounts}
        name, n = base, 2
        while name.casefold() in taken:
            name, n = f"{base} ({n})", n + 1
        return name

    def mounts(self) -> list[dict]:
        accounts = self.store.load().get("accounts") or {}
        out = []
        for mount in self.store.load().get("mounts") or []:
            row = {k: mount.get(k) for k in ("name", "kind", "path", "url", "title", "target_type")}
            row["files_path"] = f"@yadisk/{mount['name']}"
            row["readonly"] = mount["kind"] == "public"
            if mount["kind"] == "account":
                account = accounts.get(mount["account"])
                row["account"] = account and {"uid": account["uid"], "login": account["login"],
                                              "name": account.get("name"), "expired": bool(account.get("expired"))}
            out.append(row)
        return out

    def mount(self, name: str) -> dict:
        for mount in self.store.load().get("mounts") or []:
            if mount["name"] == name:
                return mount
        raise NotFound(f"нет подключения «{name}»")

    def _add_mount(self, mount: dict, wanted: str | None, default: str) -> dict:
        def apply(data: dict) -> str:
            mounts = data.setdefault("mounts", [])
            if wanted:
                name = clean_name(wanted)
                if any(m["name"].casefold() == name.casefold() for m in mounts):
                    raise UserError(f"Подключение «{name}» уже есть — выберите другое имя")
            else:
                name = self._free_name(mounts, default)
            mounts.append({**mount, "name": name})
            return name
        name = self.store.change(apply)
        return next(m for m in self.mounts() if m["name"] == name)

    def add_folder(self, account_ref: str, path: str = "/", name: str | None = None) -> dict:
        account = self._account(account_ref)
        path = "/" + path.strip().strip("/")
        if path != "/":
            info = self.disk(account["uid"]).stat(path)
            if info.get("type") != "dir":
                raise UserError(f"«{path}» — файл, а не папка: подключить можно папку")
        default = (account.get("name") or account["login"]) if path == "/" else path.rsplit("/", 1)[-1]
        return self._add_mount({"kind": "account", "account": account["uid"], "path": path}, name, default)

    def preview_public(self, url: str) -> dict:
        url = (url or "").strip()
        if not _PUBLIC.match(url):
            raise UserError("Это не публичная ссылка Яндекс Диска (https://disk.yandex.ru/d/…)")
        try:
            info = Public(self.http, url).stat()
        except NotFound:
            raise UserError("Ссылка не найдена или её закрыли — проверьте адрес") from None
        embedded = info.get("_embedded") or {}
        return {"url": url, "name": info.get("name"), "type": info.get("type"), "size": info.get("size"),
                "items": embedded.get("total")}

    def add_public(self, url: str, name: str | None = None) -> dict:
        preview = self.preview_public(url)
        return self._add_mount({"kind": "public", "url": preview["url"], "title": preview["name"],
                                "target_type": preview["type"]}, name, preview["name"] or "Публичная ссылка")

    def rename(self, name: str, new: str) -> dict:
        new = clean_name(new)

        def apply(data: dict) -> None:
            mounts = data.get("mounts") or []
            if not any(m["name"] == name for m in mounts):
                raise UserError(f"Нет подключения «{name}»")
            if new.casefold() != name.casefold() and any(m["name"].casefold() == new.casefold() for m in mounts):
                raise UserError(f"Подключение «{new}» уже есть")
            for m in mounts:
                if m["name"] == name:
                    m["name"] = new
        self.store.change(apply)
        return next(m for m in self.mounts() if m["name"] == new)

    def unmount(self, name: str) -> dict:
        def apply(data: dict) -> None:
            mounts = data.get("mounts") or []
            if not any(m["name"] == name for m in mounts):
                raise UserError(f"Нет подключения «{name}»")
            data["mounts"] = [m for m in mounts if m["name"] != name]
        self.store.change(apply)
        return {"unmounted": name}

    def browse(self, account_ref: str, path: str = "/") -> dict:
        """Папки аккаунта для выбора подключения; общие (чужие) папки помечены shared."""
        account = self._account(account_ref)
        items = self.disk(account["uid"]).list(path)
        folders = [{"name": i["name"], "path": "/" + join(path, i["name"])} for i in items if i.get("type") == "dir"]
        return {"account": account["login"], "path": "/" + path.strip("/"), "folders": folders}

    def save_public(self, name: str, account_ref: str = "") -> dict:
        mount = self.mount(name)
        if mount["kind"] != "public":
            raise UserError("Сохранить к себе можно только публичную ссылку")
        account = self._account(account_ref)
        self.disk(account["uid"]).save_public(mount["url"])
        return {"saved": mount.get("title") or name, "account": account["login"], "folder": "Загрузки"}


class YadiskFiles(FileProvider):
    """@yadisk/<подключение>/… → диск аккаунта или публичная ссылка."""

    def __init__(self, yadisk: Yadisk):
        self.y = yadisk

    def _resolve(self, path: str) -> tuple[dict | None, str]:
        if not path:
            return None, ""
        name, _, rest = path.partition("/")
        return self.y.mount(name), rest

    def _target(self, mount: dict, rest: str):
        """(api, путь в нём) — Disk аккаунта или Public ссылки."""
        if mount["kind"] == "public":
            if mount.get("target_type") == "file":
                if not rest:
                    return None, ""
                if rest != mount.get("title"):
                    raise NotFound(rest)
                return Public(self.y.http, mount["url"]), ""
            return Public(self.y.http, mount["url"]), rest
        return self.y.disk(mount["account"]), join(mount.get("path") or "/", rest)

    @staticmethod
    def _readonly(mount: dict) -> bool:
        return mount["kind"] == "public"

    def list(self, path: str) -> list[dict]:
        mount, rest = self._resolve(path)
        if mount is None:
            return [entry(m["name"], "dir", readonly=self._readonly(m)) for m in self.y.store.load().get("mounts") or []]
        api, inner = self._target(mount, rest)
        if api is None:                                  # публичная ссылка на один файл — папка с этим файлом
            return [{**item(Public(self.y.http, mount["url"]).stat()), "readonly": True}]
        return [{**item(raw), "readonly": self._readonly(mount)} for raw in api.list(inner)]

    def stat(self, path: str) -> dict:
        mount, rest = self._resolve(path)
        if mount is None:
            return entry("", "dir")
        api, inner = self._target(mount, rest)
        if api is None or (not rest):
            return entry(mount["name"], "dir", readonly=self._readonly(mount))
        info = {**item(api.stat(inner)), "readonly": self._readonly(mount)}
        info["name"] = rest.rsplit("/", 1)[-1]
        return info

    def read(self, path: str, offset: int = 0, length: int | None = None):
        mount, rest = self._resolve(path)
        if mount is None or not rest:
            raise FsError("это папка")
        api, inner = self._target(mount, rest)
        return api.read(inner, offset, length)

    def _writable(self, path: str, what: str) -> tuple[dict, str]:
        if "/" not in path:
            raise ReadOnly(f"{what}: подключения меняются на странице плагина «Яндекс Диск»")
        mount, rest = self._resolve(path)
        if mount is None or not rest:
            raise ReadOnly(f"{what}: подключения меняются на странице плагина «Яндекс Диск»")
        if self._readonly(mount):
            raise ReadOnly("публичная ссылка — только чтение: сохраните её к себе на диск")
        return mount, rest

    def write(self, path: str, stream, overwrite: bool = True) -> dict:
        mount, rest = self._writable(path, "файл в корне @yadisk")
        disk, inner = self._target(mount, rest)
        disk.write(inner, stream, overwrite)
        return self.stat(path)

    def mkdir(self, path: str) -> dict:
        mount, rest = self._writable(path, "новая папка в корне @yadisk")
        disk, inner = self._target(mount, rest)
        disk.mkdir(inner)
        return entry(rest.rsplit("/", 1)[-1], "dir")

    def move(self, source: str, target: str) -> dict:
        src_mount, src_rest = self._resolve(source)
        if src_mount is not None and not src_rest and "/" not in target:   # переименование подключения
            self.y.rename(src_mount["name"], target)
            return entry(target, "dir", readonly=self._readonly(src_mount))
        src_mount, src_rest = self._writable(source, "перенос подключения")
        dst_mount, dst_rest = self._writable(target, "перенос в корень @yadisk")
        if src_mount["account"] == dst_mount["account"]:
            disk, src_inner = self._target(src_mount, src_rest)
            disk.move(src_inner, self._target(dst_mount, dst_rest)[1])
            return self.stat(target)
        info = self.stat(source)                         # разные аккаунты — копия и удаление
        if info["kind"] == "dir":
            raise FsError("папки между аккаунтами переносите по файлам")
        if self._exists(target):
            raise Exists(target)
        with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as spool:
            for chunk in self.read(source):
                spool.write(chunk)
            spool.seek(0)
            result = self.write(target, spool, overwrite=False)
        self.delete(source)
        return result

    def _exists(self, path: str) -> bool:
        try:
            self.stat(path)
            return True
        except NotFound:
            return False

    def delete(self, path: str, recursive: bool = False) -> None:
        mount, rest = self._writable(path, "отключить подключение")
        disk, inner = self._target(mount, rest)
        if not recursive and disk.has_children(inner):
            raise NotEmpty("папка не пуста")
        disk.delete(inner)
