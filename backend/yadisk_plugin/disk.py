"""REST API Яндекс Диска (cloud-api.yandex.net/v1/disk): ресурсы аккаунта и публичные ссылки.

Пути аккаунта — «disk:/папка/файл»; публичной ссылки — от её корня («/папка»). Ошибки Диска переводятся в ошибки
хранилища pulse_plugin.fs: 404 — NotFound, 409 — Exists, 403/423 — ReadOnly, 507 — NoSpace.
"""

import time
from collections.abc import Callable, Iterator
from typing import IO

import httpx
from pulse_plugin.fs import Exists, FsError, NoSpace, NotFound, ReadOnly

API = "https://cloud-api.yandex.net/v1/disk"
PAGE = 1000
FIELDS = "name,type,size,modified,mime_type,path"
LIST_FIELDS = ",".join(["type", "_embedded.total"] + [f"_embedded.items.{f}" for f in FIELDS.split(",")])
CHUNK = 256 * 1024
OPERATION_WAIT_SEC = 120


class AuthExpired(FsError):
    """Вход в Яндекс истёк"""
    status = 401


def disk_path(path: str) -> str:
    return "disk:/" + path.strip("/")


def _detail(response: httpx.Response) -> str:
    try:
        data = response.json()
        return str(data.get("description") or data.get("message") or data.get("error") or "")
    except ValueError:
        return response.text[:200]


def check(response: httpx.Response, what: str = "") -> httpx.Response:
    if response.status_code < 400:
        return response
    detail = _detail(response)
    code = response.status_code
    if code == 401:
        raise AuthExpired("вход в Яндекс истёк — войдите снова на странице плагина")
    if code == 404:
        raise NotFound(f"не найдено{': ' + what if what else ''}")
    if code == 409:
        raise Exists(f"уже существует{': ' + what if what else ''}")
    if code in (403, 423):
        raise ReadOnly(detail or "нет прав на запись")
    if code == 507:
        raise NoSpace("на Яндекс Диске нет места")
    if code == 429:
        raise FsError("Яндекс Диск просит подождать: слишком много запросов")
    raise FsError(f"Яндекс Диск: {detail or code}")


def item(raw: dict) -> dict:
    is_dir = raw.get("type") == "dir"
    return {"name": raw.get("name") or "", "kind": "dir" if is_dir else "file",
            "size": None if is_dir else raw.get("size"), "modified_at": raw.get("modified"),
            "mime": None if is_dir else raw.get("mime_type"), "readonly": False}


def stream_href(http: httpx.Client, href: str, offset: int = 0, length: int | None = None) -> Iterator[bytes]:
    """Содержимое по ссылке скачивания (часть — заголовком Range)."""
    headers = {}
    if offset or length is not None:
        headers["Range"] = f"bytes={offset}-{'' if length is None else offset + length - 1}"
    with http.stream("GET", href, headers=headers, follow_redirects=True) as response:
        if response.status_code >= 400:
            response.read()
            check(response)
        skip = offset if response.status_code == 200 and offset else 0     # Range не поддержан — режем сами
        left = length
        for chunk in response.iter_bytes(CHUNK):
            if skip:
                if len(chunk) <= skip:
                    skip -= len(chunk)
                    continue
                chunk, skip = chunk[skip:], 0
            if left is not None:
                chunk = chunk[:left]
                left -= len(chunk)
            if chunk:
                yield chunk
            if left == 0:
                break


def file_chunks(stream: IO[bytes]) -> Iterator[bytes]:
    while chunk := stream.read(CHUNK):
        yield chunk


