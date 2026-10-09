"""Плагин без сети: oauth.yandex.ru и cloud-api.yandex.net подменены (httpx.MockTransport). Запуск: cd backend && pytest"""

import json
import os
import tempfile
import threading
import time
from posixpath import dirname
from urllib.parse import parse_qs, quote, unquote, urlsplit

os.environ.setdefault("PULSE_PLUGIN_DATA", tempfile.mkdtemp(prefix="yadisk-test-"))
os.environ.setdefault("PULSE_PLUGIN_SETTINGS", json.dumps({"client_id": "test-app"}))
os.environ.setdefault("PULSE_PLUGIN_SECRETS", json.dumps({"client_secret": "test-secret"}))

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from yadisk_plugin import app as module  # noqa: E402
from yadisk_plugin.oauth import App, app_from  # noqa: E402
from yadisk_plugin.service import Yadisk  # noqa: E402
from yadisk_plugin.store import Store  # noqa: E402

PUBLIC = "https://disk.yandex.ru/d/Qw3rTy"


class FakeYandex:
    """OAuth (вход с устройства, продление) и Диск двух аккаунтов + публичная ссылка — в памяти."""

    def __init__(self):
        self.approved = False
        self.refresh_ok = True
        self.tokens = {}                    # access_token -> uid
        self.issued = 0
        self.users = {"1001": {"login": "ivan.petrov", "display_name": "Иван Петров"},
                      "2002": {"login": "petrov.work", "display_name": "Работа"}}
        self.next_uid = "1001"
        self.disks = {
            "1001": {"/": None, "/Документы": None, "/Документы/план.md": "# План\n".encode(),
                     "/Проект Альфа": None, "/Проект Альфа/ТЗ.pdf": bytes(range(256)) * 4},
            "2002": {"/": None, "/Отчёты": None},
        }
        self.public = {"/": None, "/фото1.jpg": b"jpeg-1", "/Вечер": None, "/Вечер/фото2.jpg": b"jpeg-2"}
        self.ranges = []
        self.oauth_down = False

    # --- помощники -----------------------------------------------------------------------------------------
    @staticmethod
    def _p(value: str) -> str:
        value = unquote(value)
        value = value[len("disk:"):] if value.startswith("disk:") else value
        return "/" + value.strip("/")

    @staticmethod
    def _res(fs: dict, path: str, public: bool = False) -> dict:
        data = fs[path]
        name = path.rsplit("/", 1)[-1] or ("Фото с конференции" if public else "disk")
        if data is None:
            return {"name": name, "type": "dir", "path": path, "modified": "2026-10-01T10:00:00+00:00"}
        return {"name": name, "type": "file", "path": path, "size": len(data), "mime_type": "application/octet-stream",
                "modified": "2026-10-02T10:00:00+00:00"}

    def _listing(self, fs: dict, path: str, query: dict, public: bool = False) -> httpx.Response:
        if path not in fs:
            return httpx.Response(404, json={"error": "DiskNotFoundError", "description": "Resource not found."})
        res = self._res(fs, path, public)
        if fs[path] is None:
            kids = sorted(p for p in fs if p != path and dirname(p) == path)
            offset, limit = int(query.get("offset", ["0"])[0]), int(query.get("limit", ["20"])[0])
            res["_embedded"] = {"items": [self._res(fs, p, public) for p in kids[offset:offset + limit]],
                                "total": len(kids), "limit": limit, "offset": offset}
        return httpx.Response(200, json=res)

    def _uid(self, request) -> str | None:
        auth = request.headers.get("authorization", "")
        return self.tokens.get(auth.removeprefix("OAuth "))

    def _issue(self, uid: str) -> dict:
        self.issued += 1
        token = f"tok-{uid}-{self.issued}"
        self.tokens[token] = uid
        return {"access_token": token, "refresh_token": f"ref-{uid}", "expires_in": 3600 * 24 * 365, "token_type": "bearer"}

    # --- обработчик ----------------------------------------------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        url = urlsplit(str(request.url))
        q = parse_qs(url.query)
        if url.netloc == "oauth.yandex.ru":
            if self.oauth_down:
                raise httpx.ReadTimeout("TLS handshake hangs", request=request)
            if request.method == "GET":
                return httpx.Response(200, text="oauth")
            form = parse_qs(request.content.decode())
            if url.path == "/device/code":
                assert form["client_id"] == ["test-app"] and form["device_id"][0]
                return httpx.Response(200, json={"device_code": "dev-1", "user_code": "4821937", "interval": 5,
                                                 "expires_in": 600, "verification_url": "https://oauth.yandex.ru/device"})
            assert form["client_secret"] == ["test-secret"]
            if form["grant_type"] == ["device_code"]:
                if not self.approved:
                    return httpx.Response(400, json={"error": "authorization_pending"})
                return httpx.Response(200, json=self._issue(self.next_uid))
            if form["grant_type"] == ["refresh_token"]:
                if not self.refresh_ok:
                    return httpx.Response(400, json={"error": "invalid_grant", "error_description": "revoked"})
                return httpx.Response(200, json=self._issue(form["refresh_token"][0].removeprefix("ref-")))
        if url.netloc == "downloader.test":
            fs = self.public if url.path.startswith("/public") else self.disks[url.path.split("/")[1]]
            key = url.path[len("/public"):] if url.path.startswith("/public") else "/" + url.path.split("/", 2)[2]
            data = fs[unquote(key)]
            rng = request.headers.get("range")
            if rng:
                self.ranges.append(rng)
                start, _, end = rng.removeprefix("bytes=").partition("-")
                part = data[int(start):(int(end) + 1 if end else None)]
                return httpx.Response(206, content=part)
            return httpx.Response(200, content=data)
        if url.netloc == "uploader.test":
            uid, path = url.path.split("/", 2)[1], "/" + unquote(url.path.split("/", 2)[2])
            self.disks[uid][path] = request.read()
            return httpx.Response(201)
        if url.netloc != "cloud-api.yandex.net":
            return httpx.Response(500)
        api = url.path.removeprefix("/v1/disk")
        if api.startswith("/public/resources"):
            if q.get("public_key") != [PUBLIC]:
                return httpx.Response(404, json={"error": "DiskNotFoundError"})
            path = self._p(q.get("path", ["/"])[0])
            if api == "/public/resources":
                return self._listing(self.public, path, q, public=True)
            if api == "/public/resources/download":
                return httpx.Response(200, json={"href": f"https://downloader.test/public{quote(path)}"})
            if api == "/public/resources/save-to-disk":
                uid = self._uid(request)
                self.disks[uid]["/Загрузки"] = None
                return httpx.Response(201, json={})
        uid = self._uid(request)
        if uid is None:
            return httpx.Response(401, json={"error": "UnauthorizedError"})
        fs = self.disks[uid]
        if api == "/":
            return httpx.Response(200, json={"total_space": 100, "used_space": 38,
                                             "user": {"uid": uid, **self.users[uid]}})
        path = self._p(q.get("path", ["/"])[0])
        if api == "/resources" and request.method == "GET":
            if q.get("fields") == ["public_url"]:
                return httpx.Response(200, json={"public_url": f"https://disk.yandex.ru/d/pub{quote(path)}"})
            return self._listing(fs, path, q)
        if api == "/resources" and request.method == "PUT":
            if path in fs:
                return httpx.Response(409, json={"error": "DiskPathPointsToExistentDirectoryError"})
            fs[path] = None
            return httpx.Response(201, json={})
        if api == "/resources" and request.method == "DELETE":
            assert q["permanently"] == ["false"]
            for p in [p for p in fs if p == path or p.startswith(path + "/")]:
                del fs[p]
            return httpx.Response(202, json={"href": "https://cloud-api.yandex.net/v1/disk/operations/op1"})
        if api == "/operations/op1":
            return httpx.Response(200, json={"status": "success"})
        if api == "/resources/download":
            if path not in fs:
                return httpx.Response(404, json={"error": "DiskNotFoundError"})
            return httpx.Response(200, json={"href": f"https://downloader.test/{uid}/{quote(path.lstrip('/'))}"})
        if api == "/resources/upload":
            if path in fs and q["overwrite"] == ["false"]:
                return httpx.Response(409, json={"error": "DiskResourceAlreadyExistsError"})
            return httpx.Response(200, json={"href": f"https://uploader.test/{uid}/{quote(path.lstrip('/'))}"})
        if api == "/resources/move":
            src = self._p(q["from"][0])
            if path in fs:
                return httpx.Response(409, json={"error": "DiskResourceAlreadyExistsError"})
            for p in [p for p in list(fs) if p == src or p.startswith(src + "/")]:
                fs[path + p[len(src):]] = fs.pop(p)
            return httpx.Response(201, json={})
        if api == "/resources/publish":
            return httpx.Response(200, json={})
        return httpx.Response(500, json={"error": f"нет ручки {request.method} {api}"})


