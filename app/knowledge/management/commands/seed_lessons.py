from django.core.management.base import BaseCommand
from knowledge.models import Article

LESSONS = [
    (1, "terraform-yc", "Первая инфраструктура в Yandex Cloud", "Terraform, сеть и безопасный SSH-доступ.",
     "ДЗ01 посвящено созданию виртуальной машины через Terraform.\nКонфигурация инфраструктуры хранится в Git, реальные параметры и state остаются локальными.\nИсходники: https://github.com/airiga1897/OTUS_DZ01"),
    (2, "shared-gfs2", "Общее хранилище GFS2", "iSCSI, multipath и распределённые блокировки.",
     "ДЗ02 объединяет iSCSI-хранилище и несколько клиентов GFS2.\nПроверяются общий доступ и перекрёстная запись.\nИсходники: https://github.com/airiga1897/OTUS_DZ02"),
    (3, "nginx-balance", "Балансировка веб-приложения", "Два веб-узла, общая БД и проверки переключения.",
     "ДЗ03 использует Nginx, PHP-FPM и WordPress. Проверены балансировка и отказ веб-службы.\nИсходники: https://github.com/airiga1897/OTUS_DZ03"),
    (4, "high-load", "Отказоустойчивое приложение", "Пять ВМ, два фронта и эксперимент DRBD + GFS2.",
     "ДЗ04 объединяет инфраструктуру и веб-приложение.\nDRBD dual-primary + GFS2 — учебный эксперимент в неподдерживаемом режиме; результаты отказных испытаний публикуются только после их выполнения.\nДля промышленного применения рекомендуется отказоустойчивое общее блочное хранилище с резервируемыми iSCSI-контроллерами и несколькими путями доступа.\nИсходники: https://github.com/airiga1897/OTUS_DZ04"),
]

class Command(BaseCommand):
    help = "Добавить исходные материалы без перезаписи редакторских изменений."

    def handle(self, *args: object, **options: object) -> None:
        for lesson, slug, title, summary, body in LESSONS:
            Article.objects.get_or_create(slug=slug, defaults={"lesson": lesson, "title": title,
                                          "summary": summary, "body": body, "published": True})
        self.stdout.write("Начальные материалы готовы.")