class Disk:
    """Диск одного аккаунта. token() — действующий токен (продление — забота вызывающего)."""

    def __init__(self, http: httpx.Client, token: Callable[[], str]):
        self.http = http
        self.token = token

    def _request(self, method: str, url: str, what: str = "", **kwargs) -> httpx.Response:
        headers = {"Authorization": f"OAuth {self.token()}", **kwargs.pop("headers", {})}
        try:
            response = self.http.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise FsError(f"Яндекс Диск не ответил: {exc.__class__.__name__}") from None
        return check(response, what)

    def info(self) -> dict:
        return self._request("GET", f"{API}/").json()

    def stat(self, path: str) -> dict:
        return self._request("GET", f"{API}/resources", path, params={"path": disk_path(path), "fields": FIELDS,
                                                                       "limit": 0}).json()

    def list(self, path: str) -> list[dict]:
        items, offset = [], 0
        while True:
            data = self._request("GET", f"{API}/resources", path, params={
                "path": disk_path(path), "limit": PAGE, "offset": offset, "fields": LIST_FIELDS}).json()
            if data.get("type") == "file":
                raise FsError("это файл, а не папка")
            embedded = data.get("_embedded") or {}
            page = embedded.get("items") or []
            items += page
            offset += len(page)
            if not page or offset >= (embedded.get("total") or 0):
                return items

    def has_children(self, path: str) -> bool:
        data = self._request("GET", f"{API}/resources", path, params={
            "path": disk_path(path), "limit": 1, "fields": "type,_embedded.total"}).json()
        return data.get("type") == "dir" and bool((data.get("_embedded") or {}).get("total"))

    def read(self, path: str, offset: int = 0, length: int | None = None) -> Iterator[bytes]:
        href = self._request("GET", f"{API}/resources/download", path, params={"path": disk_path(path)}).json()["href"]
        return stream_href(self.http, href, offset, length)

    def write(self, path: str, stream: IO[bytes], overwrite: bool = True) -> None:
        href = self._request("GET", f"{API}/resources/upload", path, params={
            "path": disk_path(path), "overwrite": str(overwrite).lower()}).json()["href"]
        stream.seek(0, 2)
        size = stream.tell()
        stream.seek(0)
        try:
            response = self.http.put(href, content=file_chunks(stream), headers={"Content-Length": str(size)},
                                     timeout=httpx.Timeout(None, connect=10))
        except httpx.HTTPError as exc:
            raise FsError(f"Яндекс Диск не принял файл: {exc.__class__.__name__}") from None
        check(response, path)

    def mkdir(self, path: str) -> None:
        self._request("PUT", f"{API}/resources", path, params={"path": disk_path(path)})

    def move(self, source: str, target: str) -> None:
        response = self._request("POST", f"{API}/resources/move", target, params={
            "from": disk_path(source), "path": disk_path(target), "overwrite": "false"})
        self._wait(response)

    def delete(self, path: str) -> None:
        """В корзину Диска, не навсегда."""
        response = self._request("DELETE", f"{API}/resources", path, params={"path": disk_path(path),
                                                                              "permanently": "false"})
        self._wait(response)

    def publish(self, path: str) -> str:
        self._request("PUT", f"{API}/resources/publish", path, params={"path": disk_path(path)})
        return self._request("GET", f"{API}/resources", path, params={"path": disk_path(path),
                                                                       "fields": "public_url"}).json()["public_url"]

    def save_public(self, public_key: str, name: str | None = None) -> None:
        params = {"public_key": public_key}
        if name:
            params["name"] = name
        self._wait(self._request("POST", f"{API}/public/resources/save-to-disk", params=params))

    def _wait(self, response: httpx.Response) -> None:
        """202 — долгая операция (перенос, удаление папки): ждём её завершения."""
        if response.status_code != 202 or not response.content:
            return
        href = response.json().get("href")
        deadline = time.monotonic() + OPERATION_WAIT_SEC
        while href and time.monotonic() < deadline:
            status = self._request("GET", href).json().get("status")
            if status == "success":
                return
            if status == "failed":
                raise FsError("Яндекс Диск не выполнил операцию")
            time.sleep(1)


class Public:
    """Публичная ссылка: без входа, только чтение."""

    def __init__(self, http: httpx.Client, public_key: str):
        self.http = http
        self.key = public_key

    def _get(self, url: str, path: str, **params) -> dict:
        try:
            response = self.http.get(url, params={"public_key": self.key, "path": "/" + path.strip("/"), **params})
        except httpx.HTTPError as exc:
            raise FsError(f"Яндекс Диск не ответил: {exc.__class__.__name__}") from None
        if response.status_code == 404:
            raise NotFound("ссылка не найдена или её закрыли" if not path.strip("/") else f"не найдено: {path}")
        return check(response, path).json()

    def stat(self, path: str = "") -> dict:
        return self._get(f"{API}/public/resources", path, limit=0)

    def list(self, path: str) -> list[dict]:
        items, offset = [], 0
        while True:
            data = self._get(f"{API}/public/resources", path, limit=PAGE, offset=offset)
            if data.get("type") == "file":
                raise FsError("это файл, а не папка")
            embedded = data.get("_embedded") or {}
            page = embedded.get("items") or []
            items += page
            offset += len(page)
            if not page or offset >= (embedded.get("total") or 0):
                return items

    def read(self, path: str, offset: int = 0, length: int | None = None) -> Iterator[bytes]:
        href = self._get(f"{API}/public/resources/download", path)["href"]
        return stream_href(self.http, href, offset, length)