@pytest.fixture
def fake(tmp_path, monkeypatch):
    fake = FakeYandex()
    http = httpx.Client(transport=httpx.MockTransport(fake.handler))
    signals = []
    service = Yadisk(Store(tmp_path), http, lambda: App("test-app", "test-secret"),
                     on_expired=signals.append, sleep=lambda s: threading.Event().wait())   # фоновый опрос стоит; опрашивает тест
    monkeypatch.setattr(module, "yadisk", service)
    monkeypatch.setattr(module.files, "y", service)          # хранилище /fs/… — тот же сервис
    fake.service, fake.signals = service, signals
    return fake


@pytest.fixture
def client(fake):
    return TestClient(module.app)


def login(client, fake, uid="1001"):
    fake.next_uid = uid
    fake.approved = False
    started = client.post("/login").json()
    assert started["status"] == "pending" and started["user_code"] == "4821937"
    assert fake.service.poll_once() == "pending"
    fake.approved = True
    assert fake.service.poll_once() == "done"


# --- вход и аккаунты ---------------------------------------------------------------------------------------

def test_app_defaults_and_override():
    assert app_from({}, {}) is None                              # значений по умолчанию нет
    assert app_from({"client_id": "my"}, {}) is None              # без секрета — тоже не настроено
    app = app_from({"client_id": " my "}, {"client_secret": "s"})
    assert app.client_id == "my" and app.client_secret == "s"


