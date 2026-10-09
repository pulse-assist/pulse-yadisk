"""Вход в Яндекс по коду с другого устройства (OAuth device flow): плагин получает код, владелец вводит его на
oauth.yandex.ru/device в своём браузере, плагин опрашивает /token и получает токены. Ни пароль, ни внешний адрес
Пульса не нужны.

Приложение Яндекс OAuth: по умолчанию — общее приложение «Пульс» (ниже); владелец может заменить его своим в
настройках плагина (client_id, client_secret). У приложения должны быть права Яндекс Диска: cloud_api:disk.read,
cloud_api:disk.write, cloud_api:disk.info.
"""

import time

import httpx

OAUTH = "https://oauth.yandex.ru"
# приложение «Пульс» организации pulse-assist (oauth.yandex.ru). Пусто — пока не зарегистрировано: тогда нужен свой
# client_id в настройках плагина. Секрет приложения для входа с устройства не защищает данные владельца: доступ
# даёт только код, который владелец сам ввёл в своём аккаунте.
DEFAULT_CLIENT_ID = ""
DEFAULT_CLIENT_SECRET = ""


class OAuthError(Exception):
    pass


class Pending(OAuthError):
    """Владелец ещё не ввёл код"""


class App:
    def __init__(self, client_id: str, client_secret: str, own: bool):
        self.client_id = client_id
        self.client_secret = client_secret
        self.own = own                      # своё приложение владельца, а не «Пульс»


def app_from(settings: dict, secrets: dict) -> App | None:
    client_id = (settings.get("client_id") or "").strip()
    if client_id:
        return App(client_id, (secrets.get("client_secret") or "").strip(), True)
    if DEFAULT_CLIENT_ID:
        return App(DEFAULT_CLIENT_ID, DEFAULT_CLIENT_SECRET, False)
    return None


def _error(response: httpx.Response) -> tuple[str, str]:
    try:
        data = response.json()
    except ValueError:
        return "error", response.text[:200]
    return str(data.get("error") or "error"), str(data.get("error_description") or data.get("error") or "")


class YandexOAuth:
    def __init__(self, app: App, http: httpx.Client):
        self.app = app
        self.http = http

    def _credentials(self) -> dict:
        creds = {"client_id": self.app.client_id}
        if self.app.client_secret:
            creds["client_secret"] = self.app.client_secret
        return creds

    def device_code(self, device_id: str, device_name: str = "Пульс") -> dict:
        response = self.http.post(f"{OAUTH}/device/code", data={"client_id": self.app.client_id,
                                                                 "device_id": device_id, "device_name": device_name})
        if response.status_code != 200:
            code, text = _error(response)
            if code in ("invalid_client", "unauthorized_client"):
                raise OAuthError("Яндекс не узнал приложение OAuth — проверьте client_id в настройках плагина")
            raise OAuthError(f"Яндекс не выдал код входа: {text or code}")
        data = response.json()
        return {"device_code": data["device_code"], "user_code": data["user_code"],
                "verification_url": data.get("verification_url") or f"{OAUTH}/device",
                "interval": int(data.get("interval") or 5), "expires_at": time.time() + int(data.get("expires_in") or 600)}

    def _token(self, form: dict) -> dict:
        response = self.http.post(f"{OAUTH}/token", data={**form, **self._credentials()})
        if response.status_code != 200:
            code, text = _error(response)
            if code == "authorization_pending":
                raise Pending()
            if code in ("invalid_grant", "expired_token"):
                raise OAuthError("Код входа истёк или уже использован — начните вход заново")
            if code == "invalid_client":
                raise OAuthError("Яндекс не принял приложение OAuth — проверьте client_id и client_secret в настройках")
            raise OAuthError(f"Яндекс отказал во входе: {text or code}")
        data = response.json()
        return {"access_token": data["access_token"], "refresh_token": data.get("refresh_token"),
                "expires_at": time.time() + int(data.get("expires_in") or 3600 * 24 * 365)}

    def poll(self, device_code: str) -> dict:
        """Токены, если владелец уже ввёл код; иначе Pending."""
        return self._token({"grant_type": "device_code", "code": device_code})

    def refresh(self, refresh_token: str) -> dict:
        return self._token({"grant_type": "refresh_token", "refresh_token": refresh_token})
