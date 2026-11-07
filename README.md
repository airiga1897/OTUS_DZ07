# ДЗ07. Централизованный сбор логов в Elasticsearch

## Цель работы

Развернуть кластер Elasticsearch из трёх узлов, собирать журналы
веб-сервера, базы данных и балансировщика и просматривать их в Kibana.
[Задание](docs/assignment.md) · [Соответствие требованиям](docs/assignment-review.md)

## Архитектура

| ВМ | Компоненты |
| --- | --- |
| `app` | HAProxy, Nginx, Django/uWSGI, PostgreSQL 16, Kibana 8.19.0, Filebeat |
| `es1`, `es2`, `es3` | Elasticsearch 8.19.0 и Filebeat на каждой ВМ |

Terraform создаёт инфраструктуру Selectel, Ansible настраивает службы.
Узлы Elasticsearch обнаруживают друг друга по приватным адресам, используют
TLS и хранят одну реплику индексов. Filebeat на четырёх ВМ отправляет журналы
в `logs-otus-*`. В Kibana доступны дешборд и поиск по источнику и ВМ.
При отказе ВМ `app` сайт, PostgreSQL и Kibana становятся недоступны.
[Схема и потоки данных](docs/architecture.md)

## Развёртывание и проверка

Команды выполняются в PowerShell 5.1 или 7 из корня клона ветки `elkcluster`.
Нужны Windows x64, Git и проект Selectel с сервисным пользователем с ролью
`member`. Для межпулового размещения и аудита общих ресурсов см. требования
к доступу. [Создание пользователя](https://docs.selectel.ru/access-control/manage/add-user/) · [Назначение ролей](https://docs.selectel.ru/access-control/manage/edit-user-data-or-role/) · [Требования к доступу](docs/distributed-placement.md#доступ)

**1. Подготовить инструменты и ввести реквизиты.** Подготовка скачивает
Python 3.12 и Terraform в каталог клона и создаёт виртуальное окружение.
`env.ps1` запросит номер аккаунта, UUID проекта, имя и пароль сервисного
пользователя. Пароль вводится скрыто, реквизиты остаются в текущей сессии.

```powershell
.\scripts\setup.ps1
. .\scripts\env.ps1
```

**2. Развернуть стенд.** Предварительные проверки входят в `deploy`.
ВМ получают исходники и пакеты из закреплённого GitHub Release с проверкой
SHA512. Для основного сценария достаточно реквизитов Selectel, введённых выше.

```powershell
.\.tools\venv\Scripts\python.exe scripts/lab.py deploy
```

**3. Проверить стенд и открыть сайт.** После успешного `verify` команда
`open` открывает сайт и показывает адрес Kibana.

```powershell
.\.tools\venv\Scripts\python.exe scripts/lab.py verify
.\.tools\venv\Scripts\python.exe scripts/lab.py open
```

В Kibana войдите как `elastic`. Пароль берётся из поля `elastic` локального
файла `.local/secrets.json`. Команда копирует его без вывода в консоль.

```powershell
(Get-Content .\.local\secrets.json -Raw | ConvertFrom-Json).elastic | Set-Clipboard
```

**4. Сохранить результаты.** Сделайте снимок дешборда с событиями и видимым
периодом. В **Discover** выберите `logs-otus-*` и сохраните подтверждение
логов со всех четырёх ВМ по полю `host.name`. Сохраните обезличенные примеры
событий Nginx, PostgreSQL, HAProxy и Elasticsearch и итог `verify`.
[Чек-лист Kibana](docs/operations.md#проверка-kibana-и-сохранение-результатов)

**5. Удалить стенд.** После сохранения результатов выполните отдельную
команду. Дождитесь подтверждения очистки через state и Selectel API.
При ошибке сохраните вывод и локальные файлы восстановления.

```powershell
.\.tools\venv\Scripts\python.exe scripts/lab.py destroy
```

После успешного удаления очистите реквизиты из текущей сессии PowerShell.

```powershell
. .\scripts\env.ps1 -Clear
```

## Подтверждённые результаты

В облачном испытании базовой схемы в ru-2 подтверждены три узла Elasticsearch
в состоянии `green`, сайт, семь видов журналов, доступность Kibana и очистка.
Этот прогон использовал режим `cached`. Сведения об опубликованном выпуске и
границах проверки дешборда приведены отдельно. [Отчёты и примеры логов](docs/results.md)

## Дополнительные материалы

| Тема | Материалы |
| --- | --- |
| Устройство и ограничения | [Архитектура](docs/architecture.md), [решения и повторное использование](docs/adr/0007-scope-and-reuse.md) |
| Настройка и доставка | [Настройки запуска](docs/run-settings.md), [чистый клон и GitHub Releases](docs/releases.md), [отдельный режим S3 для разработки](docs/artifacts.md) |
| Размещение | [Подбор пула, образа и дисков](docs/placement.md), [межпуловая связь](docs/distributed-placement.md) |
| Эксплуатация | [Повторный deploy и очистка](docs/operations.md), [восстановление правил SSH и Kibana](docs/operations.md#ручная-проверка-восстановления-правил-доступа), [операции Ansible](docs/ansible-operations.md) |
| Дополнительные проверки | [Добавление es4 и проверка отказоустойчивости](docs/optional-elasticsearch.md), [локальные тесты и проверки качества](docs/local-quality.md) |
| Документация проекта | [Навигация](docs/README.md), [ADR](docs/adr/README.md) |

`preflight` запускает предварительную проверку отдельно. `test-cycle`
предназначен для временного испытания и удаляет созданный им тестовый стенд.
Обе команды описаны в [эксплуатационной документации](docs/operations.md).

Для разработки `. .\scripts\env.ps1 -SaveLocal` явно сохраняет реквизиты
локально, а `. .\scripts\env.ps1 -Development` (или `-Dev`) загружает их
в новую сессию. Схема реквизитов хранится отдельно от настроек размещения.
[Схема реквизитов Selectel](config/selectel-auth.json)

Исходники разделены по назначению: [Terraform](terraform-selectel/staged/),
[Ansible](ansible/), [контроллер и проверки](scripts/), [портал](app/),
[локальные тесты](tests/).
Начальные данные подготовлены по материалам ДЗ04 и загружаются
Django-фикстурой. [Источник основы портала из ДЗ06](config/upstream.json)