def test_login_adds_account_and_whole_disk_mount(client, fake):
    login(client, fake)
    state = client.get("/state").json()
    assert state["login"]["status"] == "done" and state["login"]["account"] == "Иван Петров"
    [account] = state["accounts"]
    assert account["login"] == "ivan.petrov" and account["used_space"] == 38 and "access_token" not in account
    [mount] = state["mounts"]
    assert mount["name"] == "Иван Петров" and mount["path"] == "/" and mount["files_path"] == "@yadisk/Иван Петров"
    # второй вход в тот же аккаунт не плодит подключения; второй аккаунт — своё
    login(client, fake)
    login(client, fake, "2002")
    names = [m["name"] for m in client.get("/state").json()["mounts"]]
    assert names == ["Иван Петров", "Работа"]


def test_login_errors(client, fake, monkeypatch):
    monkeypatch.setattr(fake.service, "app", lambda: None)
    failed = client.post("/login")
    assert failed.status_code == 400 and "ClientID" in failed.json()["detail"]
    assert client.get("/state").json()["app"] == {"configured": False}


def test_token_refresh_and_expired_signal(client, fake):
    login(client, fake)
    fake.service.store.change(lambda d: d["accounts"]["1001"].update(expires_at=time.time() + 10))
    assert client.get("/fs/list", params={"path": "Иван Петров"}).status_code == 200
    assert fake.service.store.load()["accounts"]["1001"]["access_token"] == "tok-1001-2"     # продлён

    fake.refresh_ok = False
    fake.service.store.change(lambda d: d["accounts"]["1001"].update(expires_at=time.time() + 10))
    expired = client.get("/fs/list", params={"path": "Иван Петров"})
    assert expired.status_code == 401 and "войдите снова" in expired.json()["detail"]
    client.get("/fs/list", params={"path": "Иван Петров"})
    assert [a["login"] for a in fake.signals] == ["ivan.petrov"]                             # сигнал один раз
    assert client.get("/state").json()["accounts"][0]["expired"] is True
    # вошли снова — вход жив, сигнал снова возможен
    fake.refresh_ok = True
    login(client, fake)
    assert client.get("/fs/list", params={"path": "Иван Петров"}).status_code == 200


# --- хранилище @yadisk ---------------------------------------------------------------------------------------

