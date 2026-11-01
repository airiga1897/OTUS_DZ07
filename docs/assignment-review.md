# Соответствие заданию ДЗ07

| Требование | Реализация | Подтверждение |
| --- | --- | --- |
| Три узла Elasticsearch | [Топология](../config/topology.json), [роль ES](../ansible/roles/elasticsearch/tasks/main.yml) | [Полный test-cycle ru-2a](ru2-test-cycle-summary.json): три ES-узла на трёх физических хостах, кластер green, документы сохранены после повторного deploy |
| Discovery и сеть | [Шаблон ES](../ansible/roles/elasticsearch/templates/elasticsearch.yml.j2), [Terraform](../terraform-selectel/main.tf) | Bootstrap и TLS применены; [API подтвердил разнесение, фильтрацию и маршруты](network-result.json) |
| Логи веб-сервера, БД и балансировщика | Nginx и HAProxy на app, Django, PostgreSQL и Kibana на app, Filebeat на каждой ВМ; [параметры](../config/services.json) | [Подтверждено поступление всех семи видов журналов](logs-verification.json), включая PostgreSQL, Nginx и HAProxy |
| Индексы и обработка | [ELK API](../scripts/remote/elk_api.py): logs-otus-*, реплика, политика удаления, разбор Nginx | Настройка применена в Ansible; запись контрольного документа ES прошла |
| Поступление и отображение | [Проверки](../scripts/observability.py), [Kibana](../ansible/roles/kibana/tasks/main.yml) | [API подтвердил](logs-verification.json) доступность Kibana, представление `otus-logs` и дешборд; проверены документы всех источников |
| Скрипты, примеры логов, README | [README](../README.md), [сценарии](../scripts/lab.py), [роли](../ansible/site.yml) | [Реальные обезличенные примеры](logs-verification.json), [инструкция Releases](releases.md), [подтверждение удаления](cleanup-result.json) |

Облачный test-cycle использовал доставку S3. Чистый клон с Releases проверен
локально без S3; опубликованный выпуск требует отдельного облачного испытания.
Управляемый балансировщик Selectel и сбор его логов
не используются; требование балансировщика покрывает собственный HAProxy.
