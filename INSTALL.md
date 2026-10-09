# Установка плагина «Яндекс Диск»

Инструкция для агента Пульса после `pulse plugins install …`. Пройди её целиком.

## 1. Проверь, что плагин запустился

```bash
pulse plugins list            # у yadisk — status: running
pulse yadisk accounts         # app.configured: true — приложение Яндекс OAuth есть; accounts: [] до входа
```

Если status: error — `pulse plugins logs yadisk --lines 80`, расскажи владельцу, что в журнале, и остановись.
Нужен Пульс с контрактом плагинов 1.3 (папки-хранилища @<плагин>).

## 2. Приложение Яндекс OAuth

По умолчанию вход идёт через приложение «Пульс» — ничего настраивать не нужно. Если `app.configured: false`
(общего приложения в этой версии нет) или владелец хочет своё приложение (например, корпоративное), проведи его:

1. oauth.yandex.ru → «Создать приложение»; платформа — «Для доступа к API или отладки» (Redirect URI —
   https://oauth.yandex.ru/verification_code);
2. права: «Яндекс Диск REST API» — чтение всего Диска, запись в любом месте Диска, доступ к информации о Диске
   (cloud_api:disk.read, cloud_api:disk.write, cloud_api:disk.info);
3. ClientID и Client secret из карточки приложения — в настройки плагина:

```bash
pulse plugins settings yadisk set client_id=<ClientID>
pulse plugins secret yadisk client_secret         # секрет вводит владелец, не пиши его в чат сам
```

## 3. Закрепи страницу

```bash
pulse plugins pin yadisk main
```

## 4. Вход — делает владелец сам

Пароль Яндекса вводит только владелец, на сайте Яндекса, в своём браузере. Не проси у него пароль и коды. Либо
отправь владельца на страницу [Яндекс Диск](/p/yadisk/main) → «Войти в аккаунт Яндекса», либо:

```bash
pulse yadisk login            # user_code и verification_url — передай владельцу текст из tell_owner
pulse yadisk login status     # done — вход выполнен, весь диск аккаунта подключён папкой @yadisk/<имя>
```

Аккаунтов может быть несколько: каждый следующий вход — ещё один аккаунт.

## 5. Подключения (если владелец просит)

```bash
pulse yadisk browse --account <логин>                           # папки, в том числе принятые общие
pulse yadisk mount folder --account <логин> --path "/Проект Альфа"
pulse yadisk mount public --url https://disk.yandex.ru/d/…      # публичная ссылка, только чтение
pulse yadisk mounts
```

## 6. Проверь

```bash
pulse files ls @yadisk        # подключения как папки
pulse files ls "@yadisk/<подключение>"
```

## 7. Расскажи владельцу

Что установлен «Яндекс Диск» (версия), страница закреплена в меню, Диск виден в «Файлах» папкой @yadisk и доступен
агентам; можно войти в несколько аккаунтов, подключить отдельные и общие папки и публичные ссылки. Вход продлевается
сам; если он истечёт — придёт сигнал «Войдите в Яндекс снова».