def test_storage_reads_and_writes_account_disk(client, fake):
    login(client, fake)
    root = client.get("/fs/list").json()["items"]
    assert [(i["name"], i["kind"], i["readonly"]) for i in root] == [("Иван Петров", "dir", False)]
    top = client.get("/fs/list", params={"path": "Иван Петров"}).json()["items"]
    assert [i["name"] for i in top] == ["Документы", "Проект Альфа"]
    pdf = client.get("/fs/stat", params={"path": "Иван Петров/Проект Альфа/ТЗ.pdf"}).json()
    assert pdf["kind"] == "file" and pdf["size"] == 1024 and pdf["name"] == "ТЗ.pdf"

    whole = client.get("/fs/read", params={"path": "Иван Петров/Проект Альфа/ТЗ.pdf"})
    assert whole.content == bytes(range(256)) * 4
    part = client.get("/fs/read", params={"path": "Иван Петров/Проект Альфа/ТЗ.pdf"}, headers={"Range": "bytes=10-13"})
    assert part.status_code == 206 and part.content == bytes([10, 11, 12, 13]) and fake.ranges[-1] == "bytes=10-13"

    put = client.put("/fs/write", params={"path": "Иван Петров/Документы/итоги.md"}, content="итоги".encode())
    assert put.status_code == 200 and fake.disks["1001"]["/Документы/итоги.md"] == "итоги".encode()
    exists = client.put("/fs/write", params={"path": "Иван Петров/Документы/итоги.md", "overwrite": "false"}, content=b"x")
    assert exists.status_code == 409
    assert client.post("/fs/mkdir", json={"path": "Иван Петров/Новая"}).status_code == 200 and "/Новая" in fake.disks["1001"]
    moved = client.post("/fs/move", json={"source": "Иван Петров/Документы/итоги.md", "target": "Иван Петров/Новая/итоги.md"})
    assert moved.status_code == 200 and "/Новая/итоги.md" in fake.disks["1001"]

    not_empty = client.delete("/fs/delete", params={"path": "Иван Петров/Новая"})
    assert not_empty.status_code == 409
    assert client.delete("/fs/delete", params={"path": "Иван Петров/Новая", "recursive": "true"}).status_code == 200
    assert "/Новая" not in fake.disks["1001"]
    # корень @yadisk — только подключения
    assert client.post("/fs/mkdir", json={"path": "Своя"}).status_code == 403
    assert client.delete("/fs/delete", params={"path": "Иван Петров"}).status_code == 403
    # перенос подключения в корне — его переименование
    assert client.post("/fs/move", json={"source": "Иван Петров", "target": "Иван"}).status_code == 200
    assert [m["name"] for m in client.get("/state").json()["mounts"]] == ["Иван"]


def test_shared_folder_and_cross_account_move(client, fake):
    login(client, fake)
    login(client, fake, "2002")
    folders = client.get("/browse", params={"account": "ivan.petrov"}).json()["folders"]
    assert [f["path"] for f in folders] == ["/Документы", "/Проект Альфа"]
    mount = client.post("/mounts", json={"kind": "account", "account": "ivan.petrov", "path": "/Проект Альфа"}).json()
    assert mount["name"] == "Проект Альфа" and mount["account"]["login"] == "ivan.petrov"
    assert [i["name"] for i in client.get("/fs/list", params={"path": "Проект Альфа"}).json()["items"]] == ["ТЗ.pdf"]
    dup = client.post("/mounts", json={"kind": "account", "account": "ivan.petrov", "path": "/Документы", "name": "Проект Альфа"})
    assert dup.status_code == 400 and "уже есть" in dup.json()["detail"]
    file_mount = client.post("/mounts", json={"kind": "account", "account": "ivan.petrov", "path": "/Документы/план.md"})
    assert file_mount.status_code == 400 and "файл" in file_mount.json()["detail"]

    # между аккаунтами — копия и удаление оригинала
    moved = client.post("/fs/move", json={"source": "Проект Альфа/ТЗ.pdf", "target": "Работа/Отчёты/ТЗ.pdf"})
    assert moved.status_code == 200, moved.text
    assert fake.disks["2002"]["/Отчёты/ТЗ.pdf"] == bytes(range(256)) * 4 and "/Проект Альфа/ТЗ.pdf" not in fake.disks["1001"]


def test_public_link_is_read_only_and_can_be_saved(client, fake):
    login(client, fake)
    bad = client.get("/public", params={"url": "https://example.com/x"})
    assert bad.status_code == 400
    gone = client.get("/public", params={"url": "https://disk.yandex.ru/d/missing"})
    assert gone.status_code == 400 and "не найдена" in gone.json()["detail"]
    preview = client.get("/public", params={"url": PUBLIC}).json()
    assert preview["name"] == "Фото с конференции" and preview["items"] == 2
    mount = client.post("/mounts", json={"kind": "public", "url": PUBLIC}).json()
    assert mount["name"] == "Фото с конференции" and mount["readonly"] is True

    items = client.get("/fs/list", params={"path": "Фото с конференции"}).json()["items"]
    assert [(i["name"], i["readonly"]) for i in items] == [("Вечер", True), ("фото1.jpg", True)]
    assert client.get("/fs/read", params={"path": "Фото с конференции/Вечер/фото2.jpg"}).content == b"jpeg-2"
    denied = client.put("/fs/write", params={"path": "Фото с конференции/x.txt"}, content=b"x")
    assert denied.status_code == 403 and "только чтение" in denied.json()["detail"]
    saved = client.post("/mounts/Фото с конференции/save", json={}).json()
    assert saved["folder"] == "Загрузки" and "/Загрузки" in fake.disks["1001"]
    assert client.delete("/mounts/Фото с конференции").status_code == 200
    assert [m["name"] for m in client.get("/state").json()["mounts"]] == ["Иван Петров"]


# --- прокси и проверка подключения ------------------------------------------------------------------------------

def test_proxy_only_for_login_or_for_everything():
    from yadisk_plugin import net

    direct = net.build_http({}, {})
    assert direct._transport_for_url(httpx.URL("https://oauth.yandex.ru/token")) is direct._transport
    login_only = net.build_http({}, {"proxy": "http://user:pw@ru-proxy.example:3128"})
    assert login_only._transport_for_url(httpx.URL("https://oauth.yandex.ru/token")) is not login_only._transport
    assert login_only._transport_for_url(httpx.URL("https://cloud-api.yandex.net/v1/disk/")) is login_only._transport
    everything = net.build_http({"proxy_all": True}, {"proxy": "http://ru-proxy.example:3128"})
    assert everything._transport_for_url(httpx.URL("https://cloud-api.yandex.net/v1/disk/")) is not None
    assert net.proxy_label("http://user:pw@ru-proxy.example:3128") == "http://ru-proxy.example:3128"   # без пароля
    with pytest.raises(net.ProxyError):
        net.proxy_of({}, {"proxy": "ru-proxy.example"})


def test_connection_check(client, fake, monkeypatch):
    login(client, fake)
    result = client.post("/commands/check", json={}).json()
    titles = {c["title"]: c for c in result["checks"]}
    assert result["ok"] is True and titles["Вход: oauth.yandex.ru"]["ok"] and titles["Аккаунт ivan.petrov"]["ok"]
    assert titles["Диск: cloud-api.yandex.net"]["detail"] == "отвечает (HTTP 401)"

    fake.oauth_down = True                              # сервер вне России: вход не отвечает
    down = client.get("/check").json()
    failed = next(c for c in down["checks"] if c["title"] == "Вход: oauth.yandex.ru")
    assert down["ok"] is False and "блокировку" in failed["detail"] and "прокси в РФ" in failed["detail"]
    started = client.post("/login")
    assert started.status_code == 400 and "прокси" in started.json()["detail"]


# --- команды агентов -----------------------------------------------------------------------------------------

def test_agent_commands(client, fake):
    started = client.post("/commands/login", json={}).json()
    assert started["user_code"] == "4821937" and "oauth.yandex.ru/device" in started["tell_owner"]
    fake.approved = True
    fake.service.poll_once()
    assert client.post("/commands/login/status", json={}).json()["status"] == "done"
    assert client.post("/commands/accounts", json={}).json()["accounts"][0]["login"] == "ivan.petrov"
    mounted = client.post("/commands/mount/folder", json={"path": "/Документы", "name": "Доки"}).json()
    assert mounted["files_path"] == "@yadisk/Доки"
    no_url = client.post("/commands/mount/public", json={})
    assert no_url.status_code == 400 and "--url" in no_url.json()["detail"]
    link = client.post("/commands/publish", json={"path": "@yadisk/Доки/план.md"}).json()
    assert link["public_url"].startswith("https://disk.yandex.ru/d/")
    assert client.post("/commands/unmount", json={"name": "Доки"}).json() == {"unmounted": "Доки"}
    assert client.post("/commands/logout", json={}).json() == {"logged_out": "ivan.petrov"}
    assert client.get("/state").json()["mounts"] == []
